import hashlib
import re
from dataclasses import dataclass, field
from enum import IntEnum
from urllib.parse import quote

from pr_warden.agent.schemas import AttentionItem, DoneInput
from pr_warden.checks.registry import ATTENTION_THRESHOLD, CheckResult, Severity

# Retired: PRwarden no longer applies a generic status label, nor derives one.
# The verdict headline in the comment carries the overall read; specific facet
# labels (below) carry queue-level filtering. These two names are kept only so
# the applier can strip them from PRs labelled by an earlier version.
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


# Fallback when no per-repo config is threaded through (e.g. direct calls/tests).
# Mirrors AdvisoryEscalationConfig.threshold so behaviour is the same either way.
DEFAULT_ADVISORY_THRESHOLD = 3


@dataclass(frozen=True)
class ChecksSummary:
    """The deterministic checks reduced to the handful of facts every downstream
    decision needs — computed once, then read by the verdict ladder, the facet
    labels, and the needs-attention test. Without it, each of those re-derives
    "what failed and how badly" inline and they can quietly drift apart.
    """

    failed: tuple[CheckResult, ...]
    failed_names: frozenset[str]
    high_fail: tuple[CheckResult, ...]       # severity >= HIGH
    attention_plus: tuple[CheckResult, ...]  # severity >= ATTENTION_THRESHOLD
    advisory_count: int                      # failures below ATTENTION_THRESHOLD

    @classmethod
    def from_results(cls, results: list[CheckResult]) -> "ChecksSummary":
        failed = tuple(r for r in results if not r.passed)
        return cls(
            failed=failed,
            failed_names=frozenset(r.name for r in failed),
            high_fail=tuple(r for r in failed if r.severity >= Severity.HIGH),
            attention_plus=tuple(r for r in failed if r.severity >= ATTENTION_THRESHOLD),
            advisory_count=sum(1 for r in failed if r.severity < ATTENTION_THRESHOLD),
        )


def _advisory_escalates(advisory_count: int, advisory_threshold: int | None) -> bool:
    """True when enough advisory (sub-threshold) failures piled up to escalate.

    `advisory_threshold` None or <= 0 disables the rule.
    """
    if not advisory_threshold or advisory_threshold <= 0:
        return False
    return advisory_count >= advisory_threshold


def _needs_attention(summary: ChecksSummary, advisory_threshold: int | None) -> bool:
    """The single source of truth for the label decision.

    needs-attention when either a check at/above the attention threshold failed,
    OR enough advisory checks failed together to escalate. verdict_level and the
    comment banner both route through here so they can never disagree.
    """
    if summary.attention_plus:
        return True
    return _advisory_escalates(summary.advisory_count, advisory_threshold)


@dataclass(frozen=True)
class LinkContext:
    """What the renderer needs to turn a cited `path:line` into a GitHub link:
    the repo, the head commit, and the set of real paths so we only ever link to
    files that exist.

    `changed_paths` (a subset of `known_paths`) and `pr_number` let a cited
    location in a *changed* file link into the PR diff — straight to the change —
    rather than the whole file blob. Files that exist but weren't touched
    (repo-tree only) have no diff, so they still link to the blob at `sha`.
    """

    repo: str
    sha: str
    known_paths: frozenset[str]
    changed_paths: frozenset[str] = field(default_factory=frozenset)
    pr_number: int | None = None


# Match a LEADING `path:line` (or range) anchor; any trailing prose is kept as-is.
# Being lenient about trailing text means a location like
# "auth.py:21 (the fallback token)" still links the `auth.py:21` part.
_LOCATION_RE = re.compile(r"^(?P<path>[\w./-]+):(?P<line>\d+)(?:-(?P<end>\d+))?(?P<rest>.*)$")


