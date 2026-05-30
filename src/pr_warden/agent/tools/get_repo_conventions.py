"""Surface the repo's stated conventions so the agent can judge against them.

Without this the agent has zero awareness of what the maintainers care about — a
PR that violates a documented style or architecture rule is invisible to it.
Fetched live at the head SHA; no persistent cache yet (a per-repo daily cache is
a cheap future optimization).
"""

from __future__ import annotations

import asyncio

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.github import client
from pr_warden.repo_config import CONFIG_PATH

_CONVENTION_PATHS = [
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    ".editorconfig",
    CONFIG_PATH,
]
_ARCH_PREFIX = "docs/architecture/"
_MAX_ARCH_FILES = 10
_PER_FILE_CAP = 4_000


class GetRepoConventionsInput(ToolInput):
    pass


class GetRepoConventionsTool:
    name = "get_repo_conventions"
    description = """Get this repo's contribution guidelines, code conventions, and
architectural decisions if documented. Returns content from CONTRIBUTING.md,
AGENTS.md, CODE_OF_CONDUCT.md, .editorconfig, docs/architecture/, and the repo's
prwarden.yml if present.

Call this early when reviewing non-trivial PRs to understand what the repo's
maintainers care about. Don't call it for trivial PRs."""
    input_schema = GetRepoConventionsInput

    async def run(self, ctx: PRContext, input: GetRepoConventionsInput) -> ToolResult:
        try:
            tree = await client.list_repo_tree(ctx.token, ctx.repo, ctx.head_sha)
        except Exception:  # noqa: BLE001 — tree is best-effort; fixed paths still work
            tree = []
        arch = [
            p for p in tree if p.startswith(_ARCH_PREFIX) and p.endswith(".md")
        ][:_MAX_ARCH_FILES]

        # dedupe while preserving order
        paths = list(dict.fromkeys(_CONVENTION_PATHS + arch))
        contents = await asyncio.gather(
            *(client.get_repo_file(ctx.token, ctx.repo, p, ref=ctx.head_sha) for p in paths),
            return_exceptions=True,
        )

        sections = []
        for path, content in zip(paths, contents):
            if isinstance(content, str) and content.strip():
                sections.append(f"# {path}\n{content[:_PER_FILE_CAP]}")

        if not sections:
            return ToolResult(
                ok=True,
                content="No convention or architecture docs found in this repo.",
            )
        return ToolResult(
            ok=True,
            content="\n\n".join(sections),
            metadata={"docs_found": len(sections)},
        )
