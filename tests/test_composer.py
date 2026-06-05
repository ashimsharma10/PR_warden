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
    ChecksSummary,
    Concern,
    build_comment,
    format_agent_assessment,
    pick_facet_labels,
    verdict_level,
)


def _label(*args, **kwargs) -> str:
    """The retired binary status label, derived from the verdict level for these
    boundary tests. PRwarden stores the level (`verdict_level`) now and never
    applies a status label, but `Concern >= ATTENTION` is still the exact boundary
    the old `needs-attention` label marked — so these tests assert against it."""
    level = verdict_level(*args, **kwargs)
    return LABEL_NEEDS_ATTENTION if level >= Concern.ATTENTION else LABEL_CLEAN


def _assessment(**kwargs) -> DoneInput:
    defaults = dict(
        verdict_level="attention",
        verdict="Guards the charge, but the retry path has no test.",
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


def test_comment_leads_with_verdict_no_headings_no_table():
    # Consolidated: no "## PRwarden Review" / "### Agent Review" headings, no table.
    comment = build_comment([CheckResult("title_quality", True, "")])
    assert "## PRwarden Review" not in comment
    assert "### Agent Review" not in comment
    assert "| Check |" not in comment
    assert comment.lstrip().startswith(("🟢", "🟡", "🟠", "🔴", "⚠️"))


def test_comment_no_agent_shows_failing_checks_table():
    results = [CheckResult("title_quality", False, "Title is a single word")]
    comment = build_comment(results)
    assert "Failing checks" in comment
    assert "| Check | Severity | Detail |" in comment
    assert "Title is a single word" in comment


def test_comment_all_passed_no_table_or_fallback():
    results = [CheckResult("title_quality", True, ""), CheckResult("pr_size", True, "ok")]
    comment = build_comment(results)
    assert "No automated flags" in comment
    assert "checks only" in comment
    assert "Checks needing attention" not in comment  # nothing failed


def test_comment_fallback_orders_high_severity_first():
    results = [
        CheckResult("branch_naming", False, "nit", severity=Severity.LOW),
        CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH),
    ]
    comment = build_comment(results)  # no agent → fallback list
    assert comment.index("Secret Leak") < comment.index("Branch Naming")


def test_comment_footer():
    comment = build_comment([CheckResult("a", True, "")])
    assert "Push a new commit to re-run" in comment
    assert "Powered by PRwarden" not in comment


def _link_ctx(*paths):
    from pr_warden.composer import LinkContext
    return LinkContext(repo="o/r", sha="abc123", known_paths=frozenset(paths))


def test_location_links_when_file_known_else_plain():
    known = build_comment(
        [CheckResult("t", True, "")],
        agent=_assessment(attention=[_item("compile_to_pdf.py:55", "high", "high")]),
        link_ctx=_link_ctx("compile_to_pdf.py"),
    )
    assert "blob/abc123/compile_to_pdf.py#L55)" in known
    unknown = build_comment(
        [CheckResult("t", True, "")],
        agent=_assessment(attention=[_item("ghost.py:9", "high", "high")]),
        link_ctx=_link_ctx("compile_to_pdf.py"),
    )
    assert "](http" not in unknown  # no broken link for a file not in the PR


def test_changes_line_renders_above_body():
    from pr_warden.composer import format_changes
    prev = {"no_tests": {"passed": True, "reason": ""}}
    out = format_changes(prev, [CheckResult("no_tests", False, "now failing")], "abc1234", "def5678")
    assert out is not None and "now failing: No Tests" in out and "`abc1234` → `def5678`" in out
    assert format_changes({"x": {"passed": False}}, [CheckResult("x", False, "")], "a", "b") is None


def test_comment_agent_body_has_no_heading():
    # The agent's read flows straight under the verdict — one consolidated review.
    comment = build_comment([CheckResult("title_quality", True, "")], agent=_assessment())
    assert "### Agent Review" not in comment
    assert "Guards the charge" in comment           # agent summary
    assert "Where to focus" in comment
    assert "payments.py:42" in comment
    # Confidence line was dropped for brevity — just verify content flows.


def test_comment_no_agent_section_when_absent():
    comment = build_comment([CheckResult("a", True, "")])
    assert "### Agent Review" not in comment