def _diff_anchor(path: str, line: str | None) -> str:
    """The fragment for a file (and optional line) inside GitHub's PR diff view.

    GitHub anchors each file's diff by `diff-<sha256 of the path>`, and a line on
    the new (right) side by suffixing `R<line>`. Landing on the start line is the
    whole point of "where to focus", so a range collapses to its first line.
    """
    anchor = "diff-" + hashlib.sha256(path.encode("utf-8")).hexdigest()
    return anchor + (f"R{line}" if line else "")


def _location_url(link_ctx: LinkContext, path: str, line: str | None, end: str | None) -> str:
    # Changed file → into the PR diff, so the maintainer lands on the change itself.
    if path in link_ctx.changed_paths and link_ctx.pr_number is not None:
        return (
            f"https://github.com/{link_ctx.repo}/pull/{link_ctx.pr_number}"
            f"/files#{_diff_anchor(path, line)}"
        )
    # Unchanged file that still exists → the blob at head, with the line range.
    anchor = f"#L{line}" + (f"-L{end}" if end else "") if line else ""
    return f"https://github.com/{link_ctx.repo}/blob/{link_ctx.sha}/{quote(path, safe='/')}{anchor}"


def _linkify_location(location: str, link_ctx: LinkContext | None) -> str:
    """A cited location → a Markdown link to the line on GitHub, but only when the
    path resolves to a real file in the PR. Anything else stays plain `code` so a
    guessed/broken link never reaches a maintainer."""
    loc = location.strip()
    if link_ctx is None:
        return f"`{loc}`"

    m = _LOCATION_RE.match(loc)
    if m and m.group("path") in link_ctx.known_paths:
        path, line, end = m.group("path"), m.group("line"), m.group("end")
        span = f"{path}:{line}" + (f"-{end}" if end else "")
        anchor = f"#L{line}" + (f"-L{end}" if end else "")
        url = f"https://github.com/{link_ctx.repo}/blob/{link_ctx.sha}/{quote(path, safe='/')}{anchor}"
        link = f"[`{span}`]({url})"
        rest = m.group("rest").strip()
        return f"{link} {rest}" if rest else link

    if loc in link_ctx.known_paths:  # a bare path, no line
        url = f"https://github.com/{link_ctx.repo}/blob/{link_ctx.sha}/{quote(loc, safe='/')}"
        return f"[`{loc}`]({url})"
    return f"`{loc}`"


def _format_attention(items: list[AttentionItem], link_ctx: LinkContext | None) -> list[str]:
    """The top 3 spots to look, ranked by risk × centrality. Numbered because the
    order is the message — #1 is where a 30-second maintainer looks first. The
    rank drives the order but isn't printed (terse); each location links to the
    line when it resolves to a real file."""
    ranked = sorted(items, key=lambda it: -it.priority)[:3]
    lines = ["\n**Where to focus:**"]
    for i, it in enumerate(ranked, 1):
        lines.append(f"{i}. {_linkify_location(it.location, link_ctx)} — {it.why}")
    return lines


def format_agent_assessment(assessment: DoneInput, link_ctx: LinkContext | None = None) -> str:
    """Render the agent's read — kept deliberately short. When the PR is low-risk
    and there's nothing to surface, the summary alone suffices (or nothing, when
    the verdict already says Clean). Otherwise: summary → focus → questions."""
    lines = [assessment.summary.strip()]

    if assessment.attention:
        lines += _format_attention(assessment.attention, link_ctx)

    if assessment.open_questions:
        lines.append("\n**Questions:**")
        lines += [f"- {q}" for q in assessment.open_questions[:2]]

    return "\n".join(lines)


# Severity badge for the failing-checks table. Word + glyph so the signal
# survives in clients without emoji and tests can assert on the word.
_SEV_BADGE: dict[Severity, str] = {
    Severity.HIGH: "🔴 High",
    Severity.MEDIUM: "🟠 Medium",
    Severity.LOW: "🟡 Advisory",
}


