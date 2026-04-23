"""Tests for review_diff_text's non-API paths."""

from __future__ import annotations

import pytest

from claude_pr_reviewer.config import Settings
from claude_pr_reviewer.review import review_diff_text


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