def test_agent_body_omits_intent_mismatch_block():
    # The mismatch is carried by the verdict headline, not repeated as a body block.
    out = format_agent_assessment(
        _assessment(intent_matches_diff=False, intent_mismatch_reason="Adds telemetry, not a fix")
    )
    assert "Intent vs. diff mismatch" not in out


def test_format_agent_assessment_renders_questions():
    out = format_agent_assessment(_assessment(open_questions=["Is the lock released on error?"]))
    assert "Questions" in out
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


def test_verdict_level_all_pass():
    results = [CheckResult("a", True, ""), CheckResult("b", True, "")]
    assert _label(results) == LABEL_CLEAN


def test_verdict_level_any_fail():
    results = [CheckResult("a", True, ""), CheckResult("b", False, "reason")]
    assert _label(results) == LABEL_NEEDS_ATTENTION


def test_verdict_level_all_fail():
    results = [CheckResult("a", False, "x"), CheckResult("b", False, "y")]
    assert _label(results) == LABEL_NEEDS_ATTENTION


# ── Clickable locations (granular) ────────────────────────────────────────────


def test_location_links_even_with_trailing_text():
    # The agent sometimes appends prose to the anchor; we still link the path:line.
    ctx = _link_ctx("scrape_jobs.py")
    out = format_agent_assessment(
        _assessment(attention=[_item("scrape_jobs.py:236 (auth header added)", "high", "high")]), ctx
    )
    assert "blob/abc123/scrape_jobs.py#L236)" in out
    assert "(auth header added)" in out  # trailing prose kept


def test_agent_high_verdict_applies_blocker_label():
    # A serious agent verdict is labelled seriously, not merely intent-mismatch.
    labels = pick_facet_labels([CheckResult("t", True, "")], _assessment(verdict_level="high"))
    assert LABEL_BLOCKER in labels


def test_location_links_to_line_when_file_known():
    ctx = _link_ctx("compile_to_pdf.py")
    out = format_agent_assessment(
        _assessment(attention=[_item("compile_to_pdf.py:55", "high", "high")]), ctx
    )
    assert "[`compile_to_pdf.py:55`](https://github.com/o/r/blob/abc123/compile_to_pdf.py#L55)" in out


def test_location_handles_line_range():
    ctx = _link_ctx("a/b.py")
    out = format_agent_assessment(_assessment(attention=[_item("a/b.py:55-60", "high", "high")]), ctx)
    assert "blob/abc123/a/b.py#L55-L60)" in out


def test_location_stays_plain_when_file_unknown():
    # No 404 links: a path not in the PR is left as a plain code span.
    ctx = _link_ctx("other.py")
    out = format_agent_assessment(_assessment(attention=[_item("ghost.py:9", "high", "high")]), ctx)
    assert "`ghost.py:9`" in out
    assert "](http" not in out


def test_location_stays_plain_without_link_ctx():
    out = format_agent_assessment(_assessment(attention=[_item("compile_to_pdf.py:55", "high", "high")]))
    assert "`compile_to_pdf.py:55`" in out
    assert "](http" not in out


def test_location_freetext_citation_not_linked():
    ctx = _link_ctx("compile_to_pdf.py")
    out = format_agent_assessment(_assessment(attention=[_item("the retry loop", "high", "high")]), ctx)
    assert "](http" not in out


def test_verdict_headline_location_is_linked():
    ctx = _link_ctx("auth.py")
    comment = build_comment(
        [CheckResult("title_quality", True, "")],
        agent=_assessment(attention=[_item("auth.py:88", "high", "high")]),
        link_ctx=ctx,
    )
    assert "Worth a look" in comment
    assert "blob/abc123/auth.py#L88)" in comment


# ── Changed files link into the PR diff, not the blob ─────────────────────────


def _diff_ctx(*changed, pr_number=42, tree=()):
    """A link context where `changed` files are part of the PR diff and `tree`
    files exist but weren't touched."""
    from pr_warden.composer import LinkContext
    return LinkContext(
        repo="o/r",
        sha="abc123",
        known_paths=frozenset(changed) | frozenset(tree),
        changed_paths=frozenset(changed),
        pr_number=pr_number,
    )


def _diff_anchor(path: str) -> str:
    import hashlib
    return "diff-" + hashlib.sha256(path.encode("utf-8")).hexdigest()


