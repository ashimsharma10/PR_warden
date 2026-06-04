"""Tests for proactive author context injection into the agent prompt."""
import pytest
import respx
from httpx import Response

from pr_warden.agent.context import PRContext
from pr_warden.agent.prompts import render_initial_user_message
from pr_warden.github.schemas import GitHubUser, PullRequest, Ref


def _pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=7,
        title="feat: add OAuth login",
        body="Adds OAuth via GitHub.",
        state="open",
        draft=False,
        head=Ref(ref="feat/oauth", sha="abc123"),
        base=Ref(ref="main", sha="000000"),
        user=GitHubUser(login="alice", id=1),
        changed_files=2,
        additions=50,
        deletions=10,
    )
    defaults.update(kwargs)
    return PullRequest(**defaults)


def _ctx(author_context: str = "", **pr_kwargs) -> PRContext:
    return PRContext(
        token="tok",
        repo="acme/widgets",
        pr=_pr(**pr_kwargs),
        files=[],
        author_context=author_context,
    )


# ── Prompt rendering ─────────────────────────────────────────────────────────


def test_author_context_renders_in_prompt():
    """When author_context is set, it appears under '## Author context'."""
    msg = render_initial_user_message(
        _ctx(author_context="5 prior PR(s) on this repo, 4 merged.\nListed in CODEOWNERS for this repo.")
    )
    assert "## Author context" in msg
    assert "5 prior PR(s)" in msg
    assert "CODEOWNERS" in msg
    # Should NOT have the plain "## Author" heading
    assert "## Author\n" not in msg


def test_no_author_context_renders_plain_author():
    """When author_context is empty, falls back to plain '## Author'."""
    msg = render_initial_user_message(_ctx(author_context=""))
    assert "## Author\n" in msg
    assert "## Author context" not in msg


def test_first_time_contributor_context():
    """First-time contributors get the right label."""
    msg = render_initial_user_message(
        _ctx(author_context="First-time contributor to this repo — no prior PRs.")
    )
    assert "First-time contributor" in msg


# ── _build_author_context function ───────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_build_author_context_regular_contributor():
    """An author with prior merged PRs gets a summary."""
    from pr_warden.main import _build_author_context
    from pr_warden.github.schemas import (
        GitHubUser, Installation, PullRequest, PullRequestEvent, Ref, Repository,
    )

    event = PullRequestEvent(
        action="opened",
        number=10,
        pull_request=PullRequest(
            number=10, title="feat: X", body="...", state="open",
            head=Ref(ref="feat/x", sha="aaa"), base=Ref(ref="main", sha="bbb"),
            user=GitHubUser(login="alice", id=1),
        ),
        repository=Repository(id=1, full_name="acme/widgets", owner=GitHubUser(login="acme", id=2)),
        sender=GitHubUser(login="alice", id=1),
        installation=Installation(id=99),
    )

    respx.get("https://api.github.com/search/issues").mock(
        return_value=Response(200, json={
            "items": [
                {"number": 10, "title": "feat: X", "state": "open",
                 "pull_request": {"merged_at": None}, "created_at": "2026-06-01"},
                {"number": 8, "title": "fix: Y", "state": "closed",
                 "pull_request": {"merged_at": "2026-05-20T00:00:00Z"}, "created_at": "2026-05-20"},
                {"number": 5, "title": "chore: Z", "state": "closed",
                 "pull_request": {"merged_at": "2026-04-10T00:00:00Z"}, "created_at": "2026-04-10"},
                {"number": 3, "title": "docs: W", "state": "closed",
                 "pull_request": {"merged_at": None}, "created_at": "2026-03-01"},
            ],
        })
    )

    ctx = await _build_author_context("tok", "acme/widgets", event)
    assert "3 prior PR(s)" in ctx
    assert "2 merged" in ctx


