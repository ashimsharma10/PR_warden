from pr_warden.agent.schemas import AttentionItem, DoneInput
from pr_warden.checks.registry import CheckResult, Severity
from pr_warden.composer import (
    FACET_LABELS,
    LABEL_AI_AUTHORED,
    LABEL_BLOCKER,
    LABEL_CLEAN,
    LABEL_INTENT_MISMATCH,
    LABEL_NEEDS_ATTENTION,
    LABEL_SECURITY,
    MANAGED_LABELS,
    build_comment,
    format_agent_assessment,
    pick_facet_labels,
    pick_label,
)


def _assessment(**kwargs) -> DoneInput:
    defaults = dict(
        summary="Guards the charge behind an already_charged check.",
        files_touched=["payments.py"],
        intent_matches_diff=True,
        intent_mismatch_reason="",
        attention=[
            AttentionItem(
                location="payments.py:42",
                why="No test covers the retry path; a double-charge would be silent",
                risk="high",
                centrality="medium",
            )
        ],
        open_questions=[],
        confidence=0.8,
    )
    defaults.update(kwargs)
    return DoneInput(**defaults)


def test_comment_contains_header():
    results = [CheckResult("title_quality", True, "")]
    assert "## PRwarden Review" in build_comment(results)


def test_comment_shows_pass_icon():
    results = [CheckResult("title_quality", True, "")]
    assert "✅" in build_comment(results)


def test_comment_shows_fail_icon_and_reason():
    results = [CheckResult("title_quality", False, "Title is a single word")]
    comment = build_comment(results)
    assert "❌" in comment
    assert "Title is a single word" in comment


def test_comment_all_passed_no_reasons():
    results = [
        CheckResult("title_quality", True, ""),
        CheckResult("description", True, ""),
        CheckResult("pr_size", True, "4 files, 100 lines"),
    ]
    comment = build_comment(results)
    # No agent ran → the verdict is honest that this is checks-only.
    assert "No automated flags" in comment
    assert "checks only" in comment
    assert "❌" not in comment


def test_comment_mixed():
    results = [
        CheckResult("title_quality", False, "Generic single-word title"),
        CheckResult("description", True, ""),
    ]
    comment = build_comment(results)
    assert "✅" in comment
    assert "❌" in comment
    assert "Generic single-word title" in comment


def test_comment_has_no_separate_summary_section():
    # The agent summary is the only summary; no second `### Summary` block.
    comment = build_comment([CheckResult("title_quality", True, "")], agent=_assessment())
    assert "### Summary" not in comment
    assert "Guards the charge" in comment  # the agent's summary still shows


def test_comment_footer():
    comment = build_comment([CheckResult("a", True, "")])
    assert "/prwarden recheck" in comment


def test_comment_includes_agent_section():
    results = [CheckResult("title_quality", True, "")]
    comment = build_comment(results, agent=_assessment())
    assert "### Agent Review" in comment
    assert "Guards the charge" in comment
    assert "Attention map" in comment
    assert "payments.py:42" in comment
    assert "double-charge would be silent" in comment
    assert "Confidence: 80%" in comment


def test_comment_no_agent_section_when_absent():
    comment = build_comment([CheckResult("a", True, "")])
    assert "### Agent Review" not in comment


def test_format_agent_assessment_flags_intent_mismatch():
    out = format_agent_assessment(
        _assessment(intent_matches_diff=False, intent_mismatch_reason="Adds telemetry, not a fix")
    )
    assert "Intent vs. diff mismatch" in out
    assert "Adds telemetry, not a fix" in out


def test_format_agent_assessment_renders_open_questions():
    out = format_agent_assessment(_assessment(open_questions=["Is the lock released on error?"]))
    assert "Open questions" in out
    assert "Is the lock released on error?" in out


# ── Attention map ─────────────────────────────────────────────────────────────


def _item(loc: str, risk: str, centrality: str) -> AttentionItem:
    return AttentionItem(location=loc, why="why", risk=risk, centrality=centrality)