def test_changed_file_links_to_pr_diff_line():
    # A cited line in a changed file lands the maintainer on the change in the
    # PR's "Files changed" view, not the whole file.
    out = format_agent_assessment(
        _assessment(attention=[_item("payments.py:42", "high", "high")]),
        _diff_ctx("payments.py"),
    )
    anchor = _diff_anchor("payments.py")
    assert f"https://github.com/o/r/pull/42/files#{anchor}R42)" in out
    assert "/blob/" not in out


def test_changed_file_range_collapses_to_start_line_in_diff():
    out = format_agent_assessment(
        _assessment(attention=[_item("payments.py:42-50", "high", "high")]),
        _diff_ctx("payments.py"),
    )
    anchor = _diff_anchor("payments.py")
    assert f"/pull/42/files#{anchor}R42)" in out  # start line; no R..-R.. range


def test_changed_file_without_line_links_to_diff_file_section():
    out = format_agent_assessment(
        _assessment(attention=[_item("payments.py", "high", "high")]),
        _diff_ctx("payments.py"),
    )
    anchor = _diff_anchor("payments.py")
    assert f"/pull/42/files#{anchor})" in out  # file section, no R<line>


def test_unchanged_repo_tree_file_still_links_to_blob():
    # Exists in the repo but not in this PR's diff → blob link (no diff to show).
    out = format_agent_assessment(
        _assessment(attention=[_item("core.py:9", "high", "high")]),
        _diff_ctx("payments.py", tree=("core.py",)),
    )
    assert "blob/abc123/core.py#L9)" in out
    assert "/pull/42/files#diff-" not in out


def test_diff_link_only_when_pr_number_present():
    # changed_paths set but no pr_number → fall back to blob (can't build a diff URL).
    from pr_warden.composer import LinkContext
    ctx = LinkContext(
        repo="o/r", sha="abc123",
        known_paths=frozenset({"payments.py"}),
        changed_paths=frozenset({"payments.py"}),
        pr_number=None,
    )
    out = format_agent_assessment(_assessment(attention=[_item("payments.py:42", "high", "high")]), ctx)
    assert "blob/abc123/payments.py#L42)" in out


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


def test_facets_intent_scope_failure_sets_intent_mismatch():
    # The deterministic intent_scope check drives the same facet as the agent's
    # intent_matches_diff read — no agent needed.
    results = [CheckResult("intent_scope", False, "issue #1 about auth, diff in billing")]
    assert LABEL_INTENT_MISMATCH in pick_facet_labels(results)


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


def test_verdict_is_authored_by_the_agent():
    # The model owns the headline: its verdict_level → glyph/title, its words.
    comment = build_comment(
        _clean(),
        agent=_assessment(
            verdict_level="high",
            verdict="the diff doesn't match the stated intent — only logging was added",
            intent_matches_diff=False,
            intent_mismatch_reason="adds logging, not a fix",
        ),
    )
    assert "🔴 **High concern**" in comment
    assert "only logging was added" in comment


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
    assert "Clean" not in comment


def test_verdict_incomplete_agent_does_not_soften_a_secret():
    results = [CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH)]
    comment = build_comment(results, agent=_assessment(), agent_complete=False)
    assert "High concern" in comment      # hard fact stands
    assert "Inconclusive" not in comment


def test_verdict_agent_can_set_inconclusive():
    comment = build_comment(
        _clean(),
        agent=_assessment(verdict_level="inconclusive", verdict="couldn't verify the migration path"),
    )
    assert "⚠️ **Inconclusive**" in comment
    assert "couldn't verify the migration path" in comment


def test_verdict_agent_low_is_just_clean():
    comment = build_comment(
        _clean(),
        agent=_assessment(verdict_level="low", verdict="ignored for low"),
    )
    assert "✅ **Clean**" in comment
    assert "ignored for low" not in comment  # low → no verbose headline text


def test_label_escalates_on_agent_intent_mismatch():
    # Clean checks, but the agent says claim ≠ diff → status flips to needs-attention.
    assert _label(_clean(), _assessment(intent_matches_diff=False)) == LABEL_NEEDS_ATTENTION


def test_label_escalates_on_high_attention_spot():
    agent = _assessment(attention=[_item("core.py:9", "high", "high")])  # priority 9
    assert _label(_clean(), agent) == LABEL_NEEDS_ATTENTION


