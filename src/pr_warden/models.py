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
    overrides: Mapped[list["Override"]] = relationship(back_populates="pr_check")


class Override(Base):
    __tablename__ = "override"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pr_check_id: Mapped[int] = mapped_column(ForeignKey("pr_check.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    pr_check: Mapped["PRCheck"] = relationship(back_populates="overrides")
