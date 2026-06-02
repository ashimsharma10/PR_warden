"""Externalized agent prompts.

The system prompt lives in pr_warden/prompts/ so it can iterate without code
changes. The initial user message gives the agent enough to answer simple PRs
without tools, but not so much that it skips investigation on complex ones — it
should never need a tool to learn "what's in the diff," only "what calls this."
"""

from __future__ import annotations

from pr_warden.agent.context import PRContext
from pr_warden.summarizer.prompts import load_prompt

SYSTEM_PROMPT = load_prompt("agent_system_v1.md")

_MAX_FILES_LISTED = 50

# A single file's patch larger than this is never inlined — one giant file must
# not eat the whole budget. The total inline budget across all files keeps the
# initial message bounded regardless of PR size.
_PER_FILE_MAX_CHARS = 8_000
_TOTAL_DIFF_BUDGET = 30_000


def _file_list(files: list[dict]) -> str:
    rows = []
    for f in files[:_MAX_FILES_LISTED]:
        rows.append(
            f"- {f.get('filename')} "
            f"({f.get('status', '?')}, +{f.get('additions', 0)} -{f.get('deletions', 0)})"
        )
    if len(files) > _MAX_FILES_LISTED:
        rows.append(f"- ... (+{len(files) - _MAX_FILES_LISTED} more)")
    return "\n".join(rows)


def _file_diff_text(f: dict) -> str:
    patch = f.get("patch")
    name = f.get("filename")
    if not patch:
        return f"--- {name} (no inline diff) ---"
    return f"--- {name} ---\n{patch}"


def _withheld_stub(f: dict) -> str:
    name = f.get("filename")
    diff_lines = (f.get("patch") or "").count("\n")
    return (
        f"--- {name} (+{f.get('additions', 0)} -{f.get('deletions', 0)}, "
        f"{diff_lines} diff lines — withheld; "
        f'call get_pr_diff(file_path="{name}") to read it) ---'
    )


def render_diff_section(files: list[dict]) -> str:
    """Inline as many file diffs as fit, smallest first; stub the rest.

    Avoids the blunt char-cap's failure mode (arbitrary mid-file truncation that
    can drop the file that matters). Small files are always shown in full; a file
    too big to inline — individually, or because the total budget is spent — is
    replaced by a one-line stub that points the agent at get_pr_diff(file_path=).
    """
    sized = []
    for f in files:
        text = _file_diff_text(f)
        oversized = bool(f.get("patch")) and len(text) > _PER_FILE_MAX_CHARS
        sized.append((f, text, len(text), oversized))

    included: set[int] = set()
    total = 0
    for idx in sorted(
        (i for i, s in enumerate(sized) if not s[3]),
        key=lambda i: sized[i][2],
    ):
        size = sized[idx][2]
        if total + size > _TOTAL_DIFF_BUDGET:
            break  # ascending order: nothing after this fits either
        included.add(idx)
        total += size

    parts, withheld = [], False
    for i, (f, text, _size, _oversized) in enumerate(sized):
        if i in included:
            parts.append(text)
        else:
            parts.append(_withheld_stub(f))
            withheld = True

    body = "\n\n".join(parts) if parts else "(no file changes)"
    if withheld:
        body = (
            "Larger files are withheld and marked \"withheld\" below — call "
            "get_pr_diff(file_path=...) to read any of them in full.\n\n"
        ) + body
    return body


def render_initial_user_message(ctx: PRContext) -> str:
    pr = ctx.pr
    body = (pr.body or "").strip() or "(no description)"
    return (
        f"Review PR #{ctx.pr_number} in {ctx.repo}.\n\n"
        f"## PR Title\n{pr.title}\n\n"
        f"## PR Description\n{body}\n\n"
        f"## Author\n@{pr.user.login}\n\n"
        f"## Changed files ({len(ctx.files)})\n{_file_list(ctx.files)}\n\n"
        f"## Diff summary\nTotal: +{pr.additions} -{pr.deletions} "
        f"across {len(ctx.files)} files.\n\n"
        f"## Diff\n{render_diff_section(ctx.files)}\n\n"
        "---\nInvestigate with tools to replace assumptions with facts, then call "
        "`done`. Cite evidence for every finding; put anything you couldn't verify "
        "in open_questions rather than guessing."
    )
