"""Typer CLI for claude-pr-reviewer."""

from __future__ import annotations

import subprocess
import sys

import typer
from rich.console import Console

from claude_pr_reviewer import __version__
from claude_pr_reviewer.config import get_settings
from claude_pr_reviewer.github import post_review, resolve_pr
from claude_pr_reviewer.render import render_json, render_markdown
from claude_pr_reviewer.review import review_diff_text

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Automated PR review powered by Claude. Reads diffs, emits structured findings.",
)
console = Console()
err = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"claude-pr-reviewer {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True
    ),
) -> None:
    return


def _emit(review, fmt: str) -> None:  # type: ignore[no-untyped-def]
    if fmt == "json":
        print(render_json(review))
    else:
        print(render_markdown(review))


def _read_diff(diff_file: str | None) -> str:
    if diff_file == "-" or diff_file is None:
        # Read from stdin (empty if the user didn't pipe anything)
        return sys.stdin.read()
    with open(diff_file, "r", encoding="utf-8") as f:
        return f.read()


@app.command("review-diff")
def review_diff_cmd(
    diff_file: str = typer.Option(
        "-", "--file", "-f", help="Path to a diff file, or '-' for stdin (default)."
    ),
    fmt: str = typer.Option("markdown", "--format", help="Output format: markdown or json."),
) -> None:
    """Review a raw unified-diff (from stdin by default)."""
    diff_text = _read_diff(diff_file)
    if not diff_text.strip():
        err.print("[red]No diff content received.[/red]")
        raise typer.Exit(1)
    settings = get_settings()
    try:
        review = review_diff_text(diff_text, settings)
    except Exception as exc:
        err.print(f"[red]Review failed:[/red] {exc}")
        raise typer.Exit(1)
    _emit(review, fmt)


@app.command("review-local")
def review_local_cmd(
    base: str = typer.Option("HEAD", "--base", "-b", help="Base revision to diff against."),
    target: str = typer.Option("", "--target", "-t", help="Target revision. Empty = working tree."),
    fmt: str = typer.Option("markdown", "--format", help="Output format: markdown or json."),
) -> None:
    """Review the local working-tree diff (or two revisions) via `git diff`."""
    cmd = ["git", "diff", base]
    if target:
        cmd.append(target)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        err.print("[red]`git` not found on PATH.[/red]")
        raise typer.Exit(1)
    except subprocess.CalledProcessError as e:
        err.print(f"[red]git diff failed:[/red] {e.stderr}")
        raise typer.Exit(1)

    if not proc.stdout.strip():
        err.print("[yellow]No diff to review — the working tree matches the base.[/yellow]")
        raise typer.Exit(0)

    settings = get_settings()
    try:
        review = review_diff_text(proc.stdout, settings)
    except Exception as exc:
        err.print(f"[red]Review failed:[/red] {exc}")
        raise typer.Exit(1)
    _emit(review, fmt)


@app.command("review-pr")
def review_pr_cmd(
    pr: str = typer.Argument(..., help="PR number or URL (uses `gh pr diff`)."),
    fmt: str = typer.Option("markdown", "--format", help="Output format: markdown or json."),
    post: bool = typer.Option(
        False,
        "--post",
        help=(
            "Post the findings back to the PR as a proper inline GitHub review — "
            "each finding becomes a line-specific comment on the exact diff line "
            "(file-level findings fold into the review body). The overall "
            "recommendation maps to APPROVE / REQUEST_CHANGES / COMMENT events."
        ),
    ),
) -> None:
    """Review a GitHub PR via the `gh` CLI."""
    try:
        proc = subprocess.run(["gh", "pr", "diff", pr], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        err.print("[red]`gh` (GitHub CLI) not found. Install from https://cli.github.com/[/red]")
        raise typer.Exit(1)
    except subprocess.CalledProcessError as e:
        err.print(f"[red]gh pr diff failed:[/red] {e.stderr}")
        raise typer.Exit(1)

    settings = get_settings()
    try:
        review = review_diff_text(proc.stdout, settings)
    except Exception as exc:
        err.print(f"[red]Review failed:[/red] {exc}")
        raise typer.Exit(1)

    _emit(review, fmt)

    if post:
        try:
            owner, repo, pr_number = resolve_pr(pr)
        except subprocess.CalledProcessError as e:
            err.print(f"[red]Could not resolve PR:[/red] {e.stderr}")
            raise typer.Exit(1)
        try:
            post_review(owner, repo, pr_number, review)
        except subprocess.CalledProcessError as e:
            err.print(f"[red]gh api POST reviews failed:[/red] {e.stderr}")
            raise typer.Exit(1)
        inline_count = sum(1 for f in review.findings if f.start_line > 0)
        console.print(
            f"[green]Posted review to {owner}/{repo}#{pr_number}[/green] "
            f"({inline_count} inline comments, recommendation: {review.overall_recommendation})."
        )


if __name__ == "__main__":
    app()
