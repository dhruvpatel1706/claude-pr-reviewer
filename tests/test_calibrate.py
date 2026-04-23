"""Tests for the calibration harness.

No Anthropic calls are made — we build synthetic Reviews and spec YAML on
the fly and exercise the matching + scoring logic directly.
"""

from __future__ import annotations

import textwrap

import pytest

from claude_pr_reviewer.calibrate import (
    CalibrationCase,
    Expected,
    load_set,
    matches,
    run,
    score_case,
)
from claude_pr_reviewer.models import Finding, Review


def _finding(**over) -> Finding:
    base = {
        "category": "bug",
        "severity": "high",
        "file_path": "src/db.py",
        "start_line": 42,
        "end_line": 42,
        "title": "SQL injection via string format",
        "description": "Concatenating user input into a SQL string is unsafe",
        "suggested_fix": "Use parameterized queries",
    }
    base.update(over)
    return Finding(**base)


def test_matches_exact_line_same_file():
    f = _finding()
    e = Expected(file_path="src/db.py", line_range=(42, 42))
    assert matches(f, e)


def test_matches_ignores_different_file():
    f = _finding(file_path="src/other.py")
    e = Expected(file_path="src/db.py", line_range=(42, 42))
    assert not matches(f, e)


def test_matches_allows_line_slack():
    # Reporter said line 44, expected said 42-42. Slack of 2 covers that.
    f = _finding(start_line=44, end_line=44)
    e = Expected(file_path="src/db.py", line_range=(42, 42))
    assert matches(f, e)


def test_matches_rejects_line_outside_slack():
    f = _finding(start_line=50, end_line=50)
    e = Expected(file_path="src/db.py", line_range=(42, 42))
    assert not matches(f, e)


def test_matches_overlapping_ranges_count():
    f = _finding(start_line=40, end_line=45)
    e = Expected(file_path="src/db.py", line_range=(44, 50))
    assert matches(f, e)


def test_matches_category_mismatch_rejected():
    f = _finding(category="style")
    e = Expected(file_path="src/db.py", line_range=(42, 42), category="security")
    assert not matches(f, e)


def test_matches_severity_floor_rejects_below():
    f = _finding(severity="low")
    e = Expected(file_path="src/db.py", line_range=(42, 42), min_severity="high")
    assert not matches(f, e)


def test_matches_severity_floor_accepts_higher():
    f = _finding(severity="critical")
    e = Expected(file_path="src/db.py", line_range=(42, 42), min_severity="high")
    assert matches(f, e)


def test_matches_keyword_title_hit_case_insensitive():
    f = _finding(title="SQL Injection Risk", description="blah")
    e = Expected(file_path="src/db.py", line_range=(42, 42), any_keyword=["sql injection"])
    assert matches(f, e)


def test_matches_keyword_description_fallback():
    f = _finding(title="Issue found", description="Classic SQL injection via %s")
    e = Expected(file_path="src/db.py", line_range=(42, 42), any_keyword=["sql injection"])
    assert matches(f, e)


def test_matches_keyword_all_miss_rejects():
    f = _finding(title="Missing docstring", description="Module has no docstring")
    e = Expected(file_path="src/db.py", line_range=(42, 42), any_keyword=["sql", "injection"])
    assert not matches(f, e)


def test_score_case_one_match_one_miss():
    rep = Review(
        summary="",
        findings=[_finding()],
        overall_recommendation="comment",
    )
    case = CalibrationCase(
        name="t",
        diff_file="/tmp/fake.diff",
        expected=[
            Expected(file_path="src/db.py", line_range=(42, 42), category="bug"),
            Expected(file_path="src/db.py", line_range=(99, 99)),  # not reported
        ],
    )
    result = score_case(case, rep)
    assert result.true_positives == 1
    assert result.false_positives == 0
    assert result.false_negatives == 1
    assert result.precision == 1.0
    assert result.recall == 0.5
    assert result.f1 == pytest.approx(2 / 3)


def test_score_case_false_positive_on_extra_finding():
    rep = Review(
        summary="",
        findings=[
            _finding(),
            _finding(start_line=100, end_line=100, title="Unused import"),
        ],
        overall_recommendation="comment",
    )
    case = CalibrationCase(
        name="t",
        diff_file="/tmp/fake.diff",
        expected=[
            Expected(file_path="src/db.py", line_range=(42, 42), category="bug"),
        ],
    )
    result = score_case(case, rep)
    assert result.true_positives == 1
    assert result.false_positives == 1
    assert result.false_negatives == 0
    assert result.precision == 0.5
    assert result.recall == 1.0


