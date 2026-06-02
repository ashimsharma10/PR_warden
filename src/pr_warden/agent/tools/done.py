"""The termination tool.

Instead of parsing prose to guess "is the agent finished," the agent signals
completion by calling `done` with its structured assessment. The loop sees the
tool name and stops; the assessment IS the tool-call arguments. This tool has
no `run` — the loop intercepts `done` before tool execution.
"""

from __future__ import annotations

from pr_warden.agent.schemas import DoneInput, ToolResult


class DoneTool:
    name = "done"
    description = """Signal that you have enough information to produce your final
assessment. Call this with your structured output. After this is called, no more
tools will be invoked.

Before calling this, make sure:
- Your `summary` describes what the diff actually changes (not just what the PR
  description claims).
- Every item in `attention` is a spot you verified from the diff or a tool, cited
  by `location` (path:line, file, or issue), with a `why` that names the
  downstream impact, and honest `risk`/`centrality` ratings.
- Anything you suspect but could NOT verify is in `open_questions`, not `attention`.
- `confidence` honestly reflects how much of your assessment is backed by evidence.

When in doubt, prefer "I could not verify X — please check" in `open_questions`
over a confident guess. Admitting uncertainty is correct; guessing is not."""
    input_schema = DoneInput

    async def run(self, ctx, input: DoneInput) -> ToolResult:  # pragma: no cover
        # The loop intercepts `done` and never calls this; present only so the
        # tool satisfies the Tool protocol.
        return ToolResult(ok=True, content="done")
