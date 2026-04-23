"""Format a Review for human or machine consumption."""

from __future__ import annotations

from claude_pr_reviewer.models import Finding, Review

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}
RECOMMENDATION_EMOJI = {
    "approve": "✅",
    "request-changes": "❌",
    "comment": "💬",
}


def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER[f.severity], f.file_path, f.start_line),
    )


def render_markdown(review: Review) -> str:
    """Render the review as a markdown report, suitable for a PR comment."""
    rec_emoji = RECOMMENDATION_EMOJI[review.overall_recommendation]
    lines: list[str] = []
    lines.append(f"## {rec_emoji} Claude review — {review.overall_recommendation}")
    lines.append("")
    lines.append(review.summary)
    lines.append("")

    if not review.findings:
        lines.append("_No issues found._")
        return "\n".join(lines)

    lines.append(f"### Findings ({len(review.findings)})")
    for i, f in enumerate(_sorted_findings(review.findings), 1):
        emoji = SEVERITY_EMOJI[f.severity]
        loc = (
            f"`{f.file_path}`"
            if f.start_line == 0
            else f"`{f.file_path}`:L{f.start_line}"
            + (f"-L{f.end_line}" if f.end_line != f.start_line else "")
        )
        lines.append("")
        lines.append(f"#### {i}. {emoji} [{f.severity}] {f.category}: {f.title}")
        lines.append(f"{loc}")
        lines.append("")
        lines.append(f.description)
        if f.suggested_fix.strip():
            lines.append("")
            lines.append("**Suggested fix:**")
            lines.append("")
            lines.append(f.suggested_fix)
    return "\n".join(lines)


def render_json(review: Review) -> str:
    """Serialize the full review as pretty-printed JSON."""
    return review.model_dump_json(indent=2)
