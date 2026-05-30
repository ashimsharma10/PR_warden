from __future__ import annotations

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult


class GetPRDiffInput(ToolInput):
    file_path: str | None = None  # if set, only that file's diff


class GetPRDiffTool:
    name = "get_pr_diff"
    description = """Get the diff for this PR in unified diff format.
If file_path is given, returns only that file's diff — useful once you've
identified a relevant file and want its exact line-by-line changes.

Note: a high-level diff is already in your context. Reach for this when you
need precise hunks, especially for tests."""
    input_schema = GetPRDiffInput

    async def run(self, ctx: PRContext, input: GetPRDiffInput) -> ToolResult:
        if input.file_path:
            diff = ctx.file_diff(input.file_path)
            if diff is None:
                return ToolResult(
                    ok=False,
                    content=f"No changes to '{input.file_path}' in this PR.",
                    error="not_found",
                )
            return ToolResult(ok=True, content=diff)
        return ToolResult(ok=True, content=ctx.full_diff())
