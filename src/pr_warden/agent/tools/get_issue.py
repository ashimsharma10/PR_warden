from __future__ import annotations

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.github import client

_BODY_CAP = 2000
_COMMENT_CAP = 300


class GetIssueInput(ToolInput):
    number: int


class GetIssueTool:
    name = "get_issue"
    description = """Fetch an issue by number. Use this when the PR claims to
fix or address an issue (e.g. "Fixes #123"). Returns the title, body, labels,
and most recent comments.

Verifying the PR actually addresses the linked issue is one of the most
valuable checks you can do."""
    input_schema = GetIssueInput

    async def run(self, ctx: PRContext, input: GetIssueInput) -> ToolResult:
        issue = await client.get_issue(ctx.token, ctx.repo, input.number)
        if issue is None:
            return ToolResult(
                ok=False,
                content=f"Issue #{input.number} does not exist.",
                error="not_found",
            )

        labels = ", ".join(lbl.get("name", "") for lbl in issue.get("labels", []))
        body = (issue.get("body") or "")[:_BODY_CAP]
        out = (
            f"# Issue #{issue.get('number')}: {issue.get('title', '')}\n"
            f"State: {issue.get('state', '')}\n"
            f"Labels: {labels}\n\n"
            f"{body}\n"
        )

        comments = await client.list_issue_comments(ctx.token, ctx.repo, input.number)
        if comments:
            out += "\n## Recent comments:\n"
            for c in comments[-3:]:
                author = (c.get("user") or {}).get("login", "unknown")
                out += f"@{author}: {(c.get('body') or '')[:_COMMENT_CAP]}\n\n"

        return ToolResult(ok=True, content=out, metadata={"comment_count": len(comments)})
