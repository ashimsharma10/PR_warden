"""/stats is secure by default: closed unless a token is set or it's made public."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import pr_warden.main as main


async def test_stats_disabled_when_no_token(monkeypatch):
    monkeypatch.setattr(main.settings, "stats_bearer_token", "")
    monkeypatch.setattr(main.settings, "stats_public", False)
    with pytest.raises(HTTPException) as exc:
        await main._require_stats_token(authorization=None)
    assert exc.value.status_code == 403


async def test_stats_public_opt_in_allows_without_token(monkeypatch):
    monkeypatch.setattr(main.settings, "stats_bearer_token", "")
    monkeypatch.setattr(main.settings, "stats_public", True)
    assert await main._require_stats_token(authorization=None) is None


async def test_stats_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(main.settings, "stats_bearer_token", "secret")
    monkeypatch.setattr(main.settings, "stats_public", False)
    with pytest.raises(HTTPException) as exc:
        await main._require_stats_token(authorization="Bearer wrong")
    assert exc.value.status_code == 401


async def test_stats_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(main.settings, "stats_bearer_token", "secret")
    monkeypatch.setattr(main.settings, "stats_public", False)
    assert await main._require_stats_token(authorization="Bearer secret") is None
