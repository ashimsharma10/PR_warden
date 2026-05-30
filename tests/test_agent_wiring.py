"""Tests for the agent's integration into the webhook pipeline (issue #6).

Focus on the gating and graceful-degradation seams in `_maybe_run_agent` — the
agent is additive and must never break the base pipeline, so every failure mode
must return None rather than raise.
"""

from __future__ import annotations

import asyncio

import pr_warden.main as main
from pr_warden.agent.schemas import AgentResult, DoneInput
from pr_warden.config import Settings
from pr_warden.github.schemas import (
    GitHubUser,
    Installation,
    PullRequest,
    PullRequestEvent,
    Ref,
    Repository,
)


# ── config flag ──────────────────────────────────────────────────────────────

def test_agent_enabled_for_empty_allowlist():
    s = Settings(agent_review_repos="")
    assert not s.agent_enabled_for("acme/widgets")


def test_agent_enabled_for_allowlisted():
    s = Settings(agent_review_repos="acme/widgets, other/repo")
    assert s.agent_enabled_for("acme/widgets")
    assert s.agent_enabled_for("other/repo")     # whitespace tolerated
    assert not s.agent_enabled_for("acme/other")


# ── _maybe_run_agent gating ──────────────────────────────────────────────────

def _event() -> PullRequestEvent:
    pr = PullRequest(
        number=7, title="fix: x", body="b", state="open", draft=False,
        head=Ref(ref="fix/x", sha="head123"), base=Ref(ref="main", sha="base000"),
        user=GitHubUser(login="dev", id=1),
    )
    return PullRequestEvent(
        action="opened", number=7, pull_request=pr,
        repository=Repository(id=1, full_name="acme/widgets", owner=GitHubUser(login="acme", id=2)),
        sender=GitHubUser(login="dev", id=1),
        installation=Installation(id=99),
    )


def _agent_result() -> AgentResult:
    return AgentResult(
        assessment=DoneInput(summary="looks fine", intent_matches_diff=True, confidence=0.7),
        cost_usd=0.02,
        stopped_for="done",
    )


def _enable(monkeypatch, **overrides):
    monkeypatch.setattr(main.settings, "agent_review_repos", "acme/widgets")
    monkeypatch.setattr(main.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(main.settings, "daily_cost_limit_usd", 5.0)
    for k, v in overrides.items():
        monkeypatch.setattr(main.settings, k, v)


async def test_maybe_run_agent_disabled_repo(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_review_repos", "")  # not allowlisted
    res = await main._maybe_run_agent("tok", "acme/widgets", _event(), [])
    assert res is None


async def test_maybe_run_agent_no_api_key(monkeypatch):
    _enable(monkeypatch, anthropic_api_key="")
    res = await main._maybe_run_agent("tok", "acme/widgets", _event(), [])
    assert res is None


async def test_maybe_run_agent_over_daily_budget(monkeypatch):
    _enable(monkeypatch)

    async def spent():
        return 99.0

    monkeypatch.setattr(main, "_today_agent_cost", spent)
    res = await main._maybe_run_agent("tok", "acme/widgets", _event(), [])
    assert res is None


async def test_maybe_run_agent_happy_path(monkeypatch):
    _enable(monkeypatch)

    async def spent():
        return 0.0

    async def fake_run_agent(ctx, *, api_key, model):
        assert ctx.repo == "acme/widgets" and ctx.pr.number == 7
        return _agent_result()

    monkeypatch.setattr(main, "_today_agent_cost", spent)
    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    res = await main._maybe_run_agent("tok", "acme/widgets", _event(), [])
    assert res is not None and res.assessment.summary == "looks fine"


async def test_maybe_run_agent_timeout_returns_none(monkeypatch):
    _enable(monkeypatch, agent_timeout_s=0.01)

    async def spent():
        return 0.0

    async def slow(ctx, *, api_key, model):
        await asyncio.sleep(1)

    monkeypatch.setattr(main, "_today_agent_cost", spent)
    monkeypatch.setattr(main, "run_agent", slow)
    res = await main._maybe_run_agent("tok", "acme/widgets", _event(), [])
    assert res is None


async def test_maybe_run_agent_crash_returns_none(monkeypatch):
    _enable(monkeypatch)

    async def spent():
        return 0.0

    async def boom(ctx, *, api_key, model):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(main, "_today_agent_cost", spent)
    monkeypatch.setattr(main, "run_agent", boom)
    res = await main._maybe_run_agent("tok", "acme/widgets", _event(), [])
    assert res is None


# ── per-PR comment serialization (race fix) ───────────────────────────────────

def test_pr_comment_lock_is_per_pr():
    a = main._pr_comment_lock("acme/widgets", 7)
    assert main._pr_comment_lock("acme/widgets", 7) is a       # same PR → same lock
    assert main._pr_comment_lock("acme/widgets", 8) is not a   # different PR → different lock


async def test_handle_pr_event_serializes_concurrent_comment_creation(monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from pr_warden import models
    from pr_warden.checks import CheckContext

    # Real file-backed sqlite so a commit in one session is visible to the other.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    monkeypatch.setattr(main, "async_session_factory", async_sessionmaker(engine, expire_on_commit=False))

    async def fake_token(installation_id):
        return "tok"

    async def fake_cfg(token, repo):
        return main.DEFAULT_CONFIG

    async def fake_ctx(token, repo, event, config):
        return CheckContext(
            pr=event.pull_request, files=[], commits=[], config=config,
            repo_tree=[], codeowners_raw=None, gitleaks_findings=[],
        )

    async def no_agent(*a, **k):
        return None

    monkeypatch.setattr(main.auth, "get_installation_token", fake_token)
    monkeypatch.setattr(main, "_load_repo_config", fake_cfg)
    monkeypatch.setattr(main, "_build_check_context", fake_ctx)
    monkeypatch.setattr(main, "run_checks", lambda ctx: [])
    monkeypatch.setattr(main, "build_comment", lambda results, agent=None: "body")
    monkeypatch.setattr(main, "_maybe_run_agent", no_agent)

    created, updated = [], []

    async def fake_create(token, repo, pr_number, body):
        await asyncio.sleep(0)  # force the two tasks to interleave at this await
        created.append(pr_number)
        return 1000 + len(created)

    async def fake_update(token, repo, comment_id, body):
        updated.append(comment_id)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main.client, "create_comment", fake_create)
    monkeypatch.setattr(main.client, "update_comment", fake_update)
    monkeypatch.setattr(main.client, "add_label", noop)
    monkeypatch.setattr(main.client, "remove_label", noop)

    ev = _event()  # same PR for both
    await asyncio.gather(
        main._handle_pr_event(ev, "trace-1"),
        main._handle_pr_event(ev, "trace-2"),
    )
    # Without the per-PR lock both runs create a comment; with it, one creates
    # and the second sees the committed comment_id and updates.
    assert len(created) == 1
    assert len(updated) == 1

    await engine.dispose()
