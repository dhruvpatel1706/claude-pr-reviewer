"""Call Claude to produce a structured `Review` from diff text."""

from __future__ import annotations

import anthropic

from claude_pr_reviewer import cache as _cache
from claude_pr_reviewer.config import Settings
from claude_pr_reviewer.diff import FileDiff, ParsedDiff, fit_into_budget, parse_diff
from claude_pr_reviewer.models import Finding, Review
from claude_pr_reviewer.repo_config import RepoConfig, filter_findings

SYSTEM_PROMPT = """You are a senior software engineer reviewing a pull request diff. \
Your job is to find issues, not to praise what's already good.

Principles:
- Be specific. Every finding must name a file + line range and a concrete issue.
- Focus on the CHANGE. Don't flag pre-existing code that wasn't touched by this diff.
- Don't invent problems. If you aren't sure there's a bug, make it `severity: info` \
and phrase it as a question.
- A clean diff with no issues is fine — return an empty `findings` list and `approve`.
- Calibrate severity strictly:
  * critical: ship-blocker — actual correctness bug, introduced security vuln, or data loss risk.
  * high: likely regression, missing error handling for realistic failure modes.
  * medium: worth fixing before merge — subtle bug, stale comment, missing test.
  * low: style, minor naming, nit-level.
  * info: observation or open question for the author, not a defect.
- `overall_recommendation` must be consistent with the findings' severity:
  * request-changes only if at least one `critical` or `high` finding.
  * approve only if findings list is empty or only contains `info`.
  * comment otherwise.
- For `suggested_fix`, write the actual replacement code in a fenced block when \
helpful. Omit if the fix is trivially obvious from `description`.
- Flag security issues aggressively: hardcoded secrets, injection, auth bypass, \
unvalidated input on network boundaries.

Line numbers refer to the NEW file (the `+` side of the diff)."""

# Severity -> rank for picking the strictest recommendation across per-file reviews
_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _recommendation_from(findings: list[Finding]) -> str:
    if not findings:
        return "approve"
    max_sev = max(_SEVERITY_RANK[f.severity] for f in findings)
    if max_sev >= _SEVERITY_RANK["high"]:
        return "request-changes"
    if max_sev <= _SEVERITY_RANK["info"]:
        return "approve"
    return "comment"


def _run_single_call(
    client: anthropic.Anthropic,
    settings: Settings,
    diff_text: str,
    extra_note: str = "",
    repo_instructions: str = "",
    use_cache: bool = True,
) -> Review:
    system_prompt = SYSTEM_PROMPT
    if repo_instructions.strip():
        system_prompt = (
            SYSTEM_PROMPT
            + "\n\n---\nRepo-specific conventions from .claude-review.yml:\n"
            + repo_instructions.strip()
        )

    # Check disk cache first (v0.5). Model + repo_instructions are part of the
    # key, so swapping either invalidates automatically.
    cache_extra = repo_instructions
    if use_cache:
        cached = _cache.get(diff_text, model=settings.model, extra=cache_extra)
        if cached is not None:
            return cached

    user_prompt = (
        f"Here is the diff. Review it against the principles in the system prompt."
        f"{extra_note}\n\n```diff\n{diff_text}\n```"
    )
    response = client.messages.parse(
        model=settings.model,
        max_tokens=6000,
        thinking={"type": "adaptive"},
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
        output_format=Review,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"Review parsing failed. stop_reason={response.stop_reason}")

    result = response.parsed_output
    if use_cache:
        try:
            _cache.put(diff_text, result, model=settings.model, extra=cache_extra)
        except OSError:
            pass  # cache misses shouldn't break the review
    return result


