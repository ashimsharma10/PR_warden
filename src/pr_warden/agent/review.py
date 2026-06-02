"""The review agent, expressed as an AgentSpec.

This is the one agent pr_warden ships today. Everything review-specific that
used to live as module-level constants in the loop is gathered here: the system
prompt, the first user message, the DoneInput output schema, the `done`
guidance, the investigation toolset, and the budget-exhaustion fallback.
"""

from __future__ import annotations

from pr_warden.agent.loop import MODEL
from pr_warden.agent.prompts import SYSTEM_PROMPT, render_initial_user_message
from pr_warden.agent.schemas import DoneInput
from pr_warden.agent.spec import AgentSpec
from pr_warden.agent.tools import build_tools
from pr_warden.agent.tools.done import REVIEW_DONE_DESCRIPTION


def review_fallback(reason: str) -> DoneInput:
    """A valid, honest assessment for when the run is force-finalized without a
    real `done` (budget exhausted, model wouldn't finalize). Confidence 0.0 so
    the verdict reads inconclusive rather than a false pass."""
    return DoneInput(
        summary="The agent stopped before completing its assessment.",
        files_touched=[],
        intent_matches_diff=True,
        intent_mismatch_reason="",
        attention=[],
        open_questions=[f"Assessment incomplete: {reason}."],
        confidence=0.0,
    )


REVIEW_AGENT = AgentSpec(
    name="review",
    model=MODEL,
    system_prompt=SYSTEM_PROMPT,
    render_user_message=render_initial_user_message,
    output_schema=DoneInput,
    done_description=REVIEW_DONE_DESCRIPTION,
    build_tools=build_tools,
    fallback=review_fallback,
    finalize_hint=(
        "You've reached your budget. Call `done` now with whatever you have — "
        "put anything unverified in open_questions."
    ),
)
