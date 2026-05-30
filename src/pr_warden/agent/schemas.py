"""Pydantic contracts shared across the agent.

`ToolResult` deliberately splits `content` (what the model sees) from
`metadata` (what we log). The model must never see request IDs, byte counts,
or cache status — they clutter its reasoning without helping it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolInput(BaseModel):
    """Base for tool input schemas. Each tool subclasses and adds fields."""


class ToolResult(BaseModel):
    ok: bool
    content: str                            # what the agent sees
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)  # logging only; never shown


class DoneInput(ToolInput):
    """The agent's final structured assessment — the `done` tool's arguments."""

    summary: str
    files_touched: list[str] = Field(default_factory=list)
    intent_matches_diff: bool
    intent_mismatch_reason: str = ""
    notable: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class AgentResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    assessment: DoneInput
    trace: list[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float = 0.0
    # done | tool_call_budget | token_budget | max_iterations | no_tool_call
    stopped_for: str = "done"
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    duration_ms: int = 0