def _merge_per_file_reviews(
    file_reviews: list[tuple[FileDiff, Review]],
    skipped: list[str],
) -> Review:
    """Combine per-file `Review` objects into one."""
    all_findings: list[Finding] = []
    per_file_summaries: list[str] = []
    for fd, r in file_reviews:
        all_findings.extend(r.findings)
        if r.summary:
            per_file_summaries.append(f"- `{fd.path}`: {r.summary}")

    if not per_file_summaries:
        summary = "No issues found across the reviewed files."
    else:
        summary = "Reviewed per file due to diff size. Per-file notes:\n" + "\n".join(
            per_file_summaries
        )
    if skipped:
        summary += "\n\nSkipped (oversize even split per file): " + ", ".join(skipped)

    return Review(
        summary=summary,
        findings=all_findings,
        overall_recommendation=_recommendation_from(all_findings),  # type: ignore[arg-type]
    )


def _should_chunk(parsed: ParsedDiff, max_chars: int) -> bool:
    """Auto-chunk when total diff size exceeds the single-call budget."""
    return parsed.total_chars > max_chars and len(parsed.files) > 1


def review_diff_text(
    diff_text: str,
    settings: Settings | None = None,
    *,
    per_file: bool | None = None,
    repo_config: RepoConfig | None = None,
    use_cache: bool = True,
) -> Review:
    """Run the full review pipeline on raw unified-diff text.

    `per_file`: if None (default), the function picks — chunks by file if the
    diff exceeds `settings.max_input_chars`, single call otherwise. Force
    `True` to always split, `False` to always single-call and truncate.

    `repo_config`: optional per-repo overrides loaded from `.claude-review.yml`.
    When present, its `model` / `max_input_chars` seed the active settings
    (only if the caller didn't explicitly override them), its
    `ignore_paths` / `ignore_categories` filter findings afterwards, and
    its `extra_instructions` is appended to the system prompt so Claude
    knows about repo-specific conventions before reviewing.
    """
    settings = settings or Settings()

    if repo_config is not None:
        defaults = Settings()
        if repo_config.model and settings.model == defaults.model:
            settings = settings.model_copy(update={"model": repo_config.model})
        if repo_config.max_input_chars and settings.max_input_chars == defaults.max_input_chars:
            settings = settings.model_copy(update={"max_input_chars": repo_config.max_input_chars})

    parsed = parse_diff(diff_text)
    if not parsed.files:
        return Review(
            summary="No diff content to review.",
            findings=[],
            overall_recommendation="approve",
        )

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key.")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    repo_instructions = (repo_config.extra_instructions if repo_config else "") or ""

    if per_file is None:
        per_file = _should_chunk(parsed, settings.max_input_chars)

    if not per_file:
        trimmed_text, skipped = fit_into_budget(parsed, settings.max_input_chars)
        skip_note = ""
        if skipped:
            skip_note = (
                "\n\nNOTE: The following files were skipped to fit the review budget; "
                "please re-run the reviewer with a smaller scope if they matter:\n"
                + "\n".join(f"  - {p}" for p in skipped)
            )
        review = _run_single_call(
            client,
            settings,
            trimmed_text,
            extra_note=skip_note,
            repo_instructions=repo_instructions,
            use_cache=use_cache,
        )
    else:
        file_reviews: list[tuple[FileDiff, Review]] = []
        skipped = []
        for fd in parsed.files:
            if len(fd.raw) > settings.max_input_chars:
                skipped.append(fd.path)
                continue
            per_file_note = (
                f"\n(You are reviewing ONE file out of many in this PR: `{fd.path}`. "
                "Stay focused on issues in this file only — don't reference other files.)"
            )
            r = _run_single_call(
                client,
                settings,
                fd.raw,
                extra_note=per_file_note,
                repo_instructions=repo_instructions,
                use_cache=use_cache,
            )
            file_reviews.append((fd, r))
        review = _merge_per_file_reviews(file_reviews, skipped)

    # Post-filter based on repo config. We keep the suppressed-count notice in
    # the summary so the user knows something was hidden.
    if repo_config is not None and (repo_config.ignore_paths or repo_config.ignore_categories):
        kept, dropped = filter_findings(review.findings, repo_config)
        if dropped:
            review = Review(
                summary=(
                    review.summary
                    + f"\n\n_{len(dropped)} finding(s) suppressed by .claude-review.yml._"
                ),
                findings=kept,
                overall_recommendation=_recommendation_from(kept),  # type: ignore[arg-type]
            )
    return review
