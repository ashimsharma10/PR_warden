"""Blame a single line: who last changed it, when, why, and in which PR.

Lets the agent understand *why* existing code looks the way it does before
judging a change to it — a line last touched in a "fix race condition" PR
deserves more care than boilerplate.

Blame is not in GitHub's REST API; it's GraphQL-only, so this tool goes through
`client.get_blame` (the lone GraphQL path in the codebase) rather than the REST
client every other tool uses. Blame is computed at the PR head SHA so it
reflects the code under review.
"""

from __future__ import annotations

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.github import client


def _range_for_line(ranges: list[dict], line: int) -> dict | None:
    """Find the blame range whose [starting, ending] span covers `line`."""
    for rng in ranges:
        if rng.get("startingLine", 0) <= line <= rng.get("endingLine", 0):
            return rng
    return None


def _format_range(rng: dict, path: str, line: int) -> str:
    commit = rng.get("commit") or {}
    sha = (commit.get("oid") or "")[:8]
    headline = commit.get("messageHeadline") or "(no message)"
    when = (commit.get("committedDate") or "")[:10]

    author = commit.get("author") or {}
    user = author.get("user") or {}
    who = user.get("login") or author.get("name") or "unknown"

    prs = ((commit.get("associatedPullRequests") or {}).get("nodes")) or []
    pr_line = ""
    if prs:
        pr = prs[0]
        pr_line = f"\nIntroduced in PR #{pr.get('number')}: {pr.get('title', '')}"

    return (
        f"{path}:{line} last changed by {who} on {when}\n"
        f"commit {sha}: {headline}{pr_line}"
    )


class GitBlameInput(ToolInput):
    path: str
    line: int


class GitBlameTool:
    name = "git_blame"
    description = """Blame a single line of an existing file: who last changed it,
when, the commit message, and the PR that introduced it.

Use this to understand the history behind code the PR modifies — whether a line
was recently touched, what change it came from, and why. Pass the file path and
a 1-based line number (at the PR's head state)."""
    input_schema = GitBlameInput

    async def run(self, ctx: PRContext, input: GitBlameInput) -> ToolResult:
        if input.line < 1:
            return ToolResult(
                ok=False, content="line must be >= 1.", error="bad_input"
            )

        try:
            ranges = await client.get_blame(
                ctx.token, ctx.repo, input.path, ref=ctx.head_sha
            )
        except Exception as e:  # GraphQL/transport failure — never fatal to the loop
            return ToolResult(
                ok=False,
                content=f"Blame unavailable for '{input.path}': {e}",
                error="blame_failed",
            )

        if not ranges:
            return ToolResult(
                ok=False,
                content=f"No blame for '{input.path}' at the PR head "
                "(file may not exist or be newly added in this PR).",
                error="not_found",
            )

        rng = _range_for_line(ranges, input.line)
        if rng is None:
            last = max((r.get("endingLine", 0) for r in ranges), default=0)
            return ToolResult(
                ok=False,
                content=f"Line {input.line} is out of range for "
                f"'{input.path}' (file has {last} blamed lines).",
                error="out_of_range",
            )

        commit = rng.get("commit") or {}
        return ToolResult(
            ok=True,
            content=_format_range(rng, input.path, input.line),
            metadata={"sha": commit.get("oid"), "date": commit.get("committedDate")},
        )
