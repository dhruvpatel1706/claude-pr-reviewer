"""Parse unified-diff text into a structured representation.

We keep raw diff text for the model to reason over (models are good at reading
unified diffs), but also compute per-file stats for quick summaries and for
truncation decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from unidiff import PatchSet


@dataclass
class FileDiff:
    path: str
    additions: int
    deletions: int
    raw: str  # per-file unified-diff text, including its @@ hunks


@dataclass
class ParsedDiff:
    files: list[FileDiff]

    @property
    def total_additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    @property
    def total_chars(self) -> int:
        return sum(len(f.raw) for f in self.files)


def parse_diff(diff_text: str) -> ParsedDiff:
    """Parse unified-diff text into per-file records.

    Raises no exception on empty input — returns an empty ParsedDiff.
    """
    if not diff_text.strip():
        return ParsedDiff(files=[])

    patches = PatchSet.from_string(diff_text)
    files: list[FileDiff] = []
    for patch in patches:
        path = patch.target_file or patch.source_file or "(unknown)"
        if path.startswith("b/"):
            path = path[2:]
        elif path.startswith("a/"):
            path = path[2:]
        files.append(
            FileDiff(
                path=path,
                additions=patch.added,
                deletions=patch.removed,
                raw=str(patch),
            )
        )
    return ParsedDiff(files=files)


def fit_into_budget(diff: ParsedDiff, max_chars: int) -> tuple[str, list[str]]:
    """Return (diff_text, skipped_files). Files are added in-order until the budget fills.

    Large files that would blow the budget alone are skipped with a note.
    """
    out: list[str] = []
    skipped: list[str] = []
    used = 0
    for f in diff.files:
        if len(f.raw) > max_chars:
            skipped.append(f"{f.path} (single file exceeds budget)")
            continue
        if used + len(f.raw) > max_chars:
            skipped.append(f.path)
            continue
        out.append(f.raw)
        used += len(f.raw)
    return "\n".join(out), skipped
