from __future__ import annotations

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.github import client

_MAX_LINES = 1000
_DEFAULT_REGION = 100


class GetFileInput(ToolInput):
    path: str
    start_line: int | None = None
    end_line: int | None = None


class GetFileTool:
    name = "get_file"
    description = """Read a file from the repository at the PR's current (head) state.
Use this when you need to see code that wasn't in the diff but is relevant to
understanding the change — like the callers of a modified function, or the file
that imports the changed module.

Specify start_line and end_line to fetch only a region (preferred for large
files). If omitted, returns the entire file up to 1000 lines."""
    input_schema = GetFileInput

    async def run(self, ctx: PRContext, input: GetFileInput) -> ToolResult:
        content = await client.get_repo_file(
            ctx.token, ctx.repo, input.path, ref=ctx.head_sha
        )
        if content is None:
            return ToolResult(
                ok=False,
                content=f"File '{input.path}' does not exist at the PR head commit.",
                error="not_found",
            )

        lines = content.splitlines()
        if input.start_line is not None:
            start = max(input.start_line, 1)
            end = input.end_line or start + _DEFAULT_REGION
            region = lines[start - 1 : end]
            prefix = f"# {input.path} lines {start}-{end}\n"
            shown = region
        elif len(lines) > _MAX_LINES:
            shown = lines[:_MAX_LINES]
            prefix = f"# {input.path} (truncated to first {_MAX_LINES} lines)\n"
        else:
            shown = lines
            prefix = f"# {input.path}\n"

        return ToolResult(
            ok=True,
            content=prefix + "\n".join(shown),
            metadata={"bytes": len(content), "total_lines": len(lines)},
        )
