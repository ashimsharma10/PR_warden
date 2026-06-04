from pr_warden.checks import CheckContext
from pr_warden.checks.intent import (
    check_intent_scope,
    extract_issue_refs,
    repo_modules,
    touched_modules,
)
from pr_warden.github.schemas import GitHubUser, PullRequest, Ref
from pr_warden.repo_config import IntentScopeConfig, RepoConfig


def _pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=1,
        title="feat: tweak auth",
        body="Closes #42",
        state="open",
        draft=False,
        head=Ref(ref="feat/auth", sha="abc123"),
        base=Ref(ref="main", sha="000000"),
        user=GitHubUser(login="dev", id=1),
        changed_files=2,
        additions=20,
        deletions=2,
        requested_reviewers=[GitHubUser(login="rev", id=2)],
    )
    defaults.update(kwargs)
    return PullRequest(**defaults)


# A small but realistic repo tree spanning a few distinct modules.
_TREE = [
    "auth/login.py",
    "auth/tokens.py",
    "billing/invoice.py",
    "billing/charge.py",
    "notifications/email.py",
    "src/util/helpers.py",
    "README.md",
]


def _ctx(*, files, linked_issues, repo_tree=_TREE, config=None) -> CheckContext:
    return CheckContext(
        pr=_pr(),
        files=files,
        repo_tree=repo_tree,
        linked_issues=linked_issues,
        config=config or RepoConfig(),
    )


def _files(*paths) -> list[dict]:
    return [{"filename": p} for p in paths]


# ── extract_issue_refs ──────────────────────────────────────────────────────────


def test_extract_closing_keywords():
    assert extract_issue_refs("Closes #42") == [42]
    assert extract_issue_refs("fixes #1 and resolves #2") == [1, 2]
    assert extract_issue_refs("Fixed: #99") == [99]


def test_extract_dedupes_and_orders():
    assert extract_issue_refs("closes #7, also closes #7, fixes #3") == [7, 3]


def test_extract_ignores_plain_mentions():
    # A bare "#5" with no closing keyword is not a strong intent link.
    assert extract_issue_refs("see #5 for context") == []


def test_extract_ignores_cross_repo_refs():
    assert extract_issue_refs("closes octocat/other#5") == []


def test_extract_empty():
    assert extract_issue_refs("") == []
    assert extract_issue_refs(None) == []


# ── module helpers ──────────────────────────────────────────────────────────────


def test_repo_modules_filters_generic_and_short():
    mods = repo_modules(_TREE, IntentScopeConfig())
    assert {"auth", "billing", "notifications"} <= mods
    # "src" is generic (ignore list); "util" is generic too.
    assert "src" not in mods
    assert "util" not in mods


def test_touched_modules():
    mods = repo_modules(_TREE, IntentScopeConfig())
    assert touched_modules(_files("billing/charge.py"), mods) == {"billing"}
    assert touched_modules(_files("README.md"), mods) == set()


# ── the check ───────────────────────────────────────────────────────────────────


def test_disabled_returns_none():
    cfg = RepoConfig()
    cfg.checks.intent_scope.enabled = False
    r = check_intent_scope(_ctx(files=_files("auth/login.py"), linked_issues=[], config=cfg))
    assert r is None


def test_no_linked_issue_passes_by_default():
    r = check_intent_scope(_ctx(files=_files("billing/charge.py"), linked_issues=[]))
    assert r.passed is True


def test_no_linked_issue_flagged_when_required():
    cfg = RepoConfig()
    cfg.checks.intent_scope.require_linked_issue = True
    r = check_intent_scope(_ctx(files=_files("billing/charge.py"), linked_issues=[], config=cfg))
    assert r.passed is False
    assert "linked issue" in r.reason.lower()


def test_diff_touches_referenced_module_passes():
    issue = {"number": 42, "title": "Auth bug", "body": "The auth flow drops sessions"}
    r = check_intent_scope(_ctx(files=_files("auth/login.py"), linked_issues=[issue]))
    assert r.passed is True


def test_scope_drift_flagged():
    # Issue is about auth; diff only touches billing → flag.
    issue = {"number": 42, "title": "Auth bug", "body": "Fix the auth login flow"}
    r = check_intent_scope(_ctx(files=_files("billing/charge.py"), linked_issues=[issue]))
    assert r.passed is False
    assert "#42" in r.reason
    assert "auth" in r.reason
    assert "billing" in r.reason


def test_issue_names_no_real_module_passes():
    # Issue text references nothing that maps to a repo directory → no signal.
    issue = {"number": 42, "title": "Improve performance", "body": "Make it faster"}
    r = check_intent_scope(_ctx(files=_files("billing/charge.py"), linked_issues=[issue]))
    assert r.passed is True


def test_partial_overlap_passes():
    # Issue names auth + notifications; diff touches notifications → overlap, pass.
    issue = {"number": 42, "title": "auth + notifications", "body": "wire auth into notifications"}
    r = check_intent_scope(_ctx(files=_files("notifications/email.py"), linked_issues=[issue]))
    assert r.passed is True


def test_substring_does_not_match():
    # "authentication" in the issue must not match the "auth" module via substring.
    issue = {"number": 42, "title": "authorization rework", "body": "rework authorization tables"}
    r = check_intent_scope(_ctx(files=_files("billing/charge.py"), linked_issues=[issue]))
    assert r.passed is True


def test_no_data_passes():
    issue = {"number": 42, "title": "auth", "body": "fix auth"}
    # No repo tree → can't build module vocabulary → pass.
    r = check_intent_scope(_ctx(files=_files("billing/charge.py"), linked_issues=[issue], repo_tree=[]))
    assert r.passed is True