def test_attention_map_ranks_by_risk_times_centrality():
    # high×high (9) must render above high×low (3), regardless of input order.
    out = format_agent_assessment(
        _assessment(attention=[_item("leaf.py:1", "high", "low"), _item("core.py:2", "high", "high")])
    )
    assert out.index("core.py:2") < out.index("leaf.py:1")


def test_attention_map_caps_at_three():
    items = [_item(f"f{i}.py:{i}", "low", "low") for i in range(5)]
    out = format_agent_assessment(_assessment(attention=items))
    assert "3. `" in out
    assert "4. `" not in out  # only the top 3 spend the maintainer's eyeballs


def test_attention_map_tie_preserves_agent_order():
    # Equal priority → stable sort keeps the agent's own ordering.
    out = format_agent_assessment(
        _assessment(attention=[_item("first.py:1", "medium", "medium"), _item("second.py:2", "medium", "medium")])
    )
    assert out.index("first.py:1") < out.index("second.py:2")


def test_attention_item_normalizes_case():
    it = AttentionItem(location="x:1", why="y", risk="High", centrality=" LOW ")
    assert it.risk == "high"
    assert it.centrality == "low"
    assert it.priority == 3


def test_pick_label_all_pass():
    results = [CheckResult("a", True, ""), CheckResult("b", True, "")]
    assert pick_label(results) == LABEL_CLEAN


def test_pick_label_any_fail():
    results = [CheckResult("a", True, ""), CheckResult("b", False, "reason")]
    assert pick_label(results) == LABEL_NEEDS_ATTENTION


def test_pick_label_all_fail():
    results = [CheckResult("a", False, "x"), CheckResult("b", False, "y")]
    assert pick_label(results) == LABEL_NEEDS_ATTENTION


# ── Facet labels (the only labels applied) ────────────────────────────────────


def test_facets_none_on_clean_pr():
    assert pick_facet_labels([CheckResult("title_quality", True, "")]) == []


def test_facets_no_generic_status_label():
    # The binary status labels are retired — never returned for application.
    results = [CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH)]
    labels = pick_facet_labels(results, _assessment(intent_matches_diff=False))
    assert LABEL_CLEAN not in labels
    assert LABEL_NEEDS_ATTENTION not in labels


def test_facets_high_failure_sets_blocker_and_security():
    results = [CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH)]
    labels = pick_facet_labels(results)
    assert LABEL_BLOCKER in labels
    assert LABEL_SECURITY in labels


def test_facets_ai_branch_sets_provenance():
    results = [CheckResult("ai_branch", False, "claude- branch", severity=Severity.LOW)]
    assert pick_facet_labels(results) == [LABEL_AI_AUTHORED]


def test_facets_intent_mismatch_only_from_finished_agent():
    clean = [CheckResult("title_quality", True, "")]
    assert LABEL_INTENT_MISMATCH not in pick_facet_labels(clean, None)
    assert LABEL_INTENT_MISMATCH in pick_facet_labels(clean, _assessment(intent_matches_diff=False))
    # A force-finalized run is not trusted to set the facet.
    assert LABEL_INTENT_MISMATCH not in pick_facet_labels(
        clean, _assessment(intent_matches_diff=False), agent_complete=False
    )


def test_managed_labels_strips_retired_status_labels():
    # The applier must be able to remove the old binary labels left by older runs.
    assert {LABEL_CLEAN, LABEL_NEEDS_ATTENTION} <= MANAGED_LABELS
    assert FACET_LABELS <= MANAGED_LABELS


# ── Verdict headline ──────────────────────────────────────────────────────────


def _clean() -> list:
    return [CheckResult("title_quality", True, ""), CheckResult("pr_size", True, "")]


def test_verdict_intent_mismatch_headlines_over_clean_checks():
    # The marquee fix: every check passes, but the agent caught claim ≠ diff.
    comment = build_comment(
        _clean(), agent=_assessment(intent_matches_diff=False, intent_mismatch_reason="adds logging, not a fix")
    )
    assert "High concern" in comment
    assert "doesn't match the stated intent" in comment
    assert "adds logging, not a fix" in comment


