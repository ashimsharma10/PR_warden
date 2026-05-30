"""Text/regex search across the repo at the PR's head SHA.

Strategy decision (issue #7): GitHub's REST code-search API indexes the
*default branch*, not the PR's head SHA, and is rate-limited and
eventually-consistent — so a hit it returns may not reflect the code under
review. Rather than search the wrong commit, this reuses the `find_references`
approach: enumerate the repo tree at the head SHA and grep the fetched files
in-process. Accurate to the PR, no clone step, bounded by `_MAX_FILES` so a
single call stays within its timeout. The cost is breadth — only the first
`_MAX_FILES` text files are scanned — which the footer makes explicit.

Unlike `find_references` (AST, Python-only, symbol-precise), this is a plain
text/regex search over any text file: use it to find string literals, config
keys, TODOs, or usages in non-Python files.
"""

from __future__ import annotations

import asyncio
import re

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.github import client

_MAX_FILES = 60          # text files fetched per call
_MAX_RESULTS = 100       # hard cap on hits returned
_MAX_LINE_LEN = 200      # truncate long matched lines so one hit isn't a wall

# Binary / non-source extensions we never want to grep through.
_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".mp3", ".bin",
    ".lock", ".min.js", ".min.css", ".map",
)


def _fnmatch_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a simple glob (``*.py``, ``src/**/x.py``) to a path regex."""
    import fnmatch

    return re.compile(fnmatch.translate(pattern))


def search_in_source(
    source: str, matcher: re.Pattern[str], path: str
) -> list[str]:
    """Return ``path:line: text`` for every line in `source` matching `matcher`."""
    hits: list[str] = []
    for n, line in enumerate(source.splitlines(), start=1):
        if matcher.search(line):
            text = line.strip()
            if len(text) > _MAX_LINE_LEN:
                text = text[:_MAX_LINE_LEN] + " …"
            hits.append(f"{path}:{n}: {text}")
    return hits


class SearchCodeInput(ToolInput):
    query: str                          # literal text, or a regex if is_regex
    is_regex: bool = False
    file_pattern: str | None = None     # glob to restrict paths, e.g. "*.py"
    max_results: int = 50


class SearchCodeTool:
    name = "search_code"
    description = """Text or regex search across the repository at the PR's head state.
Use it to find where a string, config key, or pattern appears — callers of a
function, usages of a constant, TODOs, occurrences in non-Python files.

Set is_regex=true to treat the query as a regular expression. Restrict the
search with file_pattern (a glob like "*.py" or "src/api/*"). Returns
file:line: text for each hit.

For Python function/class usages prefer find_references — it parses code and
ignores matches in comments and strings. Use this for everything else, or when
you want literal/regex matching. Scans a bounded number of files."""
    input_schema = SearchCodeInput

    async def run(self, ctx: PRContext, input: SearchCodeInput) -> ToolResult:
        if not input.query:
            return ToolResult(ok=False, content="Empty query.", error="bad_input")

        try:
            flags = 0 if input.is_regex else re.IGNORECASE
            pattern = (
                re.compile(input.query, flags)
                if input.is_regex
                else re.compile(re.escape(input.query), flags)
            )
        except re.error as e:
            return ToolResult(
                ok=False, content=f"Invalid regex: {e}", error="bad_regex"
            )

        path_filter = (
            _fnmatch_to_regex(input.file_pattern) if input.file_pattern else None
        )

        tree = await client.list_repo_tree(ctx.token, ctx.repo, ctx.head_sha)
        candidates = [
            p
            for p in tree
            if not p.endswith(_SKIP_SUFFIXES)
            and (path_filter is None or path_filter.match(p))
        ]
        capped = candidates[:_MAX_FILES]

        contents = await asyncio.gather(
            *(
                client.get_repo_file(ctx.token, ctx.repo, p, ref=ctx.head_sha)
                for p in capped
            ),
            return_exceptions=True,
        )

        limit = max(1, min(input.max_results, _MAX_RESULTS))
        hits: list[str] = []
        files_with_hits = 0
        capped_results = False
        for path, content in zip(capped, contents):
            if not isinstance(content, str):
                continue
            found = search_in_source(content, pattern, path)
            if not found:
                continue
            files_with_hits += 1
            for h in found:
                hits.append(h)
                if len(hits) >= limit:
                    capped_results = True
                    break
            if capped_results:
                break

        if not hits:
            return ToolResult(
                ok=True,
                content=f"No matches for '{input.query}' in {len(capped)} files.",
            )

        footer = ""
        if capped_results:
            footer = (
                f"\n[results capped at {limit}; refine the query or file_pattern "
                "to narrow — there may be more]"
            )
        elif len(candidates) > _MAX_FILES:
            footer = (
                f"\n[scanned first {_MAX_FILES} of {len(candidates)} files; "
                "narrow with file_pattern for fuller coverage]"
            )
        return ToolResult(
            ok=True,
            content="\n".join(hits) + footer,
            metadata={
                "hits": len(hits),
                "files_with_hits": files_with_hits,
                "files_scanned": len(capped),
                "total_candidates": len(candidates),
            },
        )
