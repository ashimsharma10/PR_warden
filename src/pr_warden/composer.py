from pr_warden.agent.schemas import DoneInput
from pr_warden.checks.registry import CheckResult

LABEL_CLEAN = "prwarden:clean"
LABEL_NEEDS_ATTENTION = "prwarden:needs-attention"


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
    rows = []
    for r in results:
        icon = "✅" if r.passed else "❌"
        detail = r.reason if not r.passed else "—"
        name = r.name.replace("_", " ").title()
        rows.append(f"| {name} | {icon} | {detail} |")

    table = "\n".join(
        [
            "| Check | Status | Detail |",
            "|-------|--------|--------|",
            *rows,
        ]
    )

    parts = ["## PRwarden Review\n", table]

    if summary:
        parts.append(f"\n### Summary\n{summary}")

    if agent is not None:
        parts.append(f"\n### Agent Review\n{format_agent_assessment(agent)}")

    parts.append("\n\n---\n*Powered by PRwarden · `/prwarden recheck` to re-run*")
    return "\n".join(parts)


def pick_label(results: list[CheckResult]) -> str:
    return LABEL_CLEAN if all(r.passed for r in results) else LABEL_NEEDS_ATTENTION
