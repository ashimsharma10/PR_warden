import pytest

from pr_warden.checks import CheckContext
from pr_warden.checks.diff_aware import (
    check_dependency_only_churn,
    check_empty_function_bodies,
    check_no_tests,
    check_test_assertions_weakened,
    check_trivial_metadata_diff,
)
from pr_warden.checks.no_diff import (
    check_ai_branch,
    check_ai_commit_footer,
    check_boilerplate_description,
    check_branch_naming,
    check_description,
    check_description_vs_diff,
    check_pr_size,
    check_self_merge,
    check_title_quality,
)
from pr_warden.github.schemas import GitHubUser, PullRequest, Ref


def _pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=1,
        title="feat: add OAuth login",
        body="This PR adds OAuth login via GitHub, replacing the old password flow.",
        state="open",
        draft=False,
        head=Ref(ref="feat/oauth-login", sha="abc123def456"),
        base=Ref(ref="main", sha="000000"),
        user=GitHubUser(login="dev", id=1),
        changed_files=4,
        additions=90,
        deletions=10,
        requested_reviewers=[GitHubUser(login="reviewer", id=2)],
    )
    defaults.update(kwargs)
    return PullRequest(**defaults)


def _ctx(**pr_kwargs) -> CheckContext:
    return CheckContext(pr=_pr(**pr_kwargs))


def _ctx_with(pr=None, files=None, commits=None) -> CheckContext:
    return CheckContext(
        pr=pr or _pr(),
        files=files or [],
        commits=commits or [],
    )


# ── Title quality ──────────────────────────────────────────────────────────────

def test_title_empty():
    assert check_title_quality(_ctx(title="")).passed is False


def test_title_single_generic_word():
    for word in ["fix", "wip", "update", "patch"]:
        assert check_title_quality(_ctx(title=word)).passed is False, word


def test_title_single_nongeneric_word():
    assert check_title_quality(_ctx(title="authentication")).passed is False


def test_title_multiword_good():
    assert check_title_quality(_ctx(title="feat: add OAuth login")).passed is True


def test_title_multiword_starts_with_wip():
    assert check_title_quality(_ctx(title="wip: not done yet")).passed is True


# ── Description ───────────────────────────────────────────────────────────────

def test_description_none():
    assert check_description(_ctx(body=None)).passed is False


def test_description_too_short():
    assert check_description(_ctx(body="fix thing")).passed is False


def test_description_exactly_20_chars():
    assert check_description(_ctx(body="x" * 20)).passed is True


def test_description_good():
    assert check_description(_ctx()).passed is True


# ── PR size ───────────────────────────────────────────────────────────────────

def test_size_too_many_files():
    result = check_pr_size(_ctx(changed_files=26))
    assert result.passed is False
    assert "26" in result.reason


def test_size_too_many_lines():
    result = check_pr_size(_ctx(changed_files=5, additions=400, deletions=200))
    assert result.passed is False
    assert "600" in result.reason


def test_size_ok():
    assert check_pr_size(_ctx(changed_files=4, additions=90, deletions=10)).passed is True


def test_size_reason_includes_counts():
    result = check_pr_size(_ctx(changed_files=4, additions=90, deletions=10))
    assert "4 files" in result.reason
    assert "100 lines" in result.reason


# ── Branch naming ─────────────────────────────────────────────────────────────

def test_branch_missing_prefix():
    pr = _pr(head=Ref(ref="my-feature", sha="abc"))
    assert check_branch_naming(_ctx_with(pr=pr)).passed is False


def test_branch_main():
    pr = _pr(head=Ref(ref="main", sha="abc"))
    assert check_branch_naming(_ctx_with(pr=pr)).passed is False


@pytest.mark.parametrize("prefix", ["feat/", "fix/", "chore/", "docs/", "refactor/", "test/", "ci/", "build/"])
def test_branch_valid_prefix(prefix):
    pr = _pr(head=Ref(ref=f"{prefix}something", sha="abc"))
    assert check_branch_naming(_ctx_with(pr=pr)).passed is True


