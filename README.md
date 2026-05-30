# PR Warden

A GitHub App that reviews pull requests automatically — cuts the noise so
maintainers only look at PRs worth looking at.

On every PR open / push / reopen (drafts skipped), it runs **18 deterministic
checks**, optionally runs a **tool-using LLM review agent**, posts a single
Markdown comment (edited in place on later events, never spammed), and labels
the PR `prwarden:clean` or `prwarden:needs-attention`.

## How it works

A `pull_request` webhook (`opened` / `synchronize` / `reopened`, non-draft)
kicks off a background task that:

1. Loads the repo's `.github/prwarden.yml` config (falls back to defaults).
2. Fetches the PR's files, commits, repo tree, and CODEOWNERS, then runs
   `gitleaks` over the diff additions.
3. Runs the deterministic checks.
4. Optionally runs the LLM agent (allowlisted repos only, under a daily cost
   ceiling).
5. Creates or edits one PR comment, swaps the label, and records the run in the
   database for `/stats` and cost accounting.

The label is `prwarden:clean` only if **every** check passes. The agent is
purely additive — if it's disabled, over budget, times out, or crashes, the
deterministic checks still post.

## The deterministic checks

**PR metadata** (`checks/no_diff.py`)
- `title_quality` — title is descriptive, not a placeholder
- `description` — description meets a minimum length (`min_chars`, default 20)
- `pr_size` — under file/line limits (`file_limit` 25, `line_limit` 500)
- `branch_naming` — branch uses an allowed prefix (`feat/`, `fix/`, …)
- `self_merge` — author isn't the sole reviewer/approver
- `ai_branch` — flags AI-tool-named branches (`claude-`, `codex-`, `cursor-`,
  `copilot-`, `devin-`)
- `ai_commit_footer` — flags `Co-Authored-By: Claude` (and similar) footers
- `boilerplate_description` — flags untouched PR-template boilerplate
- `description_vs_diff` — flags a thin description on a large diff

**Diff smell** (`checks/diff_aware.py`)
- `trivial_metadata_diff` — flags trivial metadata-only churn
- `no_tests` — source changed without any test file (supports `exempt_paths`)
- `test_assertions_weakened` — flags weakened/removed test assertions
- `empty_function_bodies` — flags `pass` / `return None` / `// TODO` stubs
- `dependency_only_churn` — flags lock-file-only diffs

**Impact & security** (`checks/impact.py`)
- `shared_code` — touches conventionally-shared dirs (`utils`, `lib`, `core`, …)
- `critical_path` — touches repo-declared high-risk globs (`auth/**`,
  `payments/**`, `migrations/**`, …); off until globs are configured
- `codeowners` — changed files have a required CODEOWNERS reviewer
- `secret_leak` — `gitleaks` scan of the diff additions surfaced a secret

### Per-repo config

Drop a `.github/prwarden.yml` to tune thresholds or disable individual checks.
No config file (or invalid YAML) = sensible defaults; unknown keys are ignored.

```yaml
# Example .github/prwarden.yml
checks:
  pr_size:
    file_limit: 100
  branch_naming:
    enabled: false
  no_tests:
    exempt_paths: ["docs/**", "scripts/one_off/**"]
  critical_path:
    globs: ["auth/**", "payments/**", "migrations/**", ".github/workflows/**"]
```

## The review agent

A tool-using agent (Claude Sonnet by default) that produces a structured
assessment of the PR. It is **off by default** and gated three ways: the repo
must be in the `AGENT_REVIEW_REPOS` allowlist, an Anthropic API key must be set,
and the day's recorded spend must be under `DAILY_COST_LIMIT_USD`. A wall-clock
timeout (default 90s) guards the webhook handler.

The loop (`agent/loop.py`) owns iteration and budget — the model only decides
"what next." It bounds runs to 12 tool calls / 15 iterations / 80k tokens, runs
the model's tool calls in parallel, terminates via an explicit structured `done`
tool, and force-finalizes on budget exhaustion so a run always returns something.
Tool errors come back as `tool_result`s rather than crashing the loop.

Tools available to the agent: `get_pr_diff`, `get_file`, `get_issue`,
`find_references`, `search_code`, `git_blame`, `get_repo_conventions`,
`get_author_history`, `check_security_patterns` (Semgrep), and `done`.

The `done` assessment (rendered as an "Agent Review" section in the PR comment)
carries: a summary, files touched, whether the diff matches the stated intent
(with a mismatch reason), notable findings, open questions, and a confidence
score.

## Endpoints

- `POST /webhook` — GitHub webhook receiver (HMAC signature verified)
- `GET /stats` — totals, failures-by-check, and recent runs (optionally
  bearer-token protected via `STATS_BEARER_TOKEN`)

## Run it

Requires Python 3.12+ and a Postgres database. `gitleaks` and `semgrep` should
be on `PATH` for the secret-leak check and the agent's security tool; both
degrade gracefully if missing.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # fill in GitHub App creds (and optional agent creds)
alembic upgrade head
uvicorn pr_warden.main:app --reload
```

Or via Docker: `docker compose up`.

Tests: `pytest -q`.

## Configuration (env)

| Variable | Purpose | Default |
| --- | --- | --- |
| `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY_PATH` / `GITHUB_WEBHOOK_SECRET` | GitHub App auth | — |
| `DATABASE_URL` | Postgres (async) DSN | local dev DSN |
| `ANTHROPIC_API_KEY` | Enables the agent / summarizer | unset |
| `AGENT_REVIEW_REPOS` | Comma-separated `owner/name` allowlist for the agent | empty (agent off) |
| `AGENT_MODEL` / `AGENT_TIMEOUT_S` | Agent model and wall-clock cap | `claude-sonnet-4-6` / 90s |
| `DAILY_COST_LIMIT_USD` | Daily agent spend ceiling | 5.00 |
| `SEMGREP_CONFIGS` / `SEMGREP_EXCLUDE_RULES` | Override the security ruleset | curated defaults |
| `STATS_BEARER_TOKEN` | Protect `/stats` | unset (open) |

## Design principles

- **High signal, low noise.** A reviewer that posts noise is worse than no
  reviewer. Default to comment-only; never `request_changes`, never block a merge.
- **One comment, edited.** Never thread, never reply, never react. A per-PR lock
  serializes create-or-update so concurrent events don't double-post.
- **Cheap by default.** Deterministic checks run first and always. The LLM runs
  only for allowlisted repos, gated by a daily cost ceiling.
- **Additive, never fatal.** The agent layer can fail in any way and the base
  pipeline still posts.
- **Maintainer always owns the decision.** The bot suggests; humans decide.
