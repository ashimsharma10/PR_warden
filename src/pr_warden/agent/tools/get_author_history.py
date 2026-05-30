"""Give the agent the track-record context a maintainer carries implicitly.

Lets it reason about patterns ("five PRs in two days, one merged") and adjust
trust ("twelve merged PRs here, weight their changes accordingly").
"""

from __future__ import annotations

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.github import client

_MAX_LIMIT = 30


class GetAuthorHistoryInput(ToolInput):
    author: str | None = None  # defaults to this PR's author
    limit: int = 10


class GetAuthorHistoryTool:
    name = "get_author_history"
    description = """Get an author's recent PR history on this specific repo.
Returns their last PRs with title, state (merged/closed/open), and date.

Useful for understanding whether this is a new contributor or someone with a
track record. Skip for trusted maintainers; useful for unknown authors."""
    input_schema = GetAuthorHistoryInput

    async def run(self, ctx: PRContext, input: GetAuthorHistoryInput) -> ToolResult:
        author = input.author or ctx.pr.user.login
        limit = max(1, min(input.limit, _MAX_LIMIT))
        items = await client.search_author_prs(ctx.token, ctx.repo, author, limit=limit)

        rows, merged = [], 0
        for it in items:
            if it.get("number") == ctx.pr_number:
                continue  # don't report the PR under review back to itself
            is_merged = bool((it.get("pull_request") or {}).get("merged_at"))
            merged += is_merged
            state = "merged" if is_merged else it.get("state", "?")
            created = (it.get("created_at") or "")[:10]
            rows.append(f"#{it.get('number')} {it.get('title', '')} — {state} ({created})")

        if not rows:
            return ToolResult(ok=True, content=f"@{author} has no prior PRs on this repo.")
        header = f"@{author}: {len(rows)} recent PR(s) on this repo, {merged} merged.\n"
        return ToolResult(
            ok=True,
            content=header + "\n".join(rows),
            metadata={"total": len(rows), "merged": merged},
        )
