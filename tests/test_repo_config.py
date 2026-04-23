"""Tests for .claude-review.yml loading and finding-filter logic."""

from __future__ import annotations

import pytest

from claude_pr_reviewer.models import Finding
from claude_pr_reviewer.repo_config import (
    DEFAULT_FILENAME,
    RepoConfig,
    filter_findings,
    find_config,
    load_config,
    path_matches_any,
)


def _f(path: str, category: str = "bug") -> Finding:
    return Finding(
        category=category,  # type: ignore[arg-type]
        severity="medium",
        file_path=path,
        start_line=1,
        end_line=1,
        title="t",
        description="d",
        suggested_fix="",
    )


def test_load_minimal_config(tmp_path):
    p = tmp_path / DEFAULT_FILENAME
    p.write_text("", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.is_empty()


def test_load_full_config(tmp_path):
    p = tmp_path / DEFAULT_FILENAME
    p.write_text(
        """
model: claude-sonnet-4-6
max_input_chars: 123456
ignore_paths:
  - "docs/**"
  - "**/_generated/*"
ignore_categories:
  - style
  - docs
extra_instructions: |
  Pharma repo.
""".strip(),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.max_input_chars == 123456
    assert cfg.ignore_paths == ["docs/**", "**/_generated/*"]
    assert cfg.ignore_categories == ["style", "docs"]
    assert "Pharma repo" in cfg.extra_instructions
    assert not cfg.is_empty()


def test_load_rejects_non_mapping(tmp_path):
    p = tmp_path / DEFAULT_FILENAME
    p.write_text("- not a mapping\n- just a list", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config(p)


def test_load_rejects_non_list_ignore(tmp_path):
    p = tmp_path / DEFAULT_FILENAME
    p.write_text("ignore_paths: not-a-list", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        load_config(p)


def test_find_config_walks_up(tmp_path):
    (tmp_path / DEFAULT_FILENAME).write_text("", encoding="utf-8")
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    found = find_config(sub)
    assert found is not None
    assert found.parent == tmp_path


def test_find_config_returns_none_when_missing(tmp_path):
    assert find_config(tmp_path) is None


def test_path_matches_any():
    assert path_matches_any("docs/index.md", ["docs/**"])
    assert path_matches_any("src/_generated/x.py", ["**/_generated/*"])
    assert not path_matches_any("src/app.py", ["docs/**", "**/_generated/*"])


def test_filter_drops_matching_paths_and_categories():
    findings = [
        _f("src/app.py", "bug"),
        _f("docs/index.md", "docs"),
        _f("src/app.py", "style"),
        _f("scripts/build.sh", "bug"),
    ]
    cfg = RepoConfig(
        ignore_paths=["docs/**", "scripts/**"],
        ignore_categories=["style"],
    )
    kept, dropped = filter_findings(findings, cfg)
    assert [f.file_path for f in kept] == ["src/app.py"]
    assert len(dropped) == 3


def test_filter_passthrough_when_empty_config():
    findings = [_f("a.py"), _f("b.py")]
    cfg = RepoConfig()
    kept, dropped = filter_findings(findings, cfg)
    assert kept == findings
    assert dropped == []
