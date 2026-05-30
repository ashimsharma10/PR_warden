"""The tool protocol and the Pydantic→Anthropic schema adapter.

Tool design matters more than prompt wording for agent quality: the model
learns what a tool does from its `description`, so descriptions are part of the
implementation, not decoration.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    input_schema: type[ToolInput]

    async def run(self, ctx: PRContext, input: ToolInput) -> ToolResult: ...


def tool_to_anthropic_schema(tool: Tool) -> dict:
    """Render a Tool as the dict the Anthropic SDK's `tools=` argument expects."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema.model_json_schema(),
    }
