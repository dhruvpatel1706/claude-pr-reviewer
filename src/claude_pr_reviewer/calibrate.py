"""Measure the reviewer against a ground-truth set of known issues.

Use case: I tweaked the system prompt, or lowered the model, or added a
repo config. Did that help or hurt? Without a calibration harness this
question becomes 'let me read 50 findings and squint'. With one, you
get precision / recall / F1 on a diff you've hand-labelled once.

**The spec format** (YAML, one case per diff):

```yaml
cases:
  - name: sql-injection-leak
    diff_file: diffs/sql.diff
    expected:
      - file_path: src/db.py
        line_range: [42, 42]          # NEW-file line range
        category: security            # optional, must match exactly
        min_severity: high            # optional, reported >= this
        any_keyword:                  # at least one must appear in title or description
          - sql injection
          - format string
```

**Matching rules** — an expected issue counts as "caught" (true positive) if at
least one reported finding:

1. Has the same `file_path` (diff-relative) AND
2. Has a `[start_line, end_line]` that overlaps with `line_range` (by ≥1 line
   on either side; a ±2-line slack is allowed to absorb small off-by-one
   ambiguity around hunk boundaries), AND
3. If `category` was set, matches exactly, AND
4. If `min_severity` was set, the reported severity is at least that level, AND
5. If `any_keyword` was set, at least one keyword substring-matches the title
   or description (case-insensitive).

Each expected entry matches at most one reported finding (the first matching
one wins). Reported findings not matched against any expected entry count as
false positives.

Precision = TP / (TP + FP); Recall = TP / (TP + FN); F1 = 2PR / (P+R).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import yaml

from claude_pr_reviewer.models import Finding, Review

# Severity ordering — info < low < medium < high < critical. min_severity in
# a calibration case means "at least this".
_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# How many lines of slack we allow around the expected line range. Without
# slack, a reported line-42 finding against an expected [42, 42] would miss
# if Claude rounded to line 43. 2 is generous enough to absorb small
# hunk-boundary ambiguity without letting whole-file noise through.
_LINE_SLACK = 2


@dataclass
class Expected:
    """One known issue in a calibration case."""

    file_path: str
    line_range: tuple[int, int]
    category: str | None = None
    min_severity: str | None = None
    any_keyword: Sequence[str] = field(default_factory=list)


@dataclass
class CalibrationCase:
    name: str
    diff_file: Path
    expected: list[Expected]


@dataclass
class CaseResult:
    name: str
    true_positives: int
    false_positives: int
    false_negatives: int
    # Reported findings that didn't match any expected. Useful when debugging.
    unmatched_reported: list[Finding]
    # Expected issues that no reported finding matched. The recall miss set.
    missed_expected: list[Expected]

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r) / (p + r) if (p + r) else 0.0


@dataclass
class CalibrationSummary:
    """Aggregated metrics across the full set."""

    cases: list[CaseResult]
    total_tp: int
    total_fp: int
    total_fn: int

    @property
    def precision(self) -> float:
        denom = self.total_tp + self.total_fp
        return self.total_tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.total_tp + self.total_fn
        return self.total_tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r) / (p + r) if (p + r) else 0.0


def load_set(spec_path: Path) -> list[CalibrationCase]:
    """Parse a calibration spec YAML. Relative diff paths resolve to spec_path's dir."""
    data = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or "cases" not in data:
        raise ValueError(f"{spec_path}: expected a mapping with a top-level `cases` list")
    raw_cases = data["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{spec_path}: `cases` must be a non-empty list")

    base_dir = spec_path.resolve().parent
    out: list[CalibrationCase] = []
    for i, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise ValueError(f"{spec_path}: cases[{i}] is not a mapping")
        try:
            name = str(raw["name"]).strip()
            diff_file_raw = str(raw["diff_file"])
            expected_raw = raw["expected"]
        except KeyError as exc:
            raise ValueError(f"{spec_path}: cases[{i}] missing required key {exc}") from exc

        diff_file = Path(diff_file_raw)
        if not diff_file.is_absolute():
            diff_file = base_dir / diff_file

        if not isinstance(expected_raw, list) or not expected_raw:
            raise ValueError(f"{spec_path}: cases[{i}].expected must be a non-empty list")

        expected = []
        for j, e in enumerate(expected_raw):
            if not isinstance(e, dict):
                raise ValueError(f"{spec_path}: cases[{i}].expected[{j}] is not a mapping")
            try:
                file_path = str(e["file_path"])
                lr = e["line_range"]
            except KeyError as exc:
                raise ValueError(f"{spec_path}: cases[{i}].expected[{j}] missing {exc}") from exc
            if not (isinstance(lr, list) and len(lr) == 2 and all(isinstance(x, int) for x in lr)):
                raise ValueError(
                    f"{spec_path}: cases[{i}].expected[{j}].line_range must be [start, end]"
                )
            expected.append(
                Expected(
                    file_path=file_path,
                    line_range=(int(lr[0]), int(lr[1])),
                    category=e.get("category"),
                    min_severity=e.get("min_severity"),
                    any_keyword=[str(k) for k in e.get("any_keyword", [])],
                )
            )

        out.append(CalibrationCase(name=name, diff_file=diff_file, expected=expected))
    return out


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int], *, slack: int = _LINE_SLACK) -> bool:
    a_lo, a_hi = min(a), max(a)
    b_lo, b_hi = min(b), max(b)
    return (a_lo - slack) <= b_hi and (b_lo - slack) <= a_hi


