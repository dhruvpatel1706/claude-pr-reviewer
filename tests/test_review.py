"""Tests for review_diff_text's non-API paths + per-file merge logic."""

from __future__ import annotations

import pytest

from claude_pr_reviewer.config import Settings
from claude_pr_reviewer.diff import FileDiff
from claude_pr_reviewer.models import Finding, Review
from claude_pr_reviewer.review import (
    _merge_per_file_reviews,
    _recommendation_from,
    _should_chunk,
    review_diff_text,
)


def _finding(severity: str = "high", path: str = "x.py") -> Finding:
    return Finding(
        category="bug",
        severity=severity,  # type: ignore[arg-type]
        file_path=path,
        start_line=1,
        end_line=1,
        title="t",
        description="d",
        suggested_fix="",
    )


def test_empty_diff_returns_clean_approval() -> None:
    review = review_diff_text("", Settings(_env_file=None))
    assert review.overall_recommendation == "approve"
    assert review.findings == []
    assert "No diff content" in review.summary


def test_raises_when_no_api_key_and_diff_present() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n"
        "index 111..222 100644\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        review_diff_text(diff, Settings(_env_file=None))


def test_recommendation_maps_from_max_severity() -> None:
    assert _recommendation_from([]) == "approve"
    assert _recommendation_from([_finding("info")]) == "approve"
    assert _recommendation_from([_finding("low")]) == "comment"
    assert _recommendation_from([_finding("medium")]) == "comment"
    assert _recommendation_from([_finding("high")]) == "request-changes"
    assert _recommendation_from([_finding("critical")]) == "request-changes"
    # max wins when mixed
    assert _recommendation_from([_finding("low"), _finding("critical")]) == "request-changes"


def test_merge_combines_findings_and_picks_strictest() -> None:
    a = FileDiff(path="a.py", additions=1, deletions=0, raw="raw-a")
    b = FileDiff(path="b.py", additions=2, deletions=1, raw="raw-b")
    ra = Review(
        summary="a has a nit",
        findings=[_finding("low", path="a.py")],
        overall_recommendation="comment",
    )
    rb = Review(
        summary="b has a real bug",
        findings=[_finding("critical", path="b.py")],
        overall_recommendation="request-changes",
    )
    merged = _merge_per_file_reviews([(a, ra), (b, rb)], skipped=[])
    assert len(merged.findings) == 2
    assert merged.overall_recommendation == "request-changes"
    assert "a.py" in merged.summary and "b.py" in merged.summary


def test_merge_reports_skipped_files() -> None:
    merged = _merge_per_file_reviews([], skipped=["giant.py"])
    assert "giant.py" in merged.summary
    assert merged.findings == []
    assert merged.overall_recommendation == "approve"


def test_should_chunk_triggers_on_oversize_multi_file(monkeypatch) -> None:
    from claude_pr_reviewer.diff import parse_diff

    # Two small files under the default budget
    diff = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/b.py b/b.py\nindex 3..4 100644\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    parsed = parse_diff(diff)
    assert not _should_chunk(parsed, max_chars=10_000)
    # Shrink budget to force chunk
    assert _should_chunk(parsed, max_chars=50)
