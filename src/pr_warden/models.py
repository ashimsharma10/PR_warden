from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    cached_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    checks: Mapped[list["PRCheck"]] = relationship(back_populates="repo")


class PRCheck(Base):
    __tablename__ = "pr_check"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repo.id"), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sha: Mapped[str] = mapped_column(String(40), nullable=False)
    check_results: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(nullable=True)
    # Full AgentResult when the review agent ran: assessment, trace, stopped_for,
    # token counts. Null when the agent is disabled for the repo or didn't run.
    agent_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(String(50), nullable=True)
    comment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    repo: Mapped["Repo"] = relationship(back_populates="checks")


class WebhookEvent(Base):
    """Durable inbox for incoming webhooks.

    The handler persists an actionable event here (status ``pending``) and commits
    *before* returning 200, so a crash or redeploy mid-review can't silently drop
    it: on startup, ``pending``/``processing`` rows are re-dispatched. Work that
    completes is marked ``done``; a handler that raises is marked ``failed``.

    ``delivery_id`` is GitHub's ``X-GitHub-Delivery`` header — the natural dedup
    key for at-least-once redeliveries (a unique index is added in a later step).
    """

    __tablename__ = "webhook_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
