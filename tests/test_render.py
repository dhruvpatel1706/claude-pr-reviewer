"""Tests for the markdown + JSON renderers."""

from __future__ import annotations

import json

from claude_pr_reviewer.models import Finding, Review
from claude_pr_reviewer.render import render_json, render_markdown


def _finding(**over) -> Finding:
    base = dict(
        category="bug",
        severity="high",
        file_path="src/app.py",
        start_line=10,
        end_line=12,
        title="null pointer on empty input",
        description="If `items` is empty, indexing into it raises.",
        suggested_fix="```python\nif not items:\n    return None\n```",
    )
    base.update(over)
    return Finding(**base)


def test_render_empty_review():
    r = Review(
        summary="Clean diff.",
        findings=[],
        overall_recommendation="approve",
    )
    out = render_markdown(r)
    assert "approve" in out
    assert "No issues found" in out


def test_render_with_findings_sorts_by_severity():
    r = Review(
        summary="Two findings.",
        findings=[
            _finding(severity="low", title="low one"),
            _finding(severity="critical", title="CRIT one"),
        ],
        overall_recommendation="request-changes",
    )
    out = render_markdown(r)
    # Critical should appear before Low
    assert out.index("CRIT one") < out.index("low one")


def test_render_markdown_includes_location_and_fix():
    r = Review(
        summary="One finding.",
        findings=[_finding()],
        overall_recommendation="request-changes",
    )
    out = render_markdown(r)
    assert "src/app.py" in out
    assert "L10-L12" in out
    assert "Suggested fix" in out


def test_render_markdown_single_line_location():
    r = Review(
        summary="x",
        findings=[_finding(start_line=5, end_line=5)],
        overall_recommendation="request-changes",
    )
    out = render_markdown(r)
    assert "L5" in out
    assert "L5-L5" not in out


def test_render_json_valid():
    r = Review(
        summary="JSON roundtrip",
        findings=[_finding()],
        overall_recommendation="request-changes",
    )
    out = render_json(r)
    parsed = json.loads(out)
    assert parsed["overall_recommendation"] == "request-changes"
    assert len(parsed["findings"]) == 1
    assert parsed["findings"][0]["severity"] == "high"
