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
