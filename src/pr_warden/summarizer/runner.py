"""Summarizer orchestration.

For now this is a single Haiku agent: read the diff statically, ask the model
for a structured summary, parse it. The seams (`call_model`, the system prompt,
the schema) are factored out so this can later split into several specialised
agents (intent match, completeness, blast radius) that share the same runtime.
"""

from __future__ import annotations

import json

import structlog
from pydantic import ValidationError

from pr_warden.checks.registry import CheckContext
from pr_warden.summarizer.client import LLMResult, call_model
from pr_warden.summarizer.diff import build_diff_text
from pr_warden.summarizer.prompts import load_prompt
from pr_warden.summarizer.schemas import PRSummary

log = structlog.get_logger()

# Loaded once at import; cheap and avoids re-reading the file per PR.
_SYSTEM_PROMPT = load_prompt("summarizer_v1.md")

_RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def _strip_code_fences(text: str) -> str:
    """Models often wrap JSON in ```json ... ``` fences. Strip them."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        t = t.removeprefix("json").lstrip("\n")
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


def parse_summary(text: str, cost_usd: float) -> PRSummary | None:
    """Parse the model's text into a PRSummary, or None if it isn't valid."""
    raw = _strip_code_fences(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("summarizer.parse_error", output=raw[:200])
        return None
    if not isinstance(data, dict):
        log.warning("summarizer.parse_not_object")
        return None
    data["cost_usd"] = cost_usd
    try:
        return PRSummary.model_validate(data)
    except ValidationError as e:
        log.warning("summarizer.schema_error", error=str(e))
        return None


def _build_user_message(ctx: CheckContext, diff: str) -> str:
    pr = ctx.pr
    body = (pr.body or "").strip() or "(no description provided)"
    return (
        f"PR Title: {pr.title}\n\n"
        f"PR Description:\n{body}\n\n"
        f"Diff:\n{diff}"
    )


async def summarize_pr(
    ctx: CheckContext,
    *,
    api_key: str,
    model: str,
    call=call_model,
) -> PRSummary | None:
    """Produce a structured summary for a PR, or None if it can't/shouldn't run.

    Degrades gracefully: missing key, empty diff, API error, or unparseable
    output all return None so the caller just posts the deterministic table.
    `call` is injectable for testing.
    """
    if not api_key:
        log.info("summarizer.skipped", reason="no_api_key")
        return None

    diff = build_diff_text(ctx.files)
    if not diff.strip():
        log.info("summarizer.skipped", reason="empty_diff")
        return None

    user = _build_user_message(ctx, diff)
    try:
        result: LLMResult = await call(
            api_key=api_key,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user,
        )
    except Exception:
        log.exception("summarizer.call_failed")
        return None

    return parse_summary(result.text, result.cost_usd)


def format_summary(summary: PRSummary) -> str:
    """Render a PRSummary as the Markdown block that slots into the PR comment."""
    risk = summary.risk
    lines = [
        summary.summary,
        "",
        f"**Risk:** {_RISK_EMOJI.get(risk, '')} {risk}",
    ]
    if summary.key_changes:
        lines.append("\n**Key changes:**")
        lines += [f"- {c}" for c in summary.key_changes]
    if summary.reviewer_focus:
        lines.append("\n**Reviewer focus:**")
        lines += [f"- {c}" for c in summary.reviewer_focus]
    return "\n".join(lines)