def test_score_case_each_expected_matches_at_most_one_reported():
    # Two reported findings on the same line+file — should still only count as 1 TP.
    rep = Review(
        summary="",
        findings=[_finding(title="A"), _finding(title="B")],
        overall_recommendation="comment",
    )
    case = CalibrationCase(
        name="t",
        diff_file="/tmp/fake.diff",
        expected=[Expected(file_path="src/db.py", line_range=(42, 42))],
    )
    result = score_case(case, rep)
    assert result.true_positives == 1
    # The second reported counts as a false positive.
    assert result.false_positives == 1


def test_load_set_parses_valid_spec(tmp_path):
    diff = tmp_path / "d.diff"
    diff.write_text("diff --git a/x b/x\n", encoding="utf-8")
    spec = tmp_path / "spec.yml"
    spec.write_text(
        textwrap.dedent("""
            cases:
              - name: case-one
                diff_file: d.diff
                expected:
                  - file_path: src/a.py
                    line_range: [10, 12]
                    category: bug
                    min_severity: high
                    any_keyword: [null pointer, nil deref]
            """),
        encoding="utf-8",
    )
    cases = load_set(spec)
    assert len(cases) == 1
    c = cases[0]
    assert c.name == "case-one"
    assert c.diff_file.resolve() == diff.resolve()
    assert len(c.expected) == 1
    exp = c.expected[0]
    assert exp.file_path == "src/a.py"
    assert exp.line_range == (10, 12)
    assert exp.category == "bug"
    assert exp.min_severity == "high"
    assert list(exp.any_keyword) == ["null pointer", "nil deref"]


def test_load_set_rejects_missing_cases(tmp_path):
    spec = tmp_path / "bad.yml"
    spec.write_text("something_else: yes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level `cases`"):
        load_set(spec)


def test_load_set_rejects_bad_line_range(tmp_path):
    diff = tmp_path / "d.diff"
    diff.write_text("x", encoding="utf-8")
    spec = tmp_path / "spec.yml"
    spec.write_text(
        textwrap.dedent("""
            cases:
              - name: broken
                diff_file: d.diff
                expected:
                  - file_path: a.py
                    line_range: [1]
            """),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="line_range must be"):
        load_set(spec)


def test_run_end_to_end_with_stubbed_review_fn(tmp_path):
    diff = tmp_path / "d.diff"
    diff.write_text(
        "diff --git a/src/db.py b/src/db.py\n--- a/src/db.py\n+++ b/src/db.py\n",
        encoding="utf-8",
    )
    spec = tmp_path / "spec.yml"
    spec.write_text(
        textwrap.dedent("""
            cases:
              - name: sql-case
                diff_file: d.diff
                expected:
                  - file_path: src/db.py
                    line_range: [42, 42]
                    any_keyword: [sql]
            """),
        encoding="utf-8",
    )

    def fake_review(text):
        return Review(
            summary="stubbed",
            findings=[_finding()],
            overall_recommendation="comment",
        )

    summary = run(spec, review_fn=fake_review)
    assert summary.total_tp == 1
    assert summary.total_fp == 0
    assert summary.total_fn == 0
    assert summary.precision == 1.0
    assert summary.recall == 1.0
    assert summary.f1 == 1.0


def test_run_invokes_case_start_hook(tmp_path):
    diff = tmp_path / "d.diff"
    diff.write_text("x", encoding="utf-8")
    spec = tmp_path / "spec.yml"
    spec.write_text(
        textwrap.dedent("""
            cases:
              - name: a
                diff_file: d.diff
                expected: [{file_path: x, line_range: [1, 1]}]
              - name: b
                diff_file: d.diff
                expected: [{file_path: x, line_range: [1, 1]}]
            """),
        encoding="utf-8",
    )

    def fake_review(text):
        return Review(summary="", findings=[], overall_recommendation="approve")

    names: list[str] = []
    run(spec, review_fn=fake_review, on_case_start=names.append)
    assert names == ["a", "b"]
