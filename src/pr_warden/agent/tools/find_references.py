"""Find references to a Python symbol across the repo, using the stdlib `ast`.

Python-only by design (the dogfood repo is Python). Because there is no
sandbox, this enumerates the repo tree and fetches `.py` files at the head SHA,
bounded by `_MAX_FILES` so a single tool call stays within its timeout. AST
matching ignores comments and string literals — that precision is the whole
reason to prefer this over a text search.
"""

from __future__ import annotations

import ast

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.agent.tools._repo_files import fetch_repo_files_at_head

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
        fetched, total = await fetch_repo_files_at_head(
            ctx, match=lambda p: p.endswith(".py"), max_files=_MAX_FILES
        )
        scanned = min(_MAX_FILES, total)

        hits: list[str] = []
        for path, content in fetched:
            for line in find_symbol_refs(content, input.symbol):
                hits.append(f"{path}:{line}")

        if not hits:
            return ToolResult(
                ok=True,
                content=f"No references to '{input.symbol}' found in {scanned} Python files.",
            )

        footer = ""
        if total > _MAX_FILES:
            footer = f"\n[scanned first {_MAX_FILES} of {total} Python files]"
        return ToolResult(
            ok=True,
            content="\n".join(hits) + footer,
            metadata={"files_scanned": scanned, "total_py_files": total},
        )
