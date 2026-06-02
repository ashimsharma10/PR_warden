"""Pydantic contracts shared across the agent.

`ToolResult` deliberately splits `content` (what the model sees) from
`metadata` (what we log). The model must never see request IDs, byte counts,
or cache status — they clutter its reasoning without helping it.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The agent's output schema. Generic so the loop (the engine) can carry any
# agent's structured assessment, not just the review agent's DoneInput.
OutputT = TypeVar("OutputT", bound=BaseModel)

# Risk and centrality each map to a weight; an item's rank is their product, so a
# high-risk change in load-bearing code (3×3=9) outranks a high-risk leaf (3×1=3).
_RANK_WEIGHT = {"high": 3, "medium": 2, "low": 1}


class ToolInput(BaseModel):
    """Base for tool input schemas. Each tool subclasses and adds fields."""


class ToolResult(BaseModel):
    ok: bool
    content: str                            # what the agent sees
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)  # logging only; never shown


class AttentionItem(BaseModel):
    """One spot on the attention map: where to look, why, and how it ranks.

    Splits the rank into two axes the model scores independently — `risk` (how
    likely this is to be wrong/harmful) and `centrality` (how load-bearing the
    code is). The composer orders the map by their product so the maintainer's
    eyeballs land on the highest-leverage spot first.
    """

    location: str = Field(
        description="Where to look — `path:line`, a file, or an issue ref. The "
        "concrete anchor for the claim, never a vague area name."
    )
    why: str = Field(
        description="One line: why a maintainer must look here, naming the "
        "downstream impact (what depends on this, what breaks) — not a restatement "
        "of the code."
    )
    risk: Literal["high", "medium", "low"] = Field(
        description="How likely this is to be wrong or harmful if it ships unreviewed."
    )
    centrality: Literal["high", "medium", "low"] = Field(
        description="How load-bearing the touched code is — how much of the system "
        "depends on it."
    )

    @field_validator("risk", "centrality", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> Any:
        # Accept "High"/" LOW " without bouncing the whole `done` call back for a
        # retry over capitalization the model would otherwise have to re-emit.
        return v.lower().strip() if isinstance(v, str) else v

    @property
    def priority(self) -> int:
        return _RANK_WEIGHT[self.risk] * _RANK_WEIGHT[self.centrality]


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
    attention: list[AttentionItem] = Field(
        default_factory=list,
        description="The attention map: up to 3 spots a maintainer must look, each "
        "VERIFIED from the diff or a tool and cited. The comment ranks them by "
        "risk × centrality. No speculation — unverified concerns go in "
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


class AgentResult(BaseModel, Generic[OutputT]):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    assessment: OutputT
    trace: list[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float = 0.0
    # done | tool_call_budget | token_budget | max_iterations | no_tool_call
    stopped_for: str = "done"
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    duration_ms: int = 0
