"""Load and apply repo-specific review config from `.claude-review.yml`.

A lot of noise in generated reviews comes from flagging stuff the repo's
maintainers already decided isn't a problem: style in generated files, missing
tests in scripts/, docstrings in private helpers. A per-repo config lets the
maintainers write those conventions down once instead of repeating them in
every PR's instructions.

Shape:

    # .claude-review.yml
    model: claude-sonnet-4-6
    max_input_chars: 250000

    ignore_paths:
      - "docs/**"
      - "**/_generated/*"
      - "scripts/**"

    ignore_categories:  [style, docs]

    extra_instructions: |
      This is a pharma codebase. Never flag NDC-shaped identifiers
      (strings of digits + dashes like 12345-1234-12) as "magic numbers" —
      those are National Drug Codes.

All keys are optional. Unknown keys are ignored (for forward compat).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from claude_pr_reviewer.models import Finding

DEFAULT_FILENAME = ".claude-review.yml"


@dataclass
class RepoConfig:
    model: str | None = None
    max_input_chars: int | None = None
    ignore_paths: list[str] = field(default_factory=list)
    ignore_categories: list[str] = field(default_factory=list)
    extra_instructions: str = ""

    def is_empty(self) -> bool:
        return not (
            self.model
            or self.max_input_chars
            or self.ignore_paths
            or self.ignore_categories
            or self.extra_instructions.strip()
        )


def find_config(start: Path) -> Path | None:
    """Look for `.claude-review.yml` in `start` or any parent directory."""
    start = Path(start).resolve()
    for candidate in [start] + list(start.parents):
        path = candidate / DEFAULT_FILENAME
        if path.is_file():
            return path
    return None


def load_config(path: Path) -> RepoConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")

    def _list(key: str) -> list[str]:
        v = data.get(key, []) or []
        if not isinstance(v, list):
            raise ValueError(f"{path}: `{key}` must be a list, got {type(v).__name__}")
        return [str(x) for x in v]

    return RepoConfig(
        model=data.get("model") or None,
        max_input_chars=int(data["max_input_chars"]) if data.get("max_input_chars") else None,
        ignore_paths=_list("ignore_paths"),
        ignore_categories=_list("ignore_categories"),
        extra_instructions=str(data.get("extra_instructions") or "").strip(),
    )


def path_matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def filter_findings(
    findings: list[Finding], cfg: RepoConfig
) -> tuple[list[Finding], list[Finding]]:
    """Return (kept, dropped) based on `ignore_paths` + `ignore_categories`."""
    if not cfg.ignore_paths and not cfg.ignore_categories:
        return list(findings), []

    kept: list[Finding] = []
    dropped: list[Finding] = []
    for f in findings:
        if cfg.ignore_categories and f.category in cfg.ignore_categories:
            dropped.append(f)
            continue
        if cfg.ignore_paths and path_matches_any(f.file_path, cfg.ignore_paths):
            dropped.append(f)
            continue
        kept.append(f)
    return kept, dropped
