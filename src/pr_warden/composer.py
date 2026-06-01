import re
from dataclasses import dataclass
from enum import IntEnum
from urllib.parse import quote

from pr_warden.agent.schemas import AttentionItem, DoneInput
from pr_warden.checks.registry import ATTENTION_THRESHOLD, CheckResult, Severity

# Retired: PRwarden no longer applies a generic status label. The verdict
# headline in the comment carries the overall read; specific facet labels (below)
# carry queue-level filtering. These two are kept only so the applier can strip
# them from PRs labelled by an earlier version, and as an analytics value.
LABEL_CLEAN = "prwarden:clean"
LABEL_NEEDS_ATTENTION = "prwarden:needs-attention"

# Facet labels — the only labels PRwarden applies. Each is a specific, filterable
# signal a maintainer routes on in the PR list (security, claim≠diff, AI-authored,
# a hard blocker) — not a generic "the bot is worried" stamp.
LABEL_BLOCKER = "prwarden:blocker"              # a HIGH-severity check failed
LABEL_SECURITY = "prwarden:security"            # a security-kind check failed
LABEL_AI_AUTHORED = "prwarden:ai-authored"      # AI branch / commit-footer signal
LABEL_INTENT_MISMATCH = "prwarden:intent-mismatch"  # agent: diff ≠ stated intent

FACET_LABELS = frozenset(
    {LABEL_BLOCKER, LABEL_SECURITY, LABEL_AI_AUTHORED, LABEL_INTENT_MISMATCH}
)
RETIRED_LABELS = frozenset({LABEL_CLEAN, LABEL_NEEDS_ATTENTION})
# Everything the applier may remove from a PR: current facets + retired status
# labels. It only ever *adds* facets, so retired labels get cleaned up on next run.
MANAGED_LABELS = FACET_LABELS | RETIRED_LABELS

# Which failed checks drive the kind/provenance facets. Severity drives `blocker`.
_SECURITY_CHECKS = {"secret_leak", "critical_path"}
_AI_CHECKS = {"ai_branch", "ai_commit_footer"}


class Concern(IntEnum):
    """How worried the bot is, on one scale that drives both the headline verdict
    and the status label — so they can never disagree.

    `needs-attention` is exactly `Concern >= ATTENTION`. INFO (advisory nits or an
    inconclusive/low-confidence agent) deliberately does NOT escalate status: a
    pile of nits or an agent that timed out shouldn't flip the primary filter.
    """

    NONE = 0       # 🟢 clean / no flags
    INFO = 1       # 🟡 advisory-only · ⚠️ inconclusive — does not escalate status
    ATTENTION = 2  # 🟠 worth a look
    HIGH = 3       # 🔴 high concern

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


@dataclass(frozen=True)
class LinkContext:
    """What the renderer needs to turn a cited `path:line` into a GitHub link:
    the repo, the head commit the comment is about, and the set of real paths
    (changed files ∪ repo tree) so we only ever link to files that exist.
    """

    repo: str                       # "owner/name"
    sha: str                        # PR head commit the comment is rendered for
    known_paths: frozenset[str]


# A cited location like `path/to/file.py:55` or `file.py:55-60`. A bare path (no
# line) is handled separately; free-text citations match nothing and stay plain.
_LOCATION_RE = re.compile(r"^(?P<path>[\w./-]+):(?P<line>\d+)(?:-(?P<end>\d+))?$")


def _linkify_location(location: str, link_ctx: LinkContext | None) -> str:
    """Render a cited location as a Markdown link to the line on GitHub — but only
    when it cleanly resolves to a real file in this PR. Anything else stays a plain
    `code` span, so a broken or guessed link can never erode trust. Either way it
    adds zero visible length: the maintainer just clicks instead of hunting.
    """
    code = f"`{location}`"
    if link_ctx is None:
        return code

    loc = location.strip()
    m = _LOCATION_RE.match(loc)
    if m:
        path, line, end = m.group("path"), m.group("line"), m.group("end")
        anchor = f"#L{line}" + (f"-L{end}" if end else "")
    elif loc in link_ctx.known_paths:        # a bare path, no line number
        path, anchor = loc, ""
    else:
        return code

    if path not in link_ctx.known_paths:     # don't link a file that isn't here
        return code
    url = (
        f"https://github.com/{link_ctx.repo}/blob/{link_ctx.sha}/"
        f"{quote(path, safe='/')}{anchor}"
    )
    return f"[{code}]({url})"


def _format_attention(
    items: list[AttentionItem], link_ctx: LinkContext | None = None
) -> list[str]:
    """Render the attention map: the top 3 spots, ranked by risk × centrality.

    Numbered (not bulleted) because the order is the message — spot #1 is where a
    30-second maintainer should look first. Sorting is stable, so the agent's own
    ordering breaks ties between items of equal priority. Each location links
    straight to the line on GitHub when it resolves to a real file.
    """
    ranked = sorted(items, key=lambda it: -it.priority)[:3]
    lines = ["\n**👀 Attention map** — ranked by risk × centrality:"]
    for i, it in enumerate(ranked, 1):
        lines.append(
            f"{i}. {_linkify_location(it.location, link_ctx)} — {it.why} "
            f"_(risk {it.risk} · centrality {it.centrality})_"
        )
    return lines


