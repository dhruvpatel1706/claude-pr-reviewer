"""Tests for the GitHub review posting path. Subprocesses are stubbed — no network."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from claude_pr_reviewer.github import build_review_payload, post_review, resolve_pr
from claude_pr_reviewer.models import Finding, Review


@dataclass
class _CompletedProcess:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def _finding(**over) -> Finding:
    base = dict(
        category="bug",
        severity="high",
        file_path="src/app.py",
        start_line=10,
        end_line=12,
        title="null pointer",
        description="If list is empty, indexing raises.",
        suggested_fix="```python\nif not items: return None\n```",
    )
    base.update(over)
    return Finding(**base)


# --- resolve_pr ---------------------------------------------------------------


def test_resolve_pr_parses_url():
    def runner(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _CompletedProcess(
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/pull/42",
                    "number": 42,
                }
            )
        )

    owner, repo, num = resolve_pr("42", runner=runner)
    assert (owner, repo, num) == ("acme", "widgets", 42)


def test_resolve_pr_raises_on_bad_url():
    def runner(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _CompletedProcess(stdout=json.dumps({"url": "not a url", "number": 42}))

    with pytest.raises(RuntimeError, match="Unexpected PR URL"):
        resolve_pr("42", runner=runner)


# --- build_review_payload -----------------------------------------------------


def test_build_payload_event_maps_from_recommendation():
    r = Review(
        summary="Looks good.",
        findings=[],
        overall_recommendation="approve",
    )
    assert build_review_payload(r)["event"] == "APPROVE"

    r2 = Review(
        summary="Has issues.",
        findings=[_finding(severity="critical")],
        overall_recommendation="request-changes",
    )
    assert build_review_payload(r2)["event"] == "REQUEST_CHANGES"


def test_build_payload_inline_comment_shape():
    r = Review(
        summary="One finding.",
        findings=[_finding(start_line=10, end_line=10)],
        overall_recommendation="request-changes",
    )
    payload = build_review_payload(r)
    assert len(payload["comments"]) == 1
    c = payload["comments"][0]
    assert c["path"] == "src/app.py"
    assert c["line"] == 10
    assert c["side"] == "RIGHT"
    assert "null pointer" in c["body"]
    # Single-line: no start_line
    assert "start_line" not in c


def test_build_payload_multi_line_comment_includes_start_fields():
    r = Review(
        summary="x",
        findings=[_finding(start_line=10, end_line=15)],
        overall_recommendation="request-changes",
    )
    payload = build_review_payload(r)
    c = payload["comments"][0]
    assert c["line"] == 15
    assert c["start_line"] == 10
    assert c["start_side"] == "RIGHT"


def test_build_payload_file_level_finding_folds_into_body():
    r = Review(
        summary="File-level note.",
        findings=[_finding(start_line=0, end_line=0, title="missing LICENSE")],
        overall_recommendation="comment",
    )
    payload = build_review_payload(r)
    assert payload["comments"] == []
    assert "File-level notes" in payload["body"]
    assert "missing LICENSE" in payload["body"]


# --- post_review --------------------------------------------------------------


def test_post_review_pipes_json_payload():
    captured = {}

    def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return _CompletedProcess(stdout=json.dumps({"id": 999, "state": "COMMENTED"}))

    r = Review(
        summary="ok",
        findings=[_finding()],
        overall_recommendation="request-changes",
    )
    result = post_review("acme", "widgets", 42, r, runner=runner)
    assert result == {"id": 999, "state": "COMMENTED"}
    assert captured["cmd"][:2] == ["gh", "api"]
    assert "repos/acme/widgets/pulls/42/reviews" in captured["cmd"]
    payload = json.loads(captured["input"])
    assert payload["event"] == "REQUEST_CHANGES"
    assert len(payload["comments"]) == 1