def test_label_incomplete_agent_does_not_escalate():
    # An agent that didn't finish must not flip the primary status filter.
    assert _label(_clean(), _assessment(attention=[]), agent_complete=False) == LABEL_CLEAN


def test_label_low_confidence_does_not_escalate():
    assert _label(_clean(), _assessment(attention=[], confidence=0.3)) == LABEL_CLEAN


def test_label_no_agent_reduces_to_checks_only():
    # Backward compatible: without an agent, the rule is exactly the old one.
    assert _label(_clean()) == LABEL_CLEAN
    assert _label([CheckResult("no_tests", False, "x", severity=Severity.MEDIUM)]) == LABEL_NEEDS_ATTENTION


def test_verdict_high_check_floors_the_agent_verdict():
    # Safety floor: even if the model says low-risk, a leaked secret forces 🔴 —
    # the deterministic line, not the model's words.
    results = [CheckResult("secret_leak", False, "AWS key in config.py", severity=Severity.HIGH)]
    comment = build_comment(results, agent=_assessment(verdict_level="low", verdict="looks fine"))
    assert "🔴 **High concern**" in comment
    assert "Secret Leak" in comment
    assert "looks fine" not in comment  # the model could not downgrade the secret


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


# ── ChecksSummary (the one derivation the verdict/labels read from) ───────────


def test_checks_summary_slices_failures_by_severity_and_kind():
    results = [
        CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH),
        CheckResult("no_tests", False, "no tests", severity=Severity.MEDIUM),
        CheckResult("ai_branch", False, "claude- branch", severity=Severity.LOW),
        CheckResult("title_quality", True, "", severity=Severity.MEDIUM),  # passed
    ]
    s = ChecksSummary.from_results(results)

    assert s.failed_names == {"secret_leak", "no_tests", "ai_branch"}
    assert {r.name for r in s.high_fail} == {"secret_leak"}
    assert {r.name for r in s.attention_plus} == {"secret_leak", "no_tests"}
    assert s.advisory_count == 1  # only the LOW failure


def test_checks_summary_all_pass_is_empty():
    s = ChecksSummary.from_results([CheckResult("a", True, ""), CheckResult("b", True, "")])
    assert s.failed == ()
    assert s.failed_names == frozenset()
    assert s.high_fail == ()
    assert s.attention_plus == ()
    assert s.advisory_count == 0


# ── Severity tiering ──────────────────────────────────────────────────────────


def test_verdict_level_low_only_failure_stays_clean():
    # A hygiene nit alone must not flip the PR to needs-attention.
    results = [
        CheckResult("branch_naming", False, "bad branch", severity=Severity.LOW),
        CheckResult("secret_leak", True, "", severity=Severity.HIGH),
    ]
    assert _label(results) == LABEL_CLEAN


def test_verdict_level_medium_failure_needs_attention():
    results = [CheckResult("no_tests", False, "no tests", severity=Severity.MEDIUM)]
    assert _label(results) == LABEL_NEEDS_ATTENTION


def test_verdict_level_high_failure_needs_attention():
    results = [
        CheckResult("branch_naming", False, "nit", severity=Severity.LOW),
        CheckResult("secret_leak", False, "AWS key", severity=Severity.HIGH),
    ]
    assert _label(results) == LABEL_NEEDS_ATTENTION


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


def _lows(n: int) -> list:
    return [CheckResult(f"nit{i}", False, "x", severity=Severity.LOW) for i in range(n)]


def test_advisory_pile_escalates_at_default_threshold():
    # 3 advisory failures (the slop signature) → needs-attention by default.
    assert _label(_lows(3)) == LABEL_NEEDS_ATTENTION


def test_advisory_below_threshold_stays_clean():
    assert _label(_lows(2)) == LABEL_CLEAN


def test_advisory_threshold_is_configurable():
    # Maintainer tightens it: 2 advisories now escalates.
    assert _label(_lows(2), advisory_threshold=2) == LABEL_NEEDS_ATTENTION
    # ...or loosens it: 4 needed.
    assert _label(_lows(3), advisory_threshold=4) == LABEL_CLEAN


def test_advisory_escalation_can_be_disabled():
    # threshold None disables the rule entirely — any number of advisories is clean.
    assert _label(_lows(10), advisory_threshold=None) == LABEL_CLEAN


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
