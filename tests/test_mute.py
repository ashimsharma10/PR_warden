"""Tests for the `/warden mute` slash command: authorization, persistence, and
the review pipeline's mute-skip. The webhook is additive — an unauthorized or
malformed command must be a silent no-op, never an error.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pr_warden.main as main
from pr_warden import models
from pr_warden.checks import CheckContext
from pr_warden.github.schemas import (
    GitHubUser,
    Installation,
    IssueComment,
    IssueCommentEvent,
    IssueRef,
    PullRequest,
    PullRequestEvent,
    Ref,
    Repository,
)


async def _setup_db(monkeypatch, tmp_path) -> object:
    """File-backed sqlite with the schema created, wired into main's session
    factory. Returns the engine so the caller can dispose it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/mute.db")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    monkeypatch.setattr(
        main, "async_session_factory", async_sessionmaker(engine, expire_on_commit=False)
    )
    return engine


def _comment_event(
    *, body="/warden mute", actor="maintainer", pr_author="contributor",
    action="created", is_pr=True,
) -> IssueCommentEvent:
    return IssueCommentEvent(
        action=action,
        comment=IssueComment(id=555, body=body, user=GitHubUser(login=actor, id=1)),
        issue=IssueRef(
            number=7,
            user=GitHubUser(login=pr_author, id=2),
            pull_request={"url": "x"} if is_pr else None,
        ),
        repository=Repository(
            id=1, full_name="acme/widgets", owner=GitHubUser(login="acme", id=3)
        ),
        sender=GitHubUser(login=actor, id=1),
        installation=Installation(id=99),
    )


# ── authorization ─────────────────────────────────────────────────────────────

