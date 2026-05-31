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

# Fallback when no per-repo config is threaded through (e.g. direct calls/tests).
# Mirrors AdvisoryEscalationConfig.threshold so behaviour is the same either way.
DEFAULT_ADVISORY_THRESHOLD = 3


def _advisory_escalates(failed: list[CheckResult], advisory_threshold: int | None) -> bool:
    """True when enough advisory (sub-threshold) failures piled up to escalate.

    `advisory_threshold` None or <= 0 disables the rule.
    """
    if not advisory_threshold or advisory_threshold <= 0:
        return False
    advisories = sum(1 for r in failed if r.severity < ATTENTION_THRESHOLD)
    return advisories >= advisory_threshold


def _needs_attention(
    results: list[CheckResult], advisory_threshold: int | None
) -> bool:
    """The single source of truth for the label decision.

    needs-attention when either a check at/above the attention threshold failed,
    OR enough advisory checks failed together to escalate. pick_label and the
    comment banner both route through here so they can never disagree.
    """
    failed = [r for r in results if not r.passed]
    if any(r.severity >= ATTENTION_THRESHOLD for r in failed):
        return True
    return _advisory_escalates(failed, advisory_threshold)


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
    *,
    advisory_threshold: int | None = DEFAULT_ADVISORY_THRESHOLD,
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

    parts = ["## PRwarden Review\n", _status_banner(results, advisory_threshold), "", table]

    if summary:
        parts.append(f"\n### Summary\n{summary}")

    if agent is not None:
        parts.append(f"\n### Agent Review\n{format_agent_assessment(agent)}")

    parts.append("\n\n---\n*Powered by PRwarden · `/prwarden recheck` to re-run*")
    return "\n".join(parts)


def _status_banner(results: list[CheckResult], advisory_threshold: int | None) -> str:
    """One line above the table summarizing the failure mix by severity.

    Makes the headline obvious: a clean PR, a PR that only tripped advisories
    (still clean), one escalated because too many advisories piled up, or one
    with real flags — and how many of each.
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
    mix = ", ".join(bits)

    if _needs_attention(results, advisory_threshold):
        # Distinguish a real flag from a pile-of-nits escalation, so the
        # maintainer understands *why* it needs attention.
        escalated_only = not any(r.severity >= ATTENTION_THRESHOLD for r in failed)
        note = f" (≥{advisory_threshold} advisories escalates)" if escalated_only else ""
        return f"**⚠️ Needs attention** — {mix}{note}."
    # Only a few advisories tripped: status stays clean, but we still surface them.
    return f"**✅ Clean** — {mix} (advisory only, does not affect status)."


def pick_label(
    results: list[CheckResult],
    *,
    advisory_threshold: int | None = DEFAULT_ADVISORY_THRESHOLD,
) -> str:
    """needs-attention iff a MEDIUM+ check failed, or enough advisories piled up.

    A single advisory (LOW) failure keeps the PR `clean` — a branch-name nit must
    not raise the same flag as a leaked secret. But `advisory_threshold` or more
    advisory failures together (the slop signature) escalates to needs-attention.
    """
    return (
        LABEL_NEEDS_ATTENTION
        if _needs_attention(results, advisory_threshold)
        else LABEL_CLEAN
    )
