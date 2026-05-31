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

    summary: str = Field(
        description="1-2 sentences on what the diff actually changes, grounded in "
        "the code you read — not a restatement of the PR description."
    )
    files_touched: list[str] = Field(
        default_factory=list,
        description="High-level areas affected, e.g. [\"auth\", \"tests\"].",
    )
    intent_matches_diff: bool = Field(
        description="Does the diff actually do what the PR/issue claims? Only set "
        "false when you can point to the specific discrepancy."
    )
    intent_mismatch_reason: str = Field(
        default="",
        description="Empty if intent matches; otherwise the concrete reason with a "
        "citation (path:line, file, or issue).",
    )
    notable: list[str] = Field(
        default_factory=list,
        description="Up to 3 things worth a second look, each VERIFIED from the diff "
        "or a tool and cited. No speculation — unverified concerns go in "
        "open_questions instead.",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Everything you could not confirm, phrased as specific things "
        "for the maintainer to check. Put uncertainty here; do not guess.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0, honestly reflecting how much of this assessment is "
        "backed by evidence you read versus inference. Lower it when unsure.",
    )


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
