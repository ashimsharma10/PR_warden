"""Durable webhook inbox: work is persisted before we 200 GitHub, processed
from the DB, and recovered on startup if a crash interrupted it."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pr_warden.main as main
from pr_warden import models
from pr_warden.models import WebhookEvent


@pytest.fixture
async def db(monkeypatch, tmp_path):
    """A file-backed sqlite DB wired into main.async_session_factory."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/queue.db")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session_factory", factory)
    yield factory
    await engine.dispose()


def _pr_payload(action: str = "opened", number: int = 7) -> dict:
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number, "title": "fix: x", "state": "open", "draft": False,
            "head": {"ref": "fix/x", "sha": "head123"},
            "base": {"ref": "main", "sha": "base000"},
            "user": {"login": "dev", "id": 1},
        },
        "repository": {"id": 1, "full_name": "acme/widgets", "owner": {"login": "acme", "id": 2}},
        "sender": {"login": "dev", "id": 1},
        "installation": {"id": 99},
    }


async def _status(factory, event_id: int) -> str:
    async with factory() as s:
        row = await s.get(WebhookEvent, event_id)
        return row.status


async def test_record_event_persists_pending(db):
    eid = await main._record_event("delivery-1", "pull_request", "opened", _pr_payload(), "trace-1")
    assert eid is not None
    async with db() as s:
        row = await s.get(WebhookEvent, eid)
    assert row.status == "pending"
    assert row.delivery_id == "delivery-1"
    assert row.payload["number"] == 7


async def test_process_event_dispatches_and_marks_done(db, monkeypatch):
    seen = []

    async def fake_handle(event, trace_id):
        seen.append((event.number, trace_id))

    monkeypatch.setattr(main, "_handle_pr_event", fake_handle)
    eid = await main._record_event("d2", "pull_request", "opened", _pr_payload(number=12), "tr2")

    await main._process_event(eid)

    assert seen == [(12, "tr2")]
    assert await _status(db, eid) == "done"


async def test_process_event_records_failure(db, monkeypatch):
    async def boom(event, trace_id):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main, "_handle_pr_event", boom)
    eid = await main._record_event("d3", "pull_request", "opened", _pr_payload(), "tr3")

    await main._process_event(eid)  # must not raise

    async with db() as s:
        row = await s.get(WebhookEvent, eid)
    assert row.status == "failed"
    assert "kaboom" in row.error


async def test_recover_pending_events_redispatches(db, monkeypatch):
    # Seed an event left mid-flight (e.g. a crash): status processing, not done.
    eid = await main._record_event("d4", "pull_request", "opened", _pr_payload(), "tr4")
    async with db() as s:
        row = await s.get(WebhookEvent, eid)
        row.status = "processing"
        await s.commit()

    processed = []

    async def fake_process(event_id):
        processed.append(event_id)

    monkeypatch.setattr(main, "_process_event", fake_process)
    await main._recover_pending_events()
    await asyncio.sleep(0.02)  # let the scheduled task run

    assert processed == [eid]


async def test_recover_skips_done_events(db, monkeypatch):
    eid = await main._record_event("d5", "pull_request", "opened", _pr_payload(), "tr5")
    async with db() as s:
        row = await s.get(WebhookEvent, eid)
        row.status = "done"
        await s.commit()

    processed = []

    async def fake_process(event_id):
        processed.append(event_id)

    monkeypatch.setattr(main, "_process_event", fake_process)
    await main._recover_pending_events()
    await asyncio.sleep(0.02)

    assert processed == []
