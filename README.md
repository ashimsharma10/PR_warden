# PR Warden

A GitHub App that reviews pull requests automatically — cuts the noise so
maintainers only look at PRs worth looking at.

On every PR open / push / reopen, it runs 13 deterministic checks, posts a
single Markdown comment with a pass/fail table (edited in place on later
events, not spammed), and labels the PR `prwarden:clean` or
`prwarden:needs-attention`.

The checks cover:

- **PR hygiene** — title quality, description length, PR size, branch naming, reviewer assignment
- **Anti-slop signals** — AI-named branches (`claude-/codex-/cursor-/copilot-`), `Co-Authored-By: Claude` footers, boilerplate template descriptions, description-too-thin-for-diff
- **Diff smell** — no test files for source changes, weakened test assertions, empty function bodies (`pass`, `return None`, `// TODO`), trivial doc-only diffs, lock-file-only churn

Each repo can drop a `.github/prwarden.yml` to tune thresholds or disable
individual checks. No config file = sensible defaults.

```yaml
# Example .github/prwarden.yml
checks:
  pr_size:
    file_limit: 100
  branch_naming:
    enabled: false
  no_tests:
    exempt_paths: ["docs/**", "scripts/one_off/**"]
```

## Run it

Requires Python 3.12+.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # fill in GitHub App creds
alembic upgrade head
uvicorn pr_warden.main:app --reload
```

Tests: `pytest -q` (95 passing).

## Roadmap

The deterministic checks are the cheap, fast pre-filter. The bigger
goal is an *agentic* reviewer that helps maintainers move faster on the PRs
that pass the filter.

**Phase 1 — CI integration.** Listen for `check_run` / `check_suite` webhook
events. When CI completes, fetch the conclusion via the Checks API and add a
`ci_passing` check. Surface failing test names in the PR comment so the
maintainer doesn't have to click into the CI tab.

**Phase 2 — LLM-based scope-creep detection.** A new `scope_match` check uses
Claude to parse the PR title + description into a structured intent, parse
the file list into structured actual changes, and flag files that don't match
the stated scope. ("Fix typo in README" + touches `src/auth.py` → flagged.)
The summarizer scaffolding in `src/pr_warden/summarizer/` exists for this.

**Phase 3 — LLM-based test coverage analysis.** Replace the current
heuristic (`no_tests` flags any source-without-test diff) with a smarter one:
identify which functions changed in each source file, then check whether any
test file in the diff actually exercises those functions.

**Phase 4 — Focused review summary.** Instead of a longer review, produce a
*shorter* one that points the maintainer at the 5% of the diff that matters:
risk level, the 3 files to read first, specific concerns with `file:line`
citations. Every claim must cite evidence.

**Phase 5 — Slash commands.** Maintainer overrides via PR comments:
`/prwarden recheck`, `/prwarden explain <check>`, `/prwarden ignore <check>`.
Lets a human dismiss a false positive in one comment instead of arguing with
the bot.

**Phase 6 — Decision output + auto-close policy.** For PRs that fail many
anti-slop checks at once (AI branch + boilerplate description + lock-file
churn + no tests = clear slop), auto-close with an explanatory comment.
Default for everything else stays `comment_only`; the bot never blocks merges
on its own.

**Phase 7 — Suppression learning.** Track when maintainers use
`/prwarden ignore` and which checks correlate with PR closures vs merges.
Use the data to tune thresholds per-repo over time.

## Design principles

- **High signal, low noise.** A reviewer that posts noise is worse than no reviewer. Default to `comment_only`, never `request_changes`.
- **Cite evidence.** Every claim links to `file:line`. No vague opinions.
- **One comment, edited.** Never thread, never reply, never react.
- **Maintainer always owns the decision.** The bot suggests; humans decide.
- **Cheap by default.** Deterministic checks run first. LLM only when it adds value, gated by a daily cost ceiling.
