"""Tests for CI awareness: skip agent on red builds, surface CI status in comment."""
import pytest
import respx
from httpx import Response

from pr_warden.composer import build_comment
from pr_warden.checks.registry import CheckResult, Severity


def _passing_results() -> list[CheckResult]:
    return [
        CheckResult(name="title_quality", passed=True, reason="OK", severity=Severity.LOW),
    ]


def _failing_results() -> list[CheckResult]:
    return [
        CheckResult(name="title_quality", passed=False, reason="Too short", severity=Severity.LOW),
    ]


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
    assert "deep review skipped" in body


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
    assert "infra-check" in body


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