def _keyword_hit(f: Finding, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{f.title} {f.description}".lower()
    return any(k.lower() in haystack for k in keywords)


def matches(finding: Finding, expected: Expected) -> bool:
    """True if this reported finding satisfies every constraint of `expected`."""
    if finding.file_path != expected.file_path:
        return False
    if not _ranges_overlap((finding.start_line, finding.end_line), expected.line_range):
        return False
    if expected.category is not None and finding.category != expected.category:
        return False
    if expected.min_severity is not None:
        if _SEVERITY_RANK.get(finding.severity, -1) < _SEVERITY_RANK.get(expected.min_severity, 99):
            return False
    if not _keyword_hit(finding, expected.any_keyword):
        return False
    return True


def score_case(case: CalibrationCase, review: Review) -> CaseResult:
    """Score one case. Each expected matches at most one reported finding."""
    reported = list(review.findings)
    matched_reported_idx: set[int] = set()
    missed_expected: list[Expected] = []
    true_positives = 0

    for exp in case.expected:
        hit_idx = None
        for i, rep in enumerate(reported):
            if i in matched_reported_idx:
                continue
            if matches(rep, exp):
                hit_idx = i
                break
        if hit_idx is None:
            missed_expected.append(exp)
        else:
            matched_reported_idx.add(hit_idx)
            true_positives += 1

    unmatched_reported = [f for i, f in enumerate(reported) if i not in matched_reported_idx]
    false_positives = len(unmatched_reported)
    false_negatives = len(missed_expected)

    return CaseResult(
        name=case.name,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        unmatched_reported=unmatched_reported,
        missed_expected=missed_expected,
    )


def run(
    spec_path: Path,
    *,
    review_fn: Callable[[str], Review],
    on_case_start: Callable[[str], None] | None = None,
) -> CalibrationSummary:
    """Run the full calibration. `review_fn` takes diff text, returns a Review.

    Exists as an injectable seam so tests don't need a real Anthropic client.
    """
    cases = load_set(spec_path)
    case_results: list[CaseResult] = []
    total_tp = total_fp = total_fn = 0

    for case in cases:
        if on_case_start is not None:
            on_case_start(case.name)
        diff_text = case.diff_file.read_text(encoding="utf-8")
        review = review_fn(diff_text)
        result = score_case(case, review)
        case_results.append(result)
        total_tp += result.true_positives
        total_fp += result.false_positives
        total_fn += result.false_negatives

    return CalibrationSummary(
        cases=case_results,
        total_tp=total_tp,
        total_fp=total_fp,
        total_fn=total_fn,
    )
