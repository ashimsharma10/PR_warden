"""Deterministic intent / scope check (~0s, $0, no LLM).

The agent has an `intent_matches_diff` field, but it's gated behind the LLM
allowlist and the daily budget — most repos never get it. This is the always-on
deterministic counterpart, and it answers one narrow, high-signal question:

    The linked issue names a module of this repo. Does the diff touch it?

"The issue says auth, the diff touches billing — flag it." That single check
catches more real scope creep than any complexity metric, and it's tuned to
almost never false-fire: it only speaks when the issue name-drops a *real*,
*distinctive* directory of the repo and the diff goes *nowhere near it*.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from pr_warden.checks.registry import CheckContext, CheckResult, Severity, register
from pr_warden.repo_config import IntentScopeConfig

# GitHub closing keywords — the strong "this PR addresses that issue" signal.
# A bare "#123" mention is deliberately ignored: it's often tangential and would
# cost precision. Cross-repo refs (owner/repo#123) are skipped — we can only map
# modules against the repo we're reviewing.
_ISSUE_REF_RE = re.compile(
    r"(?<![/\w])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b",
    re.IGNORECASE,
)


def extract_issue_refs(body: str) -> list[int]:
    """Issue numbers a PR body links via a closing keyword, de-duped, in order.

    Recognizes close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved
    followed by ``#N`` (GitHub's own closing-keyword grammar). Returns ``[]`` for
    empty/None-ish input.
    """
    if not body:
        return []
    seen: dict[int, None] = {}
    for m in _ISSUE_REF_RE.finditer(body):
        seen.setdefault(int(m.group(1)), None)
    return list(seen)


def _dir_components(path: str) -> list[str]:
    """Lowercased directory components of a path (everything but the filename)."""
    return [c.lower() for c in PurePosixPath(path).parts[:-1]]


def repo_modules(repo_tree: list[str], cfg: IntentScopeConfig) -> set[str]:
    """Distinctive directory names present in the repo tree.

    A "module" here is any directory name that's specific enough to mean
    something when an issue names it — generic containers (`src`, `lib`, `tests`)
    and very short names are filtered out so a match is meaningful.
    """
    ignore = {m.lower() for m in cfg.ignore_modules}
    mods: set[str] = set()
    for path in repo_tree:
        for comp in _dir_components(path):
            if len(comp) >= cfg.min_module_len and comp not in ignore:
                mods.add(comp)
    return mods


def touched_modules(files: list[dict], modules: set[str]) -> set[str]:
    """The subset of `modules` whose directory the diff actually changes."""
    touched: set[str] = set()
    for f in files:
        for comp in _dir_components(f["filename"]):
            if comp in modules:
                touched.add(comp)
    return touched


def _mentions(text: str, module: str) -> bool:
    """True when `module` appears as a whole word in already-lowercased `text`."""
    return re.search(rf"\b{re.escape(module)}\b", text) is not None


@register(Severity.MEDIUM)
def check_intent_scope(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.intent_scope
    if not cfg.enabled:
        return None

    if not ctx.linked_issues:
        if cfg.require_linked_issue:
            return CheckResult(
                "intent_scope", False,
                "No linked issue — add 'Closes #123' so the change's intent is "
                "traceable",
            )
        return CheckResult("intent_scope", True, "")

    # Need both the repo's module vocabulary and the changed files to compare.
    # Missing either is "no data" → pass, never a false flag.
    if not ctx.repo_tree or not ctx.files:
        return CheckResult("intent_scope", True, "")

    modules = repo_modules(ctx.repo_tree, cfg)
    if not modules:
        return CheckResult("intent_scope", True, "")
    touched = touched_modules(ctx.files, modules)

    for issue in ctx.linked_issues:
        text = f"{issue.get('title', '')} {issue.get('body', '')}".lower()
        referenced = {m for m in modules if _mentions(text, m)}
        if referenced and not (referenced & touched):
            ref = ", ".join(sorted(referenced))
            touch = ", ".join(sorted(touched)) or "no recognized module"
            return CheckResult(
                "intent_scope", False,
                f"Linked issue #{issue.get('number')} is about [{ref}], but the "
                f"diff touches none of it (changed: {touch})",
            )

    return CheckResult("intent_scope", True, "")
