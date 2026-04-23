"""Post a structured review as an inline GitHub PR review via the `gh` CLI.

Builds the payload for `POST /repos/{owner}/{repo}/pulls/{n}/reviews`, then
pipes it to `gh api ... --input -`. One review, many inline comments (one per
finding that has a line number) — this is what services like CodeRabbit post.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

from claude_pr_reviewer.models import Finding, Review
from claude_pr_reviewer.render import SEVERITY_EMOJI

# `gh pr view` returns a URL of the form https://github.com/OWNER/REPO/pull/123
_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+")

# Typed callable for injecting a subprocess runner in tests.
Runner = Callable[..., subprocess.CompletedProcess]


def _default_runner(*args, **kwargs):  # type: ignore[no-untyped-def]
    return subprocess.run(*args, **kwargs)


def resolve_pr(pr_ref: str, *, runner: Runner = _default_runner) -> tuple[str, str, int]:
    """Resolve any PR reference (`123`, a URL, `owner/repo#123`) to (owner, repo, number)."""
    proc = runner(
        ["gh", "pr", "view", pr_ref, "--json", "url,number"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    match = _PR_URL_RE.match(data["url"])
    if not match:
        raise RuntimeError(f"Unexpected PR URL from gh: {data['url']!r}")
    return match.group(1), match.group(2), int(data["number"])


def _format_comment_body(f: Finding) -> str:
    emoji = SEVERITY_EMOJI[f.severity]
    parts = [f"{emoji} **[{f.severity}] {f.category}: {f.title}**", "", f.description]
    if f.suggested_fix.strip():
        parts.extend(["", "**Suggested fix:**", "", f.suggested_fix])
    return "\n".join(parts)


def build_review_payload(review: Review) -> dict:
    """Turn a `Review` into the JSON body for the GitHub reviews API."""
    event = {
        "approve": "APPROVE",
        "request-changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }[review.overall_recommendation]

    comments = []
    dropped_file_level: list[Finding] = []
    for f in review.findings:
        if f.start_line <= 0:
            # File-level findings can't be inline comments. We'll fold them into
            # the review body at the end so the author still sees them.
            dropped_file_level.append(f)
            continue
        comment = {
            "path": f.file_path,
            "line": f.end_line if f.end_line >= f.start_line else f.start_line,
            "side": "RIGHT",
            "body": _format_comment_body(f),
        }
        # Multi-line comment requires start_line + start_side.
        if f.end_line > f.start_line:
            comment["start_line"] = f.start_line
            comment["start_side"] = "RIGHT"
        comments.append(comment)

    body_parts = [review.summary]
    if dropped_file_level:
        body_parts.append("\n### File-level notes")
        for f in dropped_file_level:
            body_parts.append(f"- {_format_comment_body(f)}")
    body = "\n\n".join(body_parts)

    return {"event": event, "body": body, "comments": comments}


def post_review(
    owner: str,
    repo: str,
    pr_number: int,
    review: Review,
    *,
    runner: Runner = _default_runner,
) -> dict:
    """POST the review via `gh api`. Returns the parsed JSON response."""
    payload = build_review_payload(review)
    proc = runner(
        [
            "gh",
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            "--method",
            "POST",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Empty body on some approve events — return an empty dict rather than crashing.
        return {}