# ── Self-merge ────────────────────────────────────────────────────────────────

def test_no_reviewers():
    assert check_self_merge(_ctx(requested_reviewers=[])).passed is False


def test_self_review_only():
    pr = _pr(user=GitHubUser(login="dev", id=1), requested_reviewers=[GitHubUser(login="dev", id=1)])
    assert check_self_merge(_ctx_with(pr=pr)).passed is False


def test_valid_reviewer():
    pr = _pr(user=GitHubUser(login="dev", id=1), requested_reviewers=[GitHubUser(login="reviewer", id=2)])
    assert check_self_merge(_ctx_with(pr=pr)).passed is True


def test_self_plus_other_reviewer():
    pr = _pr(
        user=GitHubUser(login="dev", id=1),
        requested_reviewers=[GitHubUser(login="dev", id=1), GitHubUser(login="reviewer", id=2)],
    )
    assert check_self_merge(_ctx_with(pr=pr)).passed is True


# ── AI branch ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("branch", ["claude-fix", "codex/feature", "cursor-refactor", "copilot/update", "devin-patch"])
def test_ai_branch_flagged(branch):
    pr = _pr(head=Ref(ref=branch, sha="abc"))
    assert check_ai_branch(_ctx_with(pr=pr)).passed is False


def test_ai_branch_clean():
    pr = _pr(head=Ref(ref="feat/my-feature", sha="abc"))
    assert check_ai_branch(_ctx_with(pr=pr)).passed is True


# ── AI commit footer ──────────────────────────────────────────────────────────

def test_ai_commit_footer_detected():
    commits = [{"commit": {"message": "feat: add thing\n\nCo-Authored-By: Claude <noreply@anthropic.com>"}}]
    assert check_ai_commit_footer(_ctx_with(commits=commits)).passed is False


def test_ai_commit_footer_generated_with():
    commits = [{"commit": {"message": "fix: thing\n\nGenerated with Claude"}}]
    assert check_ai_commit_footer(_ctx_with(commits=commits)).passed is False


def test_ai_commit_footer_clean():
    commits = [{"commit": {"message": "feat: add thing\n\nCo-authored-by: Alice <alice@example.com>"}}]
    assert check_ai_commit_footer(_ctx_with(commits=commits)).passed is True


def test_ai_commit_footer_no_commits():
    assert check_ai_commit_footer(_ctx_with(commits=[])).passed is True


# ── Boilerplate description ────────────────────────────────────────────────────

def test_boilerplate_add_description():
    assert check_boilerplate_description(_ctx(body="Please add a description here")).passed is False


def test_boilerplate_describe_changes():
    assert check_boilerplate_description(_ctx(body="Describe your changes in detail")).passed is False


def test_boilerplate_html_comment():
    body = "<!-- What does this PR do? -->\nFixes a bug"
    assert check_boilerplate_description(_ctx(body=body)).passed is False


def test_boilerplate_good_description():
    assert check_boilerplate_description(_ctx()).passed is True


# ── Description vs diff ───────────────────────────────────────────────────────

def test_desc_vs_diff_flagged():
    pr = _pr(body="fix bug", additions=80, deletions=50)
    assert check_description_vs_diff(_ctx_with(pr=pr)).passed is False


def test_desc_vs_diff_long_desc():
    pr = _pr(body="This PR fixes the authentication bug by updating the token validation logic.", additions=80, deletions=50)
    assert check_description_vs_diff(_ctx_with(pr=pr)).passed is True


def test_desc_vs_diff_small_diff():
    pr = _pr(body="fix", additions=5, deletions=3)
    assert check_description_vs_diff(_ctx_with(pr=pr)).passed is True


# ── Trivial metadata diff ─────────────────────────────────────────────────────

