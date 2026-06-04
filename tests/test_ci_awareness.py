"""Tests for CI awareness: feed CI status to agent as context, surface in comment."""
import pytest
import respx
from httpx import Response

from pr_warden.agent.context import PRContext
from pr_warden.agent.prompts import render_initial_user_message
from pr_warden.composer import build_comment
from pr_warden.checks.registry import CheckResult, Severity
from pr_warden.github.schemas import GitHubUser, PullRequest, Ref
from pr_warden.main import _format_ci_context


def _passing_results() -> list[CheckResult]:
    return [
        CheckResult(name="title_quality", passed=True, reason="OK", severity=Severity.LOW),
    ]


def _failing_results() -> list[CheckResult]:
    return [
        CheckResult(name="title_quality", passed=False, reason="Too short", severity=Severity.LOW),
    ]


def _pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=1, title="feat: test", body="desc", state="open",
        head=Ref(ref="feat/x", sha="abc"), base=Ref(ref="main", sha="000"),
        user=GitHubUser(login="dev", id=1), changed_files=1, additions=10, deletions=2,
    )
    defaults.update(kwargs)
    return PullRequest(**defaults)


def _ctx(ci_context: str = "") -> PRContext:
    return PRContext(token="tok", repo="acme/widgets", pr=_pr(), ci_context=ci_context)


# ── _format_ci_context ───────────────────────────────────────────────────────


def test_format_ci_context_green():
    result = _format_ci_context("success", [])
    assert "passing" in result
    assert "green" in result


def test_format_ci_context_failure():
    result = _format_ci_context("failure", ["tests", "lint"])
    assert "failing" in result.lower()
    assert "tests" in result
    assert "lint" in result


def test_format_ci_context_overflow():
    many = [f"check-{i}" for i in range(8)]
    result = _format_ci_context("failure", many)
    assert "check-0" in result
    assert "+3 more" in result


def test_format_ci_context_no_names():
    result = _format_ci_context("error", [])
    assert "unknown checks" in result


# ── Agent prompt injection ───────────────────────────────────────────────────


def test_ci_context_renders_in_agent_prompt():
    """When ci_context is set, it appears under '## CI status' in the agent prompt."""
    msg = render_initial_user_message(
        _ctx(ci_context="CI is **failing** (tests). Factor this into your review.")
    )
    assert "## CI status" in msg
    assert "CI is **failing**" in msg


def test_no_ci_context_no_section():
    """When ci_context is empty, no CI section appears."""
    msg = render_initial_user_message(_ctx(ci_context=""))
    assert "## CI status" not in msg


# ── CI banner in comment ─────────────────────────────────────────────────────


def test_ci_red_banner_shown():
    """When CI is failing, the comment includes a CI failure banner."""
    body = build_comment(
        _passing_results(),
        ci_status=("failure", ["tests", "lint"]),
    )
    assert "CI is failing" in body
    assert "tests" in body
    assert "lint" in body
    assert "review reflects the current diff" in body


def test_ci_red_banner_overflow():
    """When many checks fail, only the first 5 are named."""
    many = [f"check-{i}" for i in range(8)]
    body = build_comment(
        _passing_results(),
        ci_status=("failure", many),
    )
    assert "check-0" in body
    assert "check-4" in body
    assert "+3 more" in body


def test_ci_green_no_banner():
    """When CI is passing, no CI banner appears."""
    body = build_comment(
        _passing_results(),
        ci_status=None,
    )
    assert "CI is failing" not in body


def test_ci_error_treated_as_red():
    """CI state 'error' (infra failure) is treated the same as 'failure'."""
    body = build_comment(
        _passing_results(),
        ci_status=("error", ["infra-check"]),
    )
    assert "CI is failing" in body


def test_ci_red_with_no_failed_contexts():
    """When CI is red but no individual contexts are named, show 'unknown checks'."""
    body = build_comment(
        _passing_results(),
        ci_status=("failure", []),
    )
    assert "CI is failing" in body
    assert "unknown checks" in body


def test_ci_red_does_not_suppress_checks():
    """Deterministic checks still appear even when CI is red."""
    body = build_comment(
        _failing_results(),
        ci_status=("failure", ["tests"]),
    )
    assert "CI is failing" in body
    assert "Title Quality" in body


# ── get_commit_status client function ────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_commit_status_success():
    """Green build returns ('success', [])."""
    from pr_warden.github.client import get_commit_status

    respx.get("https://api.github.com/repos/owner/repo/commits/abc123/status").mock(
        return_value=Response(200, json={
            "state": "success",
            "total_count": 2,
            "statuses": [
                {"context": "ci/tests", "state": "success"},
                {"context": "ci/lint", "state": "success"},
            ],
        })
    )
    state, failed = await get_commit_status("fake-token", "owner/repo", "abc123")
    assert state == "success"
    assert failed == []


@pytest.mark.asyncio
@respx.mock
async def test_get_commit_status_failure():
    """Red build returns ('failure', [...failing contexts])."""
    from pr_warden.github.client import get_commit_status

    respx.get("https://api.github.com/repos/owner/repo/commits/abc123/status").mock(
        return_value=Response(200, json={
            "state": "failure",
            "total_count": 2,
            "statuses": [
                {"context": "ci/tests", "state": "failure"},
                {"context": "ci/lint", "state": "success"},
            ],
        })
    )
    state, failed = await get_commit_status("fake-token", "owner/repo", "abc123")
    assert state == "failure"
    assert failed == ["ci/tests"]


@pytest.mark.asyncio
@respx.mock
async def test_get_commit_status_no_checks():
    """Repos with no CI configured return ('success', [])."""
    from pr_warden.github.client import get_commit_status

    respx.get("https://api.github.com/repos/owner/repo/commits/abc123/status").mock(
        return_value=Response(200, json={
            "state": "pending",
            "total_count": 0,
            "statuses": [],
        })
    )
    state, failed = await get_commit_status("fake-token", "owner/repo", "abc123")
    assert state == "success"
    assert failed == []