def format_changes(
    prev_results: dict[str, dict],
    curr_results: list[CheckResult],
    prev_sha: str,
    curr_sha: str,
) -> str | None:
    """One-line "what changed since the last reviewed commit" for a returning
    reviewer. None when no check flipped, so a no-op push adds nothing.
    Deterministic checks only; the agent's read is not diffed (it would flap)."""
    prev_failed = {name for name, r in prev_results.items() if not r.get("passed", True)}
    curr_failed = {r.name for r in curr_results if not r.passed}
    newly_failing = sorted(curr_failed - prev_failed)
    newly_resolved = sorted(prev_failed - curr_failed)
    if not newly_failing and not newly_resolved:
        return None

    def _names(ns: list[str]) -> str:
        return ", ".join(n.replace("_", " ").title() for n in ns)

    bits = []
    if newly_failing:
        bits.append(f"❌ now failing: {_names(newly_failing)}")
    if newly_resolved:
        bits.append(f"✅ now resolved: {_names(newly_resolved)}")
    return f"**Since last review** (`{prev_sha[:7]}` → `{curr_sha[:7]}`): " + " · ".join(bits)


def _format_failing_checks(results: list[CheckResult]) -> str | None:
    """The failed deterministic checks as a compact table — only failures, highest
    severity first; passing checks are never shown. Returns None if nothing failed."""
    failed = sorted((r for r in results if not r.passed), key=lambda r: -int(r.severity))
    if not failed:
        return None
    rows = [
        f"| {r.name.replace('_', ' ').title()} | {_SEV_BADGE[r.severity]} | {r.reason} |"
        for r in failed
    ]
    return "\n".join(
        ["\n**Failing checks**", "", "| Check | Severity | Detail |", "|---|---|---|", *rows]
    )


def build_comment(
    results: list[CheckResult],
    agent: DoneInput | None = None,
    *,
    agent_complete: bool = True,
    advisory_threshold: int | None = DEFAULT_ADVISORY_THRESHOLD,
    changes: str | None = None,
    link_ctx: LinkContext | None = None,
) -> str:
    """One consolidated, deliberately short review.

    Verdict headline → the agent's brief read (when it ran) → a compact table of
    only the *failing* deterministic checks. The model authors the verdict; a
    HIGH-severity check still floors it to 🔴 so a leaked secret can't be buried.
    With no agent, the headline + the failing-checks table still inform.
    """
    verdict = render_verdict(
        results, agent, agent_complete=agent_complete,
        advisory_threshold=advisory_threshold, link_ctx=link_ctx,
    )
    parts = [verdict]
    if changes:
        parts.append(f"\n{changes}")

    if agent is not None:
        parts.append(f"\n{format_agent_assessment(agent, link_ctx)}")

    # The failing-checks table shows in both cases — it's the deterministic record
    # of what tripped (only failures; passing checks are never listed).
    failing = _format_failing_checks(results)
    if failing:
        parts.append(failing)

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
    summary = ChecksSummary.from_results(results)
    mix = _severity_mix(summary.failed)
    # Only trust agent fields from a run that actually finished; a force-finalized
    # fallback carries default-clean values that must never read as a real verdict.
    agent_ok = agent is not None and agent_complete

    def line(glyph: str, verdict: str, lead: str, *, tail: str = "") -> str:
        s = f"{glyph} **{verdict}** — {lead}"
        return f"{s} · {tail}" if tail else s

    # ── 🔴 hard concerns ──────────────────────────────────────────────────────
    if summary.high_fail:
        c = max(summary.high_fail, key=lambda r: r.severity)
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
    if _needs_attention(summary, advisory_threshold):
        if summary.attention_plus:
            c = max(summary.attention_plus, key=lambda r: r.severity)
            lead = f"{_check_name(c)}: {c.reason}"
        else:  # escalated purely on a pile-up of advisory nits
            lead = f"{len(summary.failed)} advisory checks piled up (≥{advisory_threshold} escalates)"
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
    if summary.failed:
        n = len(summary.failed)
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


