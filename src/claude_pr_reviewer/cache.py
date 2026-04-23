"""Simple on-disk cache of past reviews, keyed by a hash of the diff text.

The motivating case: CI runs the reviewer on every PR push. When a PR push
only changes one file out of ten, the other nine files' diffs are byte-for-byte
identical — no reason to re-bill them through Claude. Storing a per-file
cache (in per-file mode) means a typical PR-iteration push only pays for the
file(s) that actually changed.

For single-call mode we cache at the whole-diff level, which is less useful
(any one-line edit invalidates the whole thing) but free to include.

Cache dir defaults to `~/.claude-pr-reviewer/cache/`; override with
`CLAUDE_PR_CACHE_DIR`. No TTL — users can `rm -rf` the dir when they want to
reset. The model and settings.max_input_chars are baked into the key so a
model swap invalidates automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from claude_pr_reviewer.models import Review

DEFAULT_CACHE_DIR = Path(
    os.environ.get("CLAUDE_PR_CACHE_DIR", Path.home() / ".claude-pr-reviewer" / "cache")
)


def _key(diff_text: str, *, model: str, extra: str = "") -> str:
    """Stable cache key — any change to diff / model / extra invalidates."""
    h = hashlib.sha256()
    h.update(diff_text.encode("utf-8"))
    h.update(b"|model=")
    h.update(model.encode("utf-8"))
    if extra:
        h.update(b"|extra=")
        h.update(extra.encode("utf-8"))
    return h.hexdigest()[:16]


def _path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def get(
    diff_text: str,
    *,
    model: str,
    extra: str = "",
    cache_dir: Path | None = None,
) -> Review | None:
    """Return a cached `Review` or None if no hit."""
    d = cache_dir or DEFAULT_CACHE_DIR
    p = _path(d, _key(diff_text, model=model, extra=extra))
    if not p.is_file():
        return None
    try:
        return Review.model_validate_json(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        # Corrupt entry — remove it and miss.
        try:
            p.unlink()
        except OSError:
            pass
        return None


def put(
    diff_text: str,
    review: Review,
    *,
    model: str,
    extra: str = "",
    cache_dir: Path | None = None,
) -> Path:
    """Write a cache entry. Best-effort — OSErrors are swallowed by the caller."""
    d = cache_dir or DEFAULT_CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = _path(d, _key(diff_text, model=model, extra=extra))
    p.write_text(review.model_dump_json(indent=2), encoding="utf-8")
    return p


def clear(cache_dir: Path | None = None) -> int:
    """Remove every cached entry. Returns the number of files removed."""
    d = cache_dir or DEFAULT_CACHE_DIR
    if not d.exists():
        return 0
    removed = 0
    for p in d.glob("*.json"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            continue
    return removed