def test_verdict_secret_beats_intent_mismatch():
    # Two 🔴 signals → the deterministic check leads (it's the harder fact).
    results = [CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH)]
    comment = build_comment(results, agent=_assessment(intent_matches_diff=False))
    assert "High concern" in comment
    assert "AWS key" in comment
    assert "stated intent" not in comment


def test_verdict_high_attention_elevates_clean_pr():
    agent = _assessment(attention=[_item("core.py:9", "high", "high")])  # priority 9
    comment = build_comment(_clean(), agent=agent)
    assert "Worth a look" in comment
    assert "core.py:9" in comment


def test_verdict_incomplete_agent_is_inconclusive_not_clean():
    comment = build_comment(_clean(), agent=_assessment(attention=[]), agent_complete=False)
    assert "Inconclusive" in comment
    assert "didn't finish" in comment
    assert "low-risk" not in comment


def test_verdict_incomplete_agent_does_not_soften_a_secret():
    results = [CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH)]
    comment = build_comment(results, agent=_assessment(), agent_complete=False)
    assert "High concern" in comment      # hard fact stands
    assert "Inconclusive" not in comment


def test_verdict_low_confidence_is_inconclusive():
    comment = build_comment(_clean(), agent=_assessment(attention=[], confidence=0.3))
    assert "Inconclusive" in comment
    assert "low-confidence" in comment


def test_verdict_clean_with_confident_agent_is_low_risk():
    comment = build_comment(_clean(), agent=_assessment(attention=[], confidence=0.85))
    assert "Looks low-risk" in comment
    assert "still your call" in comment


def test_label_escalates_on_agent_intent_mismatch():
    # Clean checks, but the agent says claim ≠ diff → status flips to needs-attention.
    assert pick_label(_clean(), _assessment(intent_matches_diff=False)) == LABEL_NEEDS_ATTENTION


def test_label_escalates_on_high_attention_spot():
    agent = _assessment(attention=[_item("core.py:9", "high", "high")])  # priority 9
    assert pick_label(_clean(), agent) == LABEL_NEEDS_ATTENTION


def test_label_incomplete_agent_does_not_escalate():
    # An agent that didn't finish must not flip the primary status filter.
    assert pick_label(_clean(), _assessment(attention=[]), agent_complete=False) == LABEL_CLEAN


def test_label_low_confidence_does_not_escalate():
    assert pick_label(_clean(), _assessment(attention=[], confidence=0.3)) == LABEL_CLEAN


def test_label_no_agent_reduces_to_checks_only():
    # Backward compatible: without an agent, the rule is exactly the old one.
    assert pick_label(_clean()) == LABEL_CLEAN
    assert pick_label([CheckResult("no_tests", False, "x", severity=Severity.MEDIUM)]) == LABEL_NEEDS_ATTENTION


def test_label_and_verdict_never_disagree():
    # The invariant the shared concern level guarantees: a "High concern" /
    # "Worth a look" headline always coincides with a needs-attention label.
    agent = _assessment(intent_matches_diff=False)
    comment = build_comment(_clean(), agent=agent)
    assert "High concern" in comment
    assert pick_label(_clean(), agent) == LABEL_NEEDS_ATTENTION


def test_verdict_never_recommends_merge():
    for agent, complete in [
        (None, True),
        (_assessment(attention=[]), True),
        (_assessment(intent_matches_diff=False), True),
        (_assessment(), False),
    ]:
        comment = build_comment(_clean(), agent=agent, agent_complete=complete).lower()
        assert "merge" not in comment
        assert "approve" not in comment
        assert "lgtm" not in comment


# ── Severity tiering ──────────────────────────────────────────────────────────


