"""Shared scaffolding for tools that scan repo files at the PR head SHA.

`find_references` and `search_code` both enumerate the repo tree at the head
SHA, keep the paths they care about, fetch a bounded number concurrently, and
scan the decoded contents. This centralizes the *fetch* half (tree → filter →
bounded gather → skip fetch errors) so the bound and the error-handling live in
one place; each tool keeps its own matcher and its own (tool-specific) scan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from pr_warden.agent.context import PRContext
from pr_warden.github import client


async def fetch_repo_files_at_head(
    ctx: PRContext,
    *,
    match: Callable[[str], bool],
    max_files: int,
) -> tuple[list[tuple[str, str]], int]:
    """Fetch up to `max_files` repo files at the PR head SHA.

    Lists the tree at the head SHA, keeps paths where `match(path)` is true,
    fetches up to `max_files` of them concurrently, and returns the decoded
    ``(path, content)`` pairs — fetch/decode failures are skipped — together
    with the total number of matching paths, which callers use for
    "scanned N of M" footers.
    """
    tree = await client.list_repo_tree(ctx.token, ctx.repo, ctx.head_sha)
    matching = [p for p in tree if match(p)]
    capped = matching[:max_files]

    contents = await asyncio.gather(
        *(
            client.get_repo_file(ctx.token, ctx.repo, p, ref=ctx.head_sha)
            for p in capped
        ),
        return_exceptions=True,
    )
    fetched = [(p, c) for p, c in zip(capped, contents) if isinstance(c, str)]
    return fetched, len(matching)
