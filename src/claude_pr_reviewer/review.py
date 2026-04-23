"""Call Claude to produce a structured `Review` from diff text."""

from __future__ import annotations

import anthropic

from claude_pr_reviewer.config import Settings
from claude_pr_reviewer.diff import fit_into_budget, parse_diff
from claude_pr_reviewer.models import Review

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


def review_diff_text(diff_text: str, settings: Settings | None = None) -> Review:
    """Run the full review pipeline on raw unified-diff text."""
    settings = settings or Settings()

    parsed = parse_diff(diff_text)
    if not parsed.files:
        return Review(
            summary="No diff content to review.",
            findings=[],
            overall_recommendation="approve",
        )

    trimmed_text, skipped = fit_into_budget(parsed, settings.max_input_chars)

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key.")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    skip_note = ""
    if skipped:
        skip_note = (
            "\n\nNOTE: The following files were skipped to fit the review budget; "
            "please re-run the reviewer with a smaller scope if they matter:\n"
            + "\n".join(f"  - {p}" for p in skipped)
        )

    user_prompt = (
        f"Here is the diff. Review it against the principles in the system prompt.{skip_note}\n\n"
        f"```diff\n{trimmed_text}\n```"
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