def test_trivial_metadata_only_md_files():
    files = [{"filename": "README.md"}, {"filename": "CONTRIBUTING.md"}]
    pr = _pr(additions=5, deletions=3)
    result = check_trivial_metadata_diff(_ctx_with(pr=pr, files=files))
    assert result.passed is False


def test_trivial_metadata_has_source_file():
    files = [{"filename": "README.md"}, {"filename": "src/main.py"}]
    pr = _pr(additions=5, deletions=3)
    assert check_trivial_metadata_diff(_ctx_with(pr=pr, files=files)).passed is True


def test_trivial_metadata_large_doc_change():
    files = [{"filename": "README.md"}]
    pr = _pr(additions=100, deletions=50)
    assert check_trivial_metadata_diff(_ctx_with(pr=pr, files=files)).passed is True


def test_trivial_metadata_no_files():
    assert check_trivial_metadata_diff(_ctx_with(files=[])).passed is True


# ── No tests ──────────────────────────────────────────────────────────────────

def test_no_tests_source_no_tests():
    files = [{"filename": "src/auth.py"}, {"filename": "src/models.py"}]
    assert check_no_tests(_ctx_with(files=files)).passed is False


def test_no_tests_source_with_tests():
    files = [{"filename": "src/auth.py"}, {"filename": "tests/test_auth.py"}]
    assert check_no_tests(_ctx_with(files=files)).passed is True


def test_no_tests_only_docs():
    files = [{"filename": "README.md"}]
    assert check_no_tests(_ctx_with(files=files)).passed is True


def test_no_tests_no_files():
    assert check_no_tests(_ctx_with(files=[])).passed is True


# ── Test assertions weakened ──────────────────────────────────────────────────

def test_assertions_weakened_detected():
    files = [{
        "filename": "tests/test_auth.py",
        "patch": "@@ -1,5 +1,4 @@\n def test_login():\n-    assertEqual(response.status, 200)\n+    pass",
    }]
    assert check_test_assertions_weakened(_ctx_with(files=files)).passed is False


def test_assertions_weakened_added_assertion():
    files = [{
        "filename": "tests/test_auth.py",
        "patch": "@@ -1,4 +1,5 @@\n def test_login():\n+    assertEqual(response.status, 200)",
    }]
    assert check_test_assertions_weakened(_ctx_with(files=files)).passed is True


def test_assertions_weakened_non_test_file():
    files = [{
        "filename": "src/auth.py",
        "patch": "-    assertEqual(a, b)",
    }]
    assert check_test_assertions_weakened(_ctx_with(files=files)).passed is True


# ── Empty function bodies ──────────────────────────────────────────────────────

def test_empty_body_pass_statement():
    files = [{
        "filename": "src/service.py",
        "patch": "@@ -1,2 +1,3 @@\n+def process_payment(amount):\n+    pass",
    }]
    assert check_empty_function_bodies(_ctx_with(files=files)).passed is False


def test_empty_body_return_none():
    files = [{
        "filename": "src/service.py",
        "patch": "+def get_user(id):\n+    return None",
    }]
    assert check_empty_function_bodies(_ctx_with(files=files)).passed is False


def test_empty_body_real_implementation():
    files = [{
        "filename": "src/service.py",
        "patch": "+def get_user(id):\n+    return db.query(User).filter_by(id=id).first()",
    }]
    assert check_empty_function_bodies(_ctx_with(files=files)).passed is True


# ── Dependency-only churn ─────────────────────────────────────────────────────

def test_dep_churn_only_lockfile():
    files = [{"filename": "package-lock.json"}, {"filename": "yarn.lock"}]
    assert check_dependency_only_churn(_ctx_with(files=files)).passed is False


def test_dep_churn_lockfile_plus_source():
    files = [{"filename": "package-lock.json"}, {"filename": "src/index.js"}]
    assert check_dependency_only_churn(_ctx_with(files=files)).passed is True


def test_dep_churn_no_files():
    assert check_dependency_only_churn(_ctx_with(files=[])).passed is True