def format_agent_assessment(
    assessment: DoneInput, link_ctx: LinkContext | None = None
) -> str:
    """Render the agent's structured assessment as a Markdown comment section."""
    lines = [assessment.summary.strip()]

    if not assessment.intent_matches_diff:
        reason = assessment.intent_mismatch_reason or "no reason given"
        lines.append(f"\n**⚠️ Intent vs. diff mismatch:** {reason}")

    if assessment.attention:
        lines += _format_attention(assessment.attention, link_ctx)

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
    agent_complete: bool = True,
    advisory_threshold: int | None = DEFAULT_ADVISORY_THRESHOLD,
    link_ctx: LinkContext | None = None,
) -> str:
    # `agent_complete` defaults True so a caller that hands over an assessment is
    # taken at face value; the live pipeline passes False for a force-finalized
    # run so the verdict shows ⚠️ Inconclusive instead of a false 🟢.
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

    verdict = build_verdict(
        results, agent, agent_complete=agent_complete,
        advisory_threshold=advisory_threshold, link_ctx=link_ctx,
    )
    parts = ["## PRwarden Review\n", verdict, "", table]

    if summary:
        parts.append(f"\n### Summary\n{summary}")

    if agent is not None:
        parts.append(f"\n### Agent Review\n{format_agent_assessment(agent, link_ctx)}")

    parts.append("\n\n---\n*Powered by PRwarden · `/prwarden recheck` to re-run*")
    return "\n".join(parts)


# Verdict tuning. An attention spot is headline-worthy at risk × centrality ≥ 6
# (i.e. at least high×medium); below that it's listed but doesn't lead. Below
# this confidence the agent's read is treated as tentative, not a verdict.
_HIGH_ATTENTION_PRIORITY = 6
_LOW_CONFIDENCE = 0.4


def _check_name(r: CheckResult) -> str:
    return r.name.replace("_", " ").title()


def _severity_mix(failed: list[CheckResult]) -> str:
    """Compact severity census of the failures, e.g. `1 high, 2 advisory`."""
    counts = {sev: sum(1 for r in failed if r.severity == sev) for sev in Severity}
    bits = []
    if counts[Severity.HIGH]:
        bits.append(f"{counts[Severity.HIGH]} high")
    if counts[Severity.MEDIUM]:
        bits.append(f"{counts[Severity.MEDIUM]} to review")
    if counts[Severity.LOW]:
        bits.append(f"{counts[Severity.LOW]} advisory")
    return ", ".join(bits)


