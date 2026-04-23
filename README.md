# claude-pr-reviewer

**Open-source Claude-powered PR reviewer. CLI and drop-in GitHub Action.**

What commercial services (Greptile, CodeRabbit, Codium) sell as a SaaS — wrapped into a single Python package you self-host. Reads a diff, sends it to Claude with a structured output schema, and returns ranked findings with severity, file/line locations, and suggested fixes. Works on local uncommitted changes, a revision range, or a live GitHub PR.

**v0.2 ships proper inline GitHub reviews.** `--post` no longer leaves one wall-of-text comment on the PR — each finding becomes a line-specific inline comment on the exact line it flags, wrapped in a single GitHub review that maps to `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` based on the highest-severity finding. Parity with what CodeRabbit and Greptile ship.

---

## What it produces

For each diff, you get a `Review`:

```yaml
summary: >
  Solid refactor overall. One critical issue: the new cache invalidation path
  reads `session_id` from the request body without validating it, which opens
  a cache poisoning vector. Two medium findings on test coverage.

overall_recommendation: request-changes   # or: approve | comment

findings:
  - category: security
    severity: critical
    file_path: src/api/cache.py
    start_line: 47
    end_line: 52
    title: "session_id from body is used as cache key without validation"
    description: >
      Any caller can set `session_id` to an arbitrary string in the POST body,
      and the result is used verbatim as the cache key. An attacker can set
      `session_id` to match another user's session key and force-serve their
      cached response to that user.
    suggested_fix: |
      ```python
      session_id = request.headers.get("X-Session-Id")
      if not session_id or not UUID_RE.match(session_id):
          raise HTTPException(400, "missing/invalid session")
      ```

  - category: tests
    severity: medium
    ...
```

You can render this as a markdown report (for PR comments or terminal) or raw JSON (for piping into other tooling).

## Install

```bash
pip install claude-pr-reviewer
```

Or for local development:

```bash
git clone https://github.com/dhruvpatel1706/claude-pr-reviewer.git
cd claude-pr-reviewer
pip install -e .
```

Python 3.10+.

## Configure

```bash
cp .env.example .env
# add ANTHROPIC_API_KEY
```

Get a key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

## Use

### Review a local diff

```bash
# Review uncommitted working-tree changes
claude-pr-reviewer review-local

# Review a revision range
claude-pr-reviewer review-local --base main --target HEAD

# Pipe an arbitrary diff
git diff main HEAD | claude-pr-reviewer review-diff

# JSON instead of markdown
claude-pr-reviewer review-local --format json > review.json
```

### Review a GitHub PR

