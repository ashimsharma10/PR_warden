"""Find references to a Python symbol across the repo, using the stdlib `ast`.

Python-only by design (the dogfood repo is Python). Because there is no
sandbox, this enumerates the repo tree and fetches `.py` files at the head SHA,
bounded by `_MAX_FILES` so a single tool call stays within its timeout. AST
matching ignores comments and string literals — that precision is the whole
reason to prefer this over a text search.
"""

from __future__ import annotations

import ast
import asyncio

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.github import client

_MAX_FILES = 40


def find_symbol_refs(source: str, symbol: str) -> list[int]:
    """Return the line numbers where `symbol` is used as a name or attribute.

    Returns [] if the source can't be parsed (syntax error at this commit).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol:
            lines.add(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == symbol:
            lines.add(node.lineno)
    return sorted(lines)


class FindReferencesInput(ToolInput):
    symbol: str                # function or class name
    file_path: str             # where it's defined (for the agent's context)


class FindReferencesTool:
    name = "find_references"
    description = """Find references to a Python function or class across the repo.
Pass the symbol name and the file where it's defined. Returns file:line for each
usage.

More precise than a text search because it parses code and ignores matches in
comments and strings. Python-only; scans a bounded number of files."""
    input_schema = FindReferencesInput

    async def run(self, ctx: PRContext, input: FindReferencesInput) -> ToolResult:
        tree = await client.list_repo_tree(ctx.token, ctx.repo, ctx.head_sha)
        py_files = [p for p in tree if p.endswith(".py")]
        capped = py_files[:_MAX_FILES]

        contents = await asyncio.gather(
            *(
                client.get_repo_file(ctx.token, ctx.repo, p, ref=ctx.head_sha)
                for p in capped
            ),
            return_exceptions=True,
        )

        hits: list[str] = []
        for path, content in zip(capped, contents):
            if not isinstance(content, str):
                continue
            for line in find_symbol_refs(content, input.symbol):
                hits.append(f"{path}:{line}")

        if not hits:
            return ToolResult(
                ok=True,
                content=f"No references to '{input.symbol}' found in {len(capped)} Python files.",
            )

        footer = ""
        if len(py_files) > _MAX_FILES:
            footer = f"\n[scanned first {_MAX_FILES} of {len(py_files)} Python files]"
        return ToolResult(
            ok=True,
            content="\n".join(hits) + footer,
            metadata={"files_scanned": len(capped), "total_py_files": len(py_files)},
        )
