"""Tests for the on-disk review cache."""

from __future__ import annotations

from claude_pr_reviewer.cache import _key, clear, get, put
from claude_pr_reviewer.models import Finding, Review


def _rev() -> Review:
    return Review(
        summary="looked fine",
        findings=[
            Finding(
                category="style",
                severity="low",
                file_path="x.py",
                start_line=3,
                end_line=3,
                title="t",
                description="d",
                suggested_fix="",
            )
        ],
        overall_recommendation="comment",
    )


def test_put_then_get_roundtrips(tmp_path):
    r = _rev()
    put("the diff", r, model="claude-opus-4-7", cache_dir=tmp_path)
    back = get("the diff", model="claude-opus-4-7", cache_dir=tmp_path)
    assert back is not None
    assert back.summary == r.summary
    assert back.findings[0].severity == "low"


def test_miss_returns_none(tmp_path):
    assert get("nothing", model="claude-opus-4-7", cache_dir=tmp_path) is None


def test_model_swap_invalidates(tmp_path):
    put("same diff", _rev(), model="claude-opus-4-7", cache_dir=tmp_path)
    assert get("same diff", model="claude-opus-4-7", cache_dir=tmp_path) is not None
    assert get("same diff", model="claude-sonnet-4-6", cache_dir=tmp_path) is None


def test_extra_field_partitions_cache(tmp_path):
    put("same diff", _rev(), model="claude-opus-4-7", extra="ctx-A", cache_dir=tmp_path)
    assert get("same diff", model="claude-opus-4-7", extra="ctx-A", cache_dir=tmp_path) is not None
    assert get("same diff", model="claude-opus-4-7", extra="ctx-B", cache_dir=tmp_path) is None


def test_corrupt_entry_silently_evicted(tmp_path):
    key_file = tmp_path / f"{_key('d', model='m')}.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    key_file.write_text("this isn't json", encoding="utf-8")
    assert get("d", model="m", cache_dir=tmp_path) is None
    assert not key_file.exists()


def test_clear_reports_count(tmp_path):
    for i in range(3):
        put(f"diff-{i}", _rev(), model="m", cache_dir=tmp_path)
    assert clear(tmp_path) == 3
    assert clear(tmp_path) == 0
