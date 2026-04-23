"""Typer CLI for claude-pr-reviewer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console

from claude_pr_reviewer import __version__
from claude_pr_reviewer import cache as _cache
from claude_pr_reviewer.config import get_settings
from claude_pr_reviewer.github import post_review, resolve_pr
from claude_pr_reviewer.render import render_json, render_markdown
from claude_pr_reviewer.repo_config import RepoConfig, find_config, load_config
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


def _resolve_repo_config(explicit: str | None) -> RepoConfig | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            err.print(f"[red]Config file not found: {p}[/red]")
            raise typer.Exit(1)
        return load_config(p)
    found = find_config(Path(os.getcwd()))
    if found is None:
        return None
    try:
        cfg = load_config(found)
    except Exception as exc:
        err.print(f"[yellow](ignoring {found}: {exc})[/yellow]")
        return None
    if cfg.is_empty():
        return None
    console.print(f"[dim]Loaded repo config from {found}[/dim]")
    return cfg


@app.command("review-diff")
def review_diff_cmd(
    diff_file: str = typer.Option(
        "-", "--file", "-f", help="Path to a diff file, or '-' for stdin (default)."
    ),
    fmt: str = typer.Option("markdown", "--format", help="Output format: markdown or json."),
    per_file: bool = typer.Option(
        None,
        "--per-file/--no-per-file",
        help="Force per-file chunking (for huge PRs). Default: auto.",
    ),
    config: str = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to a .claude-review.yml. Default: search cwd and parents.",
    ),
    use_cache: bool = typer.Option(
        True, "--cache/--no-cache", help="Read/write the on-disk review cache (v0.5)."
    ),
) -> None:
    """Review a raw unified-diff (from stdin by default)."""
    diff_text = _read_diff(diff_file)
    if not diff_text.strip():
        err.print("[red]No diff content received.[/red]")
        raise typer.Exit(1)
    settings = get_settings()
    repo_cfg = _resolve_repo_config(config)
    try:
        review = review_diff_text(
            diff_text,
            settings,
            per_file=per_file,
            repo_config=repo_cfg,
            use_cache=use_cache,
        )
    except Exception as exc:
        err.print(f"[red]Review failed:[/red] {exc}")
        raise typer.Exit(1)
    _emit(review, fmt)


@app.command("review-local")
def review_local_cmd(
    base: str = typer.Option("HEAD", "--base", "-b", help="Base revision to diff against."),
    target: str = typer.Option("", "--target", "-t", help="Target revision. Empty = working tree."),
    fmt: str = typer.Option("markdown", "--format", help="Output format: markdown or json."),
    per_file: bool = typer.Option(None, "--per-file/--no-per-file"),
    config: str = typer.Option(None, "--config", "-c"),
    use_cache: bool = typer.Option(True, "--cache/--no-cache"),
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
    repo_cfg = _resolve_repo_config(config)
    try:
        review = review_diff_text(
            proc.stdout,
            settings,
            per_file=per_file,
            repo_config=repo_cfg,
            use_cache=use_cache,
        )
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
    per_file: bool = typer.Option(None, "--per-file/--no-per-file"),
    config: str = typer.Option(None, "--config", "-c"),
    use_cache: bool = typer.Option(True, "--cache/--no-cache"),
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
    repo_cfg = _resolve_repo_config(config)
    try:
        review = review_diff_text(
            proc.stdout,
            settings,
            per_file=per_file,
            repo_config=repo_cfg,
            use_cache=use_cache,
        )
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


@app.command("cache-clear")
def cache_clear_cmd() -> None:
    """Remove every cached review. Useful after a model swap or before benchmarking."""
    n = _cache.clear()
    console.print(f"[green]Cleared[/green] {n} cached review(s) from {_cache.DEFAULT_CACHE_DIR}")


@app.command("calibrate")
def calibrate_cmd(
    spec: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="Path to a calibration-set YAML. See the docs for the schema.",
    ),
    fmt: str = typer.Option("table", "--format", "-f", help="'table' or 'json'."),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print every unmatched reported finding + every missed expected entry.",
    ),
    model: str = typer.Option(
        None,
        "--model",
        "-m",
        help="Override settings.model for this run. Useful for A/B comparisons.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Skip the review cache. Always the right default for calibration "
        "because you want fresh measurements.",
    ),
) -> None:
    """Run the reviewer against a labelled calibration set and report precision/recall.

    The calibration YAML lists cases — each case has a diff file on disk plus
    expected findings (file, line range, category/severity constraints, title
    keywords). For each case we run the reviewer, match reported findings
    against the expected set, and compute TP / FP / FN.

    Output is a per-case table plus an overall summary. `--format json` emits
    everything as a machine-readable blob, suitable for piping into a trend
    tracker.
    """
    import json as _json

    from rich.table import Table

    from claude_pr_reviewer.calibrate import run as run_calibration

    settings = get_settings()
    if model:
        settings = settings.model_copy(update={"model": model})

    # Bypass cache by default — if I ran `calibrate` twice in a row with caching
    # on, I'd get identical scores even if I'd been tweaking the prompt. Not the
    # point of the harness.
    use_cache = not no_cache

    def _review(diff_text: str):
        return review_diff_text(diff_text, settings, use_cache=use_cache)

    def _on_case(name: str) -> None:
        if fmt != "json":
            console.print(f"[dim]reviewing[/dim] {name}")

    try:
        summary = run_calibration(spec, review_fn=_review, on_case_start=_on_case)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if fmt == "json":
        payload = {
            "cases": [
                {
                    "name": c.name,
                    "tp": c.true_positives,
                    "fp": c.false_positives,
                    "fn": c.false_negatives,
                    "precision": round(c.precision, 4),
                    "recall": round(c.recall, 4),
                    "f1": round(c.f1, 4),
                }
                for c in summary.cases
            ],
            "overall": {
                "tp": summary.total_tp,
                "fp": summary.total_fp,
                "fn": summary.total_fn,
                "precision": round(summary.precision, 4),
                "recall": round(summary.recall, 4),
                "f1": round(summary.f1, 4),
            },
        }
        print(_json.dumps(payload, indent=2))
        return

    table = Table(show_header=True, header_style="bold cyan", title="Calibration results")
    table.add_column("case")
    table.add_column("TP", justify="right")
    table.add_column("FP", justify="right")
    table.add_column("FN", justify="right")
    table.add_column("precision", justify="right")
    table.add_column("recall", justify="right")
    table.add_column("F1", justify="right")
    for c in summary.cases:
        table.add_row(
            c.name,
            str(c.true_positives),
            str(c.false_positives),
            str(c.false_negatives),
            f"{c.precision:.2f}",
            f"{c.recall:.2f}",
            f"{c.f1:.2f}",
        )
    console.print(table)
    console.print(
        f"\n[bold]Overall[/bold]: "
        f"precision [green]{summary.precision:.3f}[/green] · "
        f"recall [green]{summary.recall:.3f}[/green] · "
        f"F1 [green]{summary.f1:.3f}[/green]  "
        f"[dim](TP={summary.total_tp} FP={summary.total_fp} FN={summary.total_fn})[/dim]"
    )

    if verbose:
        for c in summary.cases:
            if not c.missed_expected and not c.unmatched_reported:
                continue
            console.print(f"\n[bold]{c.name}[/bold]")
            for exp in c.missed_expected:
                console.print(
                    f"  [red]miss[/red] {exp.file_path}:"
                    f"{exp.line_range[0]}-{exp.line_range[1]} "
                    f"(category={exp.category}, severity>={exp.min_severity})"
                )
            for fnd in c.unmatched_reported:
                console.print(
                    f"  [yellow]extra[/yellow] "
                    f"{fnd.file_path}:{fnd.start_line}-{fnd.end_line} "
                    f"[{fnd.severity}/{fnd.category}] {fnd.title}"
                )


if __name__ == "__main__":
    app()