# verdict_level → (glyph, title). The model picks the level; the app maps it to a
# consistent glyph and title word, and the model's `verdict` text follows.
_VERDICT_STYLE: dict[str, tuple[str, str]] = {
    "high": ("🔴", "High concern"),
    "attention": ("🟠", "Worth a look"),
    "minor": ("🟡", "Minor note"),
    "low": ("✅", "Clean"),
    "inconclusive": ("⚠️", "Inconclusive"),
}


def render_verdict(
    results: list[CheckResult],
    agent: DoneInput | None,
    *,
    agent_complete: bool,
    advisory_threshold: int | None,
    link_ctx: LinkContext | None = None,
) -> str:
    """The headline line. When the agent finished, the model's `verdict_level` +
    `verdict` ARE the headline — except a HIGH-severity deterministic check
    (leaked secret / critical-path) floors it to the deterministic 🔴 so the model
    can't bury a hard fact. Otherwise (no agent / incomplete) the deterministic
    ladder leads.
    """
    if agent is not None and agent_complete:
        high_fail = [r for r in results if not r.passed and r.severity >= Severity.HIGH]
        if high_fail:
            # Safety floor: a leaked secret / critical-path touch is not the model's
            # to downgrade. Use the deterministic HIGH headline verbatim.
            return build_verdict(
                results, agent, agent_complete=agent_complete,
                advisory_threshold=advisory_threshold, link_ctx=link_ctx,
            )
        glyph, title = _VERDICT_STYLE.get(agent.verdict_level, _VERDICT_STYLE["attention"])
        # For "Clean" / low, just show the glyph+title — no trailing chatter.
        if agent.verdict_level == "low":
            return f"{glyph} **{title}**"
        tail = _severity_mix([r for r in results if not r.passed])
        line = f"{glyph} **{title}** — {agent.verdict.strip()}"
        return f"{line} · {tail}" if tail else line

    # No agent, or it didn't finish → deterministic ladder.
    return build_verdict(
        results, agent, agent_complete=agent_complete,
        advisory_threshold=advisory_threshold, link_ctx=link_ctx,
    )


def pick_label(
    results: list[CheckResult],
    agent: DoneInput | None = None,
    *,
    agent_complete: bool = True,
    advisory_threshold: int | None = DEFAULT_ADVISORY_THRESHOLD,
) -> Concern:
    """The verdict's concern level — the same ladder that drives the headline.

    Recorded per run for `/stats` analytics. PRwarden no longer turns this into a
    status *label* (that's retired — see RETIRED_LABELS); the verdict headline
    carries the overall read and facet labels carry queue filtering. Exposing the
    level itself keeps analytics richer than the old binary clean/needs-attention:
    `Concern >= ATTENTION` is the boundary the old `needs-attention` label marked.

    With `agent=None` this reduces to the original checks-only rule.
    """
    level, _ = _concern(
        results, agent, agent_complete=agent_complete, advisory_threshold=advisory_threshold
    )
    return level


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
    agent_ok = agent is not None and agent_complete
    labels: list[str] = []

    # blocker = a serious problem, from either source: a HIGH-severity check, OR
    # the agent's own 🔴 high verdict. So a serious agent finding is labelled
    # seriously, not merely as intent-mismatch.
    high_check = any(not r.passed and r.severity >= Severity.HIGH for r in results)
    if high_check or (agent_ok and agent.verdict_level == "high"):
        labels.append(LABEL_BLOCKER)
    if summary.failed_names & _SECURITY_CHECKS:
        labels.append(LABEL_SECURITY)
    if summary.failed_names & _AI_CHECKS:
        labels.append(LABEL_AI_AUTHORED)
    if agent_ok and not agent.intent_matches_diff:
        labels.append(LABEL_INTENT_MISMATCH)

    return labels
