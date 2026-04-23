"""Call Claude to produce a structured `Review` from diff text."""

from __future__ import annotations

import anthropic

from claude_pr_reviewer.config import Settings
from claude_pr_reviewer.diff import FileDiff, ParsedDiff, fit_into_budget, parse_diff
from claude_pr_reviewer.models import Finding, Review

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
) -> Review:
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
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
        output_format=Review,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"Review parsing failed. stop_reason={response.stop_reason}")
    return response.parsed_output


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
) -> Review:
    """Run the full review pipeline on raw unified-diff text.

    `per_file`: if None (default), the function picks — chunks by file if the
    diff exceeds `settings.max_input_chars`, single call otherwise. Force
    `True` to always split (useful when you want line-level findings and
    you've hit Claude's context limit elsewhere). `False` to always run a
    single call and rely on `fit_into_budget` truncation.
    """
    settings = settings or Settings()

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

    # Decide which path to take.
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
        return _run_single_call(client, settings, trimmed_text, extra_note=skip_note)

    # Per-file path: review each file independently.
    file_reviews: list[tuple[FileDiff, Review]] = []
    skipped: list[str] = []
    for fd in parsed.files:
        if len(fd.raw) > settings.max_input_chars:
            skipped.append(fd.path)
            continue
        per_file_note = (
            f"\n(You are reviewing ONE file out of many in this PR: `{fd.path}`. "
            "Stay focused on issues in this file only — don't reference other files.)"
        )
        review = _run_single_call(client, settings, fd.raw, extra_note=per_file_note)
        file_reviews.append((fd, review))

    return _merge_per_file_reviews(file_reviews, skipped)