def _concern(
    results: list[CheckResult],
    agent: DoneInput | None,
    *,
    agent_complete: bool,
    advisory_threshold: int | None,
    link_ctx: LinkContext | None = None,
) -> tuple[Concern, str]:
    """The single source of truth for both the verdict headline and the status
    label: read both layers, lead with the worst, return (level, headline).

    Unlike the old status banner (which only saw the checks), this surfaces the
    agent's claim-vs-diff read and top attention spot — so a clean-checks PR
    whose diff doesn't match its intent reads as a concern instead of "✅", and
    (since the label is `needs-attention` iff `level >= ATTENTION`) the queue
    flags it too.

    Precedence, most → least serious:
      🔴 HIGH        a HIGH check failed, or the agent says diff ≠ intent
      🟠 ATTENTION   a high-priority attention spot, or a MEDIUM+ check
      ⚠️ INFO        agent didn't finish / is low-confidence (downgrades a would-be
                     🟡/🟢 — never softens a 🔴/🟠, never escalates the label)
      🟡 INFO        advisory-only nits, nothing blocking
      🟢 NONE        agent ran and agrees / no automated flags

    It is a triage read, never a ruling — it never tells the maintainer to merge.
    """
    failed = [r for r in results if not r.passed]
    high_fail = [r for r in failed if r.severity >= Severity.HIGH]
    mix = _severity_mix(failed)
    # Only trust agent fields from a run that actually finished; a force-finalized
    # fallback carries default-clean values that must never read as a real verdict.
    agent_ok = agent is not None and agent_complete

    def line(glyph: str, verdict: str, lead: str, *, tail: str = "") -> str:
        s = f"{glyph} **{verdict}** — {lead}"
        return f"{s} · {tail}" if tail else s

    # ── 🔴 hard concerns ──────────────────────────────────────────────────────
    if high_fail:
        c = max(high_fail, key=lambda r: r.severity)
        return Concern.HIGH, line("🔴", "High concern", f"{_check_name(c)}: {c.reason}", tail=mix)
    if agent_ok and not agent.intent_matches_diff:
        reason = agent.intent_mismatch_reason or "no reason given"
        return Concern.HIGH, line(
            "🔴", "High concern",
            f"the diff doesn't match the stated intent — {reason}", tail=mix,
        )

    # ── 🟠 worth a look ───────────────────────────────────────────────────────
    top = (
        max(agent.attention, key=lambda a: a.priority)
        if agent_ok and agent.attention
        else None
    )
    if top is not None and top.priority >= _HIGH_ATTENTION_PRIORITY:
        return Concern.ATTENTION, line(
            "🟠", "Worth a look",
            f"start at {_linkify_location(top.location, link_ctx)} — {top.why}", tail=mix,
        )
    if _needs_attention(results, advisory_threshold):
        medplus = [r for r in failed if r.severity >= ATTENTION_THRESHOLD]
        if medplus:
            c = max(medplus, key=lambda r: r.severity)
            lead = f"{_check_name(c)}: {c.reason}"
        else:  # escalated purely on a pile-up of advisory nits
            lead = f"{len(failed)} advisory checks piled up (≥{advisory_threshold} escalates)"
        return Concern.ATTENTION, line("🟠", "Worth a look", lead, tail=mix)

    # ── ⚠️ inconclusive (downgrades only a would-be 🟡/🟢; never escalates) ────
    if agent is not None and not agent_complete:
        return Concern.INFO, line(
            "⚠️", "Inconclusive",
            "the agent didn't finish its review — rely on the checks and verify manually",
            tail=mix,
        )
    if agent_ok and agent.confidence < _LOW_CONFIDENCE:
        q = agent.open_questions[0] if agent.open_questions else "the points below"
        return Concern.INFO, line(
            "⚠️", "Inconclusive",
            f"agent low-confidence ({agent.confidence:.0%}) — treat the read as "
            f"tentative; verify {q}",
            tail=mix,
        )

    # ── 🟡 advisory-only flags ────────────────────────────────────────────────
    if failed:
        n = len(failed)
        return Concern.INFO, line(
            "🟡", "Minor flags", f"{n} advisory {'check' if n == 1 else 'checks'}, nothing blocking"
        )

    # ── 🟢 clean ──────────────────────────────────────────────────────────────
    if agent_ok:
        return Concern.NONE, line(
            "🟢", "Looks low-risk", "claim matches the diff, no flags — still your call"
        )
    return Concern.NONE, line(
        "🟢", "No automated flags", "checks only — no deep review on this repo"
    )


def build_verdict(
    results: list[CheckResult],
    agent: DoneInput | None,
    *,
    agent_complete: bool,
    advisory_threshold: int | None,
    link_ctx: LinkContext | None = None,
) -> str:
    """The one-line judgment headline (see `_concern` for the full ladder)."""
    return _concern(
        results, agent, agent_complete=agent_complete,
        advisory_threshold=advisory_threshold, link_ctx=link_ctx,
    )[1]


def pick_label(
    results: list[CheckResult],
    agent: DoneInput | None = None,
    *,
    agent_complete: bool = True,
    advisory_threshold: int | None = DEFAULT_ADVISORY_THRESHOLD,
) -> str:
    """The status label, driven by the same concern level as the verdict headline.

    needs-attention iff `Concern >= ATTENTION`: a MEDIUM+ check failed, advisories
    piled up, OR — when the agent finished — it flagged the diff as not matching
    the stated intent or surfaced a high-priority (risk × centrality) spot. So the
    label and the headline can never disagree.

    A single advisory (LOW) failure, or an inconclusive/low-confidence agent, keeps
    the PR `clean` — a nit or a timed-out agent must not raise the same flag as a
    leaked secret. With `agent=None` this reduces to the original checks-only rule.
    """
    level, _ = _concern(
        results, agent, agent_complete=agent_complete, advisory_threshold=advisory_threshold
    )
    return LABEL_NEEDS_ATTENTION if level >= Concern.ATTENTION else LABEL_CLEAN


def pick_facet_labels(
    results: list[CheckResult],
    agent: DoneInput | None = None,
    *,
    agent_complete: bool = True,
) -> list[str]:
    """The facet labels to apply — specific, filterable signals, no generic status.

    Additive and independent: a PR off an AI-named branch with no real flags gets
    `ai-authored` and nothing else. The intent-mismatch facet only fires when the
    agent actually finished (a force-finalized run is not trusted).
    """
    failed = {r.name for r in results if not r.passed}
    labels: list[str] = []

    if any(not r.passed and r.severity >= Severity.HIGH for r in results):
        labels.append(LABEL_BLOCKER)
    if failed & _SECURITY_CHECKS:
        labels.append(LABEL_SECURITY)
    if failed & _AI_CHECKS:
        labels.append(LABEL_AI_AUTHORED)
    if agent is not None and agent_complete and not agent.intent_matches_diff:
        labels.append(LABEL_INTENT_MISMATCH)

    return labels
