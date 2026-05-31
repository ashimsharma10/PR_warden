from pr_warden.agent.schemas import DoneInput
from pr_warden.checks.registry import ATTENTION_THRESHOLD, CheckResult, Severity

LABEL_CLEAN = "prwarden:clean"
LABEL_NEEDS_ATTENTION = "prwarden:needs-attention"

# How each severity renders in the comment. Word + glyph so the signal survives
# in clients that don't show emoji and so tests can assert on the word.
_SEVERITY_BADGE: dict[Severity, str] = {
    Severity.HIGH: "🔴 High",
    Severity.MEDIUM: "🟠 Medium",
    Severity.LOW: "🟡 Advisory",
}


def format_agent_assessment(assessment: DoneInput) -> str:
    """Render the agent's structured assessment as a Markdown comment section."""
    lines = [assessment.summary.strip()]

    if not assessment.intent_matches_diff:
        reason = assessment.intent_mismatch_reason or "no reason given"
        lines.append(f"\n**⚠️ Intent vs. diff mismatch:** {reason}")

    if assessment.notable:
        lines.append("\n**Notable:**")
        lines += [f"- {n}" for n in assessment.notable]

    if assessment.open_questions:
        lines.append("\n**Open questions:**")
        lines += [f"- {q}" for q in assessment.open_questions]

    lines.append(f"\n*Confidence: {assessment.confidence:.0%}*")
    return "\n".join(lines)


def build_comment(
    results: list[CheckResult],
    summary: str | None = None,
    agent: DoneInput | None = None,
) -> str:
    # Failures first, highest severity first; passing checks keep their order
    # after. A maintainer should see a leaked secret before a branch-name nit.
    ordered = sorted(
        results,
        key=lambda r: (r.passed, -int(r.severity)),
    )

    rows = []
    for r in ordered:
        icon = "✅" if r.passed else "❌"
        # Severity is only meaningful for a failure; a passing check is just "—".
        sev = _SEVERITY_BADGE[r.severity] if not r.passed else "—"
        detail = r.reason if not r.passed else "—"
        name = r.name.replace("_", " ").title()
        rows.append(f"| {name} | {icon} | {sev} | {detail} |")

    table = "\n".join(
        [
            "| Check | Status | Severity | Detail |",
            "|-------|--------|----------|--------|",
            *rows,
        ]
    )

    parts = ["## PRwarden Review\n", _status_banner(results), "", table]

    if summary:
        parts.append(f"\n### Summary\n{summary}")

    if agent is not None:
        parts.append(f"\n### Agent Review\n{format_agent_assessment(agent)}")

    parts.append("\n\n---\n*Powered by PRwarden · `/prwarden recheck` to re-run*")
    return "\n".join(parts)


def _status_banner(results: list[CheckResult]) -> str:
    """One line above the table summarizing the failure mix by severity.

    Makes the headline obvious: a clean PR, a PR that only tripped advisories
    (still clean), or one with real flags — and how many of each.
    """
    failed = [r for r in results if not r.passed]
    if not failed:
        return "**✅ Clean** — all checks passed."

    counts = {sev: sum(1 for r in failed if r.severity == sev) for sev in Severity}
    bits = []
    if counts[Severity.HIGH]:
        bits.append(f"🔴 {counts[Severity.HIGH]} high")
    if counts[Severity.MEDIUM]:
        bits.append(f"🟠 {counts[Severity.MEDIUM]} to review")
    if counts[Severity.LOW]:
        bits.append(f"🟡 {counts[Severity.LOW]} advisory")

    blocking = any(r.severity >= ATTENTION_THRESHOLD for r in failed)
    if blocking:
        return f"**⚠️ Needs attention** — {', '.join(bits)}."
    # Only advisories tripped: status stays clean, but we still surface the nits.
    return f"**✅ Clean** — {', '.join(bits)} (advisory only, does not affect status)."


def pick_label(results: list[CheckResult]) -> str:
    """needs-attention iff a check at or above the attention threshold failed.

    Advisory (LOW) failures alone keep the PR `clean` — a branch-name nit must
    not raise the same flag as a leaked secret, or the flag becomes noise.
    """
    if any(not r.passed and r.severity >= ATTENTION_THRESHOLD for r in results):
        return LABEL_NEEDS_ATTENTION
    return LABEL_CLEAN