@pytest.mark.asyncio
@respx.mock
async def test_build_author_context_first_timer():
    """An author with no prior PRs is labeled as first-time."""
    from pr_warden.main import _build_author_context
    from pr_warden.github.schemas import (
        GitHubUser, Installation, PullRequest, PullRequestEvent, Ref, Repository,
    )

    event = PullRequestEvent(
        action="opened",
        number=1,
        pull_request=PullRequest(
            number=1, title="feat: first", body="...", state="open",
            head=Ref(ref="feat/first", sha="aaa"), base=Ref(ref="main", sha="bbb"),
            user=GitHubUser(login="newbie", id=99),
        ),
        repository=Repository(id=1, full_name="acme/widgets", owner=GitHubUser(login="acme", id=2)),
        sender=GitHubUser(login="newbie", id=99),
        installation=Installation(id=99),
    )

    respx.get("https://api.github.com/search/issues").mock(
        return_value=Response(200, json={
            "items": [
                {"number": 1, "title": "feat: first", "state": "open",
                 "pull_request": {"merged_at": None}, "created_at": "2026-06-01"},
            ],
        })
    )

    ctx = await _build_author_context("tok", "acme/widgets", event)
    assert "First-time contributor" in ctx


@pytest.mark.asyncio
@respx.mock
async def test_build_author_context_codeowner():
    """An author listed in CODEOWNERS gets the flag."""
    from pr_warden.main import _build_author_context
    from pr_warden.github.schemas import (
        GitHubUser, Installation, PullRequest, PullRequestEvent, Ref, Repository,
    )

    event = PullRequestEvent(
        action="opened",
        number=5,
        pull_request=PullRequest(
            number=5, title="fix: auth", body="...", state="open",
            head=Ref(ref="fix/auth", sha="aaa"), base=Ref(ref="main", sha="bbb"),
            user=GitHubUser(login="alice", id=1),
        ),
        repository=Repository(id=1, full_name="acme/widgets", owner=GitHubUser(login="acme", id=2)),
        sender=GitHubUser(login="alice", id=1),
        installation=Installation(id=99),
    )

    respx.get("https://api.github.com/search/issues").mock(
        return_value=Response(200, json={
            "items": [
                {"number": 5, "title": "fix: auth", "state": "open",
                 "pull_request": {"merged_at": None}, "created_at": "2026-06-01"},
                {"number": 2, "title": "feat: init", "state": "closed",
                 "pull_request": {"merged_at": "2026-01-01T00:00:00Z"}, "created_at": "2026-01-01"},
            ],
        })
    )

    codeowners = "* @acme/core\nsrc/auth/ @alice @bob\n"
    ctx = await _build_author_context("tok", "acme/widgets", event, codeowners_raw=codeowners)
    assert "CODEOWNERS" in ctx
    assert "1 prior PR(s)" in ctx
    assert "1 merged" in ctx


@pytest.mark.asyncio
@respx.mock
async def test_build_author_context_api_failure_degrades():
    """If the GitHub API call fails, return empty string (never break the pipeline)."""
    from pr_warden.main import _build_author_context
    from pr_warden.github.schemas import (
        GitHubUser, Installation, PullRequest, PullRequestEvent, Ref, Repository,
    )

    event = PullRequestEvent(
        action="opened",
        number=1,
        pull_request=PullRequest(
            number=1, title="feat: X", body="...", state="open",
            head=Ref(ref="feat/x", sha="aaa"), base=Ref(ref="main", sha="bbb"),
            user=GitHubUser(login="alice", id=1),
        ),
        repository=Repository(id=1, full_name="acme/widgets", owner=GitHubUser(login="acme", id=2)),
        sender=GitHubUser(login="alice", id=1),
        installation=Installation(id=99),
    )

    respx.get("https://api.github.com/search/issues").mock(
        return_value=Response(500)
    )

    ctx = await _build_author_context("tok", "acme/widgets", event)
    assert ctx == ""