def test_pick_label_low_only_failure_stays_clean():
    # A hygiene nit alone must not flip the PR to needs-attention.
    results = [
        CheckResult("branch_naming", False, "bad branch", severity=Severity.LOW),
        CheckResult("secret_leak", True, "", severity=Severity.HIGH),
    ]
    assert pick_label(results) == LABEL_CLEAN


def test_pick_label_medium_failure_needs_attention():
    results = [CheckResult("no_tests", False, "no tests", severity=Severity.MEDIUM)]
    assert pick_label(results) == LABEL_NEEDS_ATTENTION


def test_pick_label_high_failure_needs_attention():
    results = [
        CheckResult("branch_naming", False, "nit", severity=Severity.LOW),
        CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH),
    ]
    assert pick_label(results) == LABEL_NEEDS_ATTENTION


def test_verdict_advisory_only_is_minor_flags():
    results = [CheckResult("branch_naming", False, "nit", severity=Severity.LOW)]
    comment = build_comment(results)
    assert "Minor flags" in comment
    assert "nothing blocking" in comment


def test_verdict_high_severity_is_high_concern():
    results = [CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH)]
    comment = build_comment(results)
    assert "High concern" in comment
    assert "AWS key" in comment      # leads with the failing check's own reason
    assert "1 high" in comment       # severity tail


def test_comment_has_severity_column_and_orders_high_first():
    results = [
        CheckResult("branch_naming", False, "nit", severity=Severity.LOW),
        CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH),
    ]
    comment = build_comment(results)
    assert "Severity" in comment
    # High-severity failure must appear before the low-severity one.
    assert comment.index("Secret Leak") < comment.index("Branch Naming")


def _lows(n: int) -> list:
    return [CheckResult(f"nit{i}", False, "x", severity=Severity.LOW) for i in range(n)]


def test_advisory_pile_escalates_at_default_threshold():
    # 3 advisory failures (the slop signature) → needs-attention by default.
    assert pick_label(_lows(3)) == LABEL_NEEDS_ATTENTION


def test_advisory_below_threshold_stays_clean():
    assert pick_label(_lows(2)) == LABEL_CLEAN


def test_advisory_threshold_is_configurable():
    # Maintainer tightens it: 2 advisories now escalates.
    assert pick_label(_lows(2), advisory_threshold=2) == LABEL_NEEDS_ATTENTION
    # ...or loosens it: 4 needed.
    assert pick_label(_lows(3), advisory_threshold=4) == LABEL_CLEAN


def test_advisory_escalation_can_be_disabled():
    # threshold None disables the rule entirely — any number of advisories is clean.
    assert pick_label(_lows(10), advisory_threshold=None) == LABEL_CLEAN


def test_verdict_explains_advisory_escalation():
    comment = build_comment(_lows(3))
    assert "Worth a look" in comment
    assert "piled up" in comment
    assert "escalates" in comment


def test_verdict_high_flag_beats_advisory_pileup():
    results = _lows(3) + [CheckResult("secret_leak", False, "key", severity=Severity.HIGH)]
    comment = build_comment(results)
    assert "High concern" in comment
    assert "piled up" not in comment  # the hard flag leads, not the nit pile-up


def test_advisory_escalation_config_defaults():
    from pr_warden.repo_config import DEFAULT_CONFIG, parse_config

    assert DEFAULT_CONFIG.advisory_escalation.enabled is True
    assert DEFAULT_CONFIG.advisory_escalation.threshold == 3

    cfg = parse_config("advisory_escalation:\n  threshold: 5\n  enabled: false\n")
    assert cfg.advisory_escalation.threshold == 5
    assert cfg.advisory_escalation.enabled is False


def test_run_checks_stamps_registered_severity():
    from pr_warden.checks.registry import Severity, run_checks, _registry

    by_name = {fn.__name__.replace("check_", ""): sev for fn, sev in _registry}
    assert by_name["secret_leak"] == Severity.HIGH
    assert by_name["critical_path"] == Severity.HIGH
    assert by_name["no_tests"] == Severity.MEDIUM
    assert by_name["branch_naming"] == Severity.LOW
