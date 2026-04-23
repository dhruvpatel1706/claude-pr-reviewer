"""Pydantic schema for structured code-review findings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]
Category = Literal["bug", "security", "performance", "style", "tests", "docs", "design"]


class Finding(BaseModel):
    """A single issue identified in the diff."""

    category: Category = Field(description="Which bucket this falls into.")
    severity: Severity = Field(
        description=(
            "critical = ship-blocker correctness/security; high = serious bug or regression; "
            "medium = noteworthy issue worth fixing; low = nitpick or minor smell; "
            "info = informational comment."
        )
    )
    file_path: str = Field(description="Path to the file, as it appears in the diff.")
    start_line: int = Field(
        description="Line number (in the NEW file) where the issue starts. Use 0 if file-level."
    )
    end_line: int = Field(
        description="Line number (in the NEW file) where the issue ends. Same as start if single-line."
    )
    title: str = Field(description="One-line summary of the issue (≤80 chars).")
    description: str = Field(
        description="Full explanation: what the issue is, why it matters, and how it happens here."
    )
    suggested_fix: str = Field(
        default="",
        description=(
            "A concrete suggested change. Can be a short code snippet in a fenced block, or "
            "an empty string if the fix is obvious from `description`."
        ),
    )


class Review(BaseModel):
    """Complete review of a diff."""

    summary: str = Field(
        description="2-4 sentence high-level assessment. Call out the biggest issue, if any."
    )
    findings: list[Finding] = Field(default_factory=list)
    overall_recommendation: Literal["approve", "request-changes", "comment"] = Field(
        description=(
            "'approve' = safe to merge as-is; 'request-changes' = at least one critical/high "
            "finding; 'comment' = medium/low findings only, author judgment call."
        )
    )