async def test_pr_author_can_manage_without_api_call(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("should not check permission for the PR author")

    monkeypatch.setattr(main.client, "get_user_permission", boom)
    assert await main._can_manage("tok", "acme/widgets", "alice", "alice") is True


@pytest.mark.parametrize(
    "perm,expected",
    [("admin", True), ("maintain", True), ("write", True), ("read", False), ("none", False)],
)
async def test_collaborator_permission_gates_non_author(monkeypatch, perm, expected):
    async def fake_perm(token, repo, username):
        return perm

    monkeypatch.setattr(main.client, "get_user_permission", fake_perm)
    assert await main._can_manage("tok", "acme/widgets", "bob", "alice") is expected


# ── persistence round-trip ──────────────────────────────────────────────────

async def test_set_and_read_mute(monkeypatch, tmp_path):
    engine = await _setup_db(monkeypatch, tmp_path)
    try:
        assert await main._is_muted(99, 7) is False              # no row yet
        await main._set_mute(99, "acme/widgets", 7, muted=True, actor="alice")
        assert await main._is_muted(99, 7) is True
        await main._set_mute(99, "acme/widgets", 7, muted=False, actor="alice")
        assert await main._is_muted(99, 7) is False              # unmute is reversible
        # A second mute reuses the same row (unique on repo_id+pr_number).
        await main._set_mute(99, "acme/widgets", 7, muted=True, actor="bob")
        async with main.async_session_factory() as session:
            rows = (await session.scalars(select(models.PRMute))).all()
        assert len(rows) == 1 and rows[0].muted_by == "bob"
    finally:
        await engine.dispose()


# ── command handler ───────────────────────────────────────────────────────────

async def test_handle_command_mute_authorized(monkeypatch, tmp_path):
    engine = await _setup_db(monkeypatch, tmp_path)
    try:
        reactions = []

        async def fake_token(installation_id):
            return "tok"

        async def fake_perm(token, repo, username):
            return "write"

        async def fake_react(token, repo, comment_id, content="+1"):
            reactions.append((comment_id, content))

        monkeypatch.setattr(main.auth, "get_installation_token", fake_token)
        monkeypatch.setattr(main.client, "get_user_permission", fake_perm)
        monkeypatch.setattr(main.client, "add_reaction", fake_react)

        await main._handle_command(_comment_event(), "mute", "trace")

        assert await main._is_muted(99, 7) is True
        assert reactions == [(555, "+1")]                        # acknowledged
    finally:
        await engine.dispose()


async def test_handle_command_unauthorized_is_noop(monkeypatch, tmp_path):
    engine = await _setup_db(monkeypatch, tmp_path)
    try:
        reacted = []

        async def fake_token(installation_id):
            return "tok"

        async def fake_perm(token, repo, username):
            return "read"  # not a write collaborator, and not the author

        async def fake_react(*a, **k):
            reacted.append(True)

        monkeypatch.setattr(main.auth, "get_installation_token", fake_token)
        monkeypatch.setattr(main.client, "get_user_permission", fake_perm)
        monkeypatch.setattr(main.client, "add_reaction", fake_react)

        await main._handle_command(_comment_event(actor="bob", pr_author="alice"), "mute", "trace")

        assert await main._is_muted(99, 7) is False              # not muted
        assert reacted == []                                     # no acknowledgement
    finally:
        await engine.dispose()


# ── pipeline mute-skip ────────────────────────────────────────────────────────

def _pr_event() -> PullRequestEvent:
    pr = PullRequest(
        number=7, title="fix: x", body="b", state="open", draft=False,
        head=Ref(ref="fix/x", sha="head123"), base=Ref(ref="main", sha="base000"),
        user=GitHubUser(login="dev", id=1),
    )
    return PullRequestEvent(
        action="synchronize", number=7, pull_request=pr,
        repository=Repository(id=1, full_name="acme/widgets", owner=GitHubUser(login="acme", id=2)),
        sender=GitHubUser(login="dev", id=1),
        installation=Installation(id=99),
    )


async def test_muted_pr_is_skipped_before_any_work(monkeypatch, tmp_path):
    engine = await _setup_db(monkeypatch, tmp_path)
    try:
        await main._set_mute(99, "acme/widgets", 7, muted=True, actor="alice")

        async def fake_token(installation_id):
            return "tok"

        monkeypatch.setattr(main.auth, "get_installation_token", fake_token)

        # If the pipeline didn't bail at the mute check, it would call these.
        async def boom_cfg(*a, **k):
            raise AssertionError("muted PR should not load config / run checks")

        async def boom_create(*a, **k):
            raise AssertionError("muted PR should not post a comment")

        monkeypatch.setattr(main, "_load_repo_config", boom_cfg)
        monkeypatch.setattr(main.client, "create_comment", boom_create)

        await main._handle_pr_event(_pr_event(), "trace")  # must return cleanly
    finally:
        await engine.dispose()


async def test_unmuted_pr_proceeds(monkeypatch, tmp_path):
    """The mute check defaults to 'not muted', so a normal PR runs the pipeline."""
    engine = await _setup_db(monkeypatch, tmp_path)
    try:
        reached_config = []

        async def fake_token(installation_id):
            return "tok"

        async def fake_cfg(token, repo):
            reached_config.append(repo)
            return main.DEFAULT_CONFIG

        async def fake_ctx(token, repo, event, config):
            return CheckContext(
                pr=event.pull_request, files=[], commits=[], config=config,
                repo_tree=[], codeowners_raw=None, gitleaks_findings=[],
            )

        async def no_agent(*a, **k):
            return None

        async def noop(*a, **k):
            return None

        async def fake_create(token, repo, pr_number, body):
            return 1234

        monkeypatch.setattr(main.auth, "get_installation_token", fake_token)
        monkeypatch.setattr(main, "_load_repo_config", fake_cfg)
        monkeypatch.setattr(main, "_build_check_context", fake_ctx)
        monkeypatch.setattr(main, "run_checks", lambda ctx: [])
        monkeypatch.setattr(main, "build_comment", lambda results, **kw: "body")
        monkeypatch.setattr(main, "_maybe_run_agent", no_agent)
        monkeypatch.setattr(main.client, "create_comment", fake_create)
        monkeypatch.setattr(main.client, "update_comment", noop)
        monkeypatch.setattr(main.client, "add_label", noop)
        monkeypatch.setattr(main.client, "remove_label", noop)

        await main._handle_pr_event(_pr_event(), "trace")
        assert reached_config == ["acme/widgets"]  # got past the mute gate
    finally:
        await engine.dispose()
