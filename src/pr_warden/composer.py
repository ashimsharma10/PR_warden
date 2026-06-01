from pr_warden.agent.schemas import AttentionItem, DoneInput
from pr_warden.checks.registry import ATTENTION_THRESHOLD, CheckResult, Severity

LABEL_CLEAN = "prwarden:clean"
LABEL_NEEDS_ATTENTION = "prwarden:needs-attention"

# Facet labels — additive, applied on top of the one status label above. Each is
# a routing/filtering signal a maintainer can't get from clean/needs-attention
# alone (a blocker vs. a pile of nits, a security touch, an AI-authored PR, a
# claim≠diff slop signal).
LABEL_BLOCKER = "prwarden:blocker"              # a HIGH-severity check failed
LABEL_SECURITY = "prwarden:security"            # a security-kind check failed
LABEL_AI_AUTHORED = "prwarden:ai-authored"      # AI branch / commit-footer signal
LABEL_INTENT_MISMATCH = "prwarden:intent-mismatch"  # agent: diff ≠ stated intent

# Every label the bot manages. The applier reconciles a PR against this set —
# removing any managed label no longer applicable — so it MUST list all of them.
MANAGED_LABELS = frozenset(
    {
        LABEL_CLEAN,
        LABEL_NEEDS_ATTENTION,
        LABEL_BLOCKER,
        LABEL_SECURITY,
        LABEL_AI_AUTHORED,
        LABEL_INTENT_MISMATCH,
    }
)

# Which failed checks drive the kind/provenance facets. Severity drives `blocker`
# (read off CheckResult.severity), so it needs no name list.
_SECURITY_CHECKS = {"secret_leak", "critical_path"}
_AI_CHECKS = {"ai_branch", "ai_commit_footer"}

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


def _format_attention(items: list[AttentionItem]) -> list[str]:
    """Render the attention map: the top 3 spots, ranked by risk × centrality.

    Numbered (not bulleted) because the order is the message — spot #1 is where a
    30-second maintainer should look first. Sorting is stable, so the agent's own
    ordering breaks ties between items of equal priority.
    """
    ranked = sorted(items, key=lambda it: -it.priority)[:3]
    lines = ["\n**👀 Attention map** — ranked by risk × centrality:"]
    for i, it in enumerate(ranked, 1):
        lines.append(
            f"{i}. `{it.location}` — {it.why} "
            f"_(risk {it.risk} · centrality {it.centrality})_"
        )
    return lines


def format_agent_assessment(assessment: DoneInput) -> str:
    """Render the agent's structured assessment as a Markdown comment section."""
    lines = [assessment.summary.strip()]

    if not assessment.intent_matches_diff:
        reason = assessment.intent_mismatch_reason or "no reason given"
        lines.append(f"\n**⚠️ Intent vs. diff mismatch:** {reason}")

    if assessment.attention:
        lines += _format_attention(assessment.attention)

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


def pick_labels(
    results: list[CheckResult],
    agent: DoneInput | None = None,
    *,
    advisory_threshold: int | None = DEFAULT_ADVISORY_THRESHOLD,
) -> list[str]:
    """The full label set for a PR: the one status label plus any facets.

    Element 0 is always the status label (clean/needs-attention) from
    `pick_label`; facets are appended in a stable order. Facets are additive and
    independent of status — e.g. a PR off an AI-named branch with no real flags
    stays `clean` but still carries `ai-authored`.
    """
    labels = [pick_label(results, advisory_threshold=advisory_threshold)]
    failed = {r.name for r in results if not r.passed}

    if any(not r.passed and r.severity >= Severity.HIGH for r in results):
        labels.append(LABEL_BLOCKER)
    if failed & _SECURITY_CHECKS:
        labels.append(LABEL_SECURITY)
    if failed & _AI_CHECKS:
        labels.append(LABEL_AI_AUTHORED)
    if agent is not None and not agent.intent_matches_diff:
        labels.append(LABEL_INTENT_MISMATCH)

    return labels
