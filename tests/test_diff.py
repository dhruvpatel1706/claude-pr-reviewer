"""Tests for diff parsing and budget-fitting."""

from __future__ import annotations

from claude_pr_reviewer.diff import fit_into_budget, parse_diff

SAMPLE_DIFF = """diff --git a/hello.py b/hello.py
index 1234567..abcdefg 100644
--- a/hello.py
+++ b/hello.py
@@ -1,3 +1,4 @@
 def hello():
-    print("hi")
+    print("hello")
+    print("world")
     return 0
"""

MULTI_FILE_DIFF = """diff --git a/a.py b/a.py
index 111..222 100644
--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
 first
+added
diff --git a/b.py b/b.py
index 333..444 100644
--- a/b.py
+++ b/b.py
@@ -1 +1,2 @@
 second
+also added
"""


def test_parse_empty_diff():
    parsed = parse_diff("")
    assert parsed.files == []
    assert parsed.total_additions == 0


def test_parse_single_file():
    parsed = parse_diff(SAMPLE_DIFF)
    assert len(parsed.files) == 1
    f = parsed.files[0]
    assert f.path == "hello.py"
    assert f.additions == 2
    assert f.deletions == 1
    assert '+    print("hello")' in f.raw


def test_parse_multi_file():
    parsed = parse_diff(MULTI_FILE_DIFF)
    assert [f.path for f in parsed.files] == ["a.py", "b.py"]
    assert parsed.total_additions == 2
    assert parsed.total_deletions == 0


def test_fit_into_budget_all_fits():
    parsed = parse_diff(MULTI_FILE_DIFF)
    text, skipped = fit_into_budget(parsed, max_chars=10_000)
    assert skipped == []
    assert "a.py" in text
    assert "b.py" in text


def test_fit_into_budget_skips_oversized_single_file():
    parsed = parse_diff(SAMPLE_DIFF)
    text, skipped = fit_into_budget(parsed, max_chars=10)
    assert len(skipped) == 1
    assert "hello.py" in skipped[0]
    assert text == ""


def test_fit_into_budget_second_file_skipped():
    parsed = parse_diff(MULTI_FILE_DIFF)
    # Budget large enough that each file fits individually, but not both together.
    largest = max(len(f.raw) for f in parsed.files)
    text, skipped = fit_into_budget(parsed, max_chars=largest + 5)
    assert parsed.files[0].raw in text
    assert any("b.py" in s for s in skipped), skipped
