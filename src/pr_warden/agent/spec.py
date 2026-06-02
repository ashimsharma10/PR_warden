"""What makes one agent distinct from another.

The loop in `loop.py` is the engine: it owns iteration, the budget, parallel
tool execution, force-finalize, and termination. None of that is specific to
the review agent. What *is* specific — the system prompt, the first user
message, the output schema, the toolset, and the budget-exhaustion fallback —
lives here as a value the engine is generic over.

Adding a second agent (intent, completeness, blast-radius, ...) is "write
another AgentSpec," not "edit the loop."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from pr_warden.agent.context import PRContext
from pr_warden.agent.tools.base import Tool


@dataclass(frozen=True)
class AgentSpec:
    name: str                                        # telemetry / log key
    model: str                                       # default model for this agent
    system_prompt: str
    render_user_message: Callable[[PRContext], str]  # the first user turn
    output_schema: type[BaseModel]                   # the `done` tool's args schema
    done_description: str                            # how `done` is pitched to the model
    build_tools: Callable[[], list[Tool]]            # investigation tools; the loop adds `done`
    fallback: Callable[[str], BaseModel]             # a valid assessment when the budget blows
    finalize_hint: str                               # nudge appended when forcing `done`