Requires the [`gh` CLI](https://cli.github.com/) to be authenticated.

```bash
# By PR number (in the current repo)
claude-pr-reviewer review-pr 123

# By URL (any repo your gh has access to)
claude-pr-reviewer review-pr https://github.com/org/repo/pull/456

# Auto-post the review as a PR comment
claude-pr-reviewer review-pr 123 --post
```

### As a GitHub Action

Copy [examples/action.yml](examples/action.yml) to `.github/workflows/claude-review.yml` in your target repo, and set `ANTHROPIC_API_KEY` as a repository secret. Every PR (open, push, reopen) now gets a Claude review as a comment.

## How it works

1. **Parse** — `diff.py` uses `unidiff` to split the unified diff into per-file records (path, add/del counts, raw hunks).
2. **Fit** — large diffs are trimmed to stay under `MAX_INPUT_CHARS` (default 200K). Skipped files are reported up-front so the reviewer can note the incomplete coverage.
3. **Review** — `review.py` calls Claude with:
   - Adaptive thinking (the model decides how much reasoning a given diff needs)
   - A deliberately strict severity rubric in the system prompt (critical = ship blocker only)
   - Prompt caching on the system prompt — repeat reviews against the same repo reuse the cached prefix
   - Structured output via `client.messages.parse()` constrained to a Pydantic `Review` schema — no JSON parsing, no hallucinated fields
4. **Render** — `render.py` formats markdown (for PR comments) or JSON (for tooling). Findings are sorted by severity and file path.

## Design choices

- **Structured output, not free-form prose.** The `Finding` schema forces the model to separate severity from category from location. Without this, "reviews" turn into wall-of-text summaries that hide the one critical bug among the style nits.
- **Strict severity calibration.** The system prompt explicitly defines what each severity level means. `critical` is reserved for actual ship-blockers (correctness, security, data loss). `low` and `info` don't block merges — they're comments.
- **Consistency enforcement.** `overall_recommendation` is constrained to match the findings: `request-changes` requires at least one critical or high; `approve` means zero findings or only info-level.
- **Focus on the change, not pre-existing code.** The prompt explicitly tells the model not to flag untouched code, because otherwise every review bikeshed-bombs the author with unrelated nits.
- **Truncation transparency.** When diffs get trimmed, the reviewer is told which files were skipped so it can flag incomplete coverage in its summary.

## What it isn't

- ❌ Not a test runner. It won't run your code or detect runtime failures.
- ❌ Not a type checker. It won't catch everything mypy/pyright would.
- ❌ Not a ground-truth oracle. It augments human review; it doesn't replace it.
- ❌ Not a security scanner. Treat security findings as prompts to investigate, not as verdicts.

Pair it with your existing CI (tests, linters, type checkers). It's a reviewer, not a replacement for automation.

## Development

```bash
pip install -e ".[dev]"
pytest
black --check src tests
isort --check-only --profile black src tests
flake8 src tests --max-line-length=100 --ignore=E501,W503,E203
```

CI runs on Python 3.10 / 3.11 / 3.12.

## Inline review posting

`--post` now posts a proper GitHub review, not a top-level comment. Each finding:

1. With a line number becomes an **inline review comment** anchored to that exact line (multi-line findings use GitHub's `start_line`/`line` pair).
2. At file level (no line number) folds into the review body under a "File-level notes" section so nothing is lost.

The `overall_recommendation` on the `Review` object maps to a GitHub **review event**:

| Review field | GitHub event | When |
| --- | --- | --- |
| `approve` | `APPROVE` | No findings, or only `info` |
| `comment` | `COMMENT` | Only `medium` / `low` findings |
| `request-changes` | `REQUEST_CHANGES` | Any `critical` or `high` finding |

This is posted via `gh api POST /repos/{owner}/{repo}/pulls/{n}/reviews` — single atomic review, multiple inline comments.

## Per-file chunking (v0.3)

When a PR is too big to fit in a single Claude request, the reviewer auto-splits: one call per file, findings aggregated, `overall_recommendation` set to the strictest across all files. The single-file budget cap still applies — a single file too large to fit on its own is listed under "Skipped" in the summary.

```
# auto (default) — single call unless total diff > MAX_INPUT_CHARS
claude-pr-reviewer review-pr 1234

# force per-file even for small PRs (useful for debugging)
claude-pr-reviewer review-diff --per-file < huge.diff

# disable chunking entirely, accept truncation
claude-pr-reviewer review-diff --no-per-file < huge.diff
```

## Roadmap

- [x] **v0.2 — inline GitHub PR review comments (line-specific) via the reviews API**
- [x] **v0.3 — per-file chunking for very large PRs; reviews files independently, merges findings**
- [x] **v0.4 — `.claude-review.yml` config for per-repo path/category ignores + extra instructions**
- [ ] v0.5 — cache recent reviews by diff hash to avoid re-billing on PR pushes that only changed one file

### Repo config (v0.4)

Drop a `.claude-review.yml` at the repo root to shape reviews without repeating yourself:

```yaml
model: claude-sonnet-4-6        # optional override
max_input_chars: 250000         # optional override

ignore_paths:
  - "docs/**"
  - "**/_generated/*"
  - "scripts/**"

ignore_categories:              # any of: bug security performance style tests docs design
  - style
  - docs

extra_instructions: |
  This is a pharma codebase. Don't flag NDC-shaped identifiers
  (strings like 12345-1234-12) as "magic numbers" — those are
  National Drug Codes.
```

The config is auto-discovered by walking up from the current directory. Override with `--config path/to/file.yml`. `extra_instructions` is appended to the system prompt so Claude knows the repo's conventions before reviewing; `ignore_paths` and `ignore_categories` filter findings *after* Claude returns, and the summary notes how many were suppressed.

## License

MIT. See [LICENSE](LICENSE).
