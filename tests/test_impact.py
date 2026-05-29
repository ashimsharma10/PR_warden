import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_warden.checks import CheckContext
from pr_warden.checks.impact import (
    CodeownersRule,
    _codeowners_match,
    _critical_path_match,
    _write_diff_additions,
    check_codeowners,
    check_critical_path,
    check_secret_leak,
    check_shared_code,
    find_owners,
    parse_codeowners,
    run_gitleaks,
)
from pr_warden.github.schemas import GitHubUser, PullRequest, Ref
from pr_warden.repo_config import RepoConfig, parse_config


def _pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=1,
        title="feat: add OAuth login",
        body="This PR adds OAuth login via GitHub.",
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


def _ctx(
    pr=None, files=None, commits=None, config=None,
    repo_tree=None, codeowners_raw=None, gitleaks_findings=None,
) -> CheckContext:
    return CheckContext(
        pr=pr or _pr(),
        files=files or [],
        commits=commits or [],
        config=config or RepoConfig(),
        repo_tree=repo_tree or [],
        codeowners_raw=codeowners_raw,
        gitleaks_findings=gitleaks_findings,
    )


# ── _write_diff_additions helper ─────────────────────────────────────────────


class TestWriteDiffAdditions:

    def test_only_added_lines_written(self, tmp_path):
        files = [{"filename": "src/auth.py", "patch": "-old = 1\n+new = 2\n context"}]
        has_content = _write_diff_additions(str(tmp_path), files)
        assert has_content is True
        written = (tmp_path / "src" / "auth.py").read_text()
        assert "new = 2" in written
        assert "old = 1" not in written
        assert "context" not in written

    def test_no_patch_skipped(self, tmp_path):
        files = [{"filename": "image.png"}]
        assert _write_diff_additions(str(tmp_path), files) is False

    def test_only_removed_lines_skipped(self, tmp_path):
        files = [{"filename": "a.py", "patch": "-removed = True"}]
        assert _write_diff_additions(str(tmp_path), files) is False

    def test_nested_directory_created(self, tmp_path):
        files = [{"filename": "a/b/c/deep.py", "patch": "+x = 1"}]
        _write_diff_additions(str(tmp_path), files)
        assert (tmp_path / "a" / "b" / "c" / "deep.py").exists()

    def test_diff_header_stripped(self, tmp_path):
        files = [{"filename": "main.py", "patch": "+++ b/main.py\n+x = 1"}]
        _write_diff_additions(str(tmp_path), files)
        written = (tmp_path / "main.py").read_text()
        assert "+++" not in written
        assert "x = 1" in written


# ── run_gitleaks ──────────────────────────────────────────────────────────────


def _mock_process(stdout: bytes, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.kill = MagicMock()
    return proc


_FINDING = {"RuleID": "aws-access-key-id", "Description": "AWS Access Key ID", "File": "config.py", "Secret": "AKIA..."}


class TestRunGitleaks:

    async def test_not_installed_returns_none(self):
        with patch("shutil.which", return_value=None):
            result = await run_gitleaks([{"filename": "f.py", "patch": "+x=1"}])
        assert result is None

    async def test_no_added_content_returns_empty(self):
        with patch("shutil.which", return_value="/usr/bin/gitleaks"):
            result = await run_gitleaks([{"filename": "image.png"}])
        assert result == []

    async def test_no_findings_returns_empty(self):
        proc = _mock_process(b"[]", returncode=0)
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await run_gitleaks([{"filename": "main.py", "patch": "+x = 1"}])
        assert result == []

    async def test_findings_returned(self):
        output = json.dumps([_FINDING]).encode()
        proc = _mock_process(output, returncode=1)
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await run_gitleaks([{"filename": "config.py", "patch": "+AKIAIOSFODNN7EXAMPLE"}])
        assert result is not None
        assert len(result) == 1
        assert result[0]["RuleID"] == "aws-access-key-id"

    async def test_file_path_stripped_of_tmpdir_prefix(self):
        finding_with_abs = dict(_FINDING, File="/tmp/prwarden_xyz/src/config.py")
        output = json.dumps([finding_with_abs]).encode()
        proc = _mock_process(output, returncode=1)
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await run_gitleaks([{"filename": "src/config.py", "patch": "+secret=abc123"}])
        # The temp dir prefix should be stripped, leaving a relative path
        assert result is not None
        assert not result[0]["File"].startswith("/tmp")

    async def test_subprocess_error_returns_none(self):
        proc = _mock_process(b"", returncode=126)
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await run_gitleaks([{"filename": "f.py", "patch": "+x=1"}])
        assert result is None

    async def test_invalid_json_returns_none(self):
        proc = _mock_process(b"not valid json", returncode=0)
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await run_gitleaks([{"filename": "f.py", "patch": "+x=1"}])
        assert result is None

    async def test_null_output_returns_empty(self):
        proc = _mock_process(b"null", returncode=0)
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await run_gitleaks([{"filename": "f.py", "patch": "+x=1"}])
        assert result == []

    async def test_timeout_returns_none(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await run_gitleaks([{"filename": "f.py", "patch": "+x=1"}])
        assert result is None
        proc.kill.assert_called_once()


# ── check_secret_leak (reads pre-populated context, no subprocess) ────────────


class TestCheckSecretLeak:

    def test_gitleaks_not_available(self):
        result = check_secret_leak(_ctx(gitleaks_findings=None))
        assert result.passed is True
        assert "not available" in result.reason

    def test_no_findings(self):
        result = check_secret_leak(_ctx(gitleaks_findings=[]))
        assert result.passed is True
        assert "No secrets detected" in result.reason

    def test_findings_fail(self):
        result = check_secret_leak(_ctx(gitleaks_findings=[_FINDING]))
        assert result.passed is False
        assert "AWS Access Key ID" in result.reason
        assert "config.py" in result.reason

    def test_count_in_reason(self):
        findings = [dict(_FINDING, File=f"file{i}.py") for i in range(3)]
        result = check_secret_leak(_ctx(gitleaks_findings=findings))
        assert "3 secret(s)" in result.reason

    def test_overflow_truncated(self):
        findings = [dict(_FINDING, File=f"file{i}.py") for i in range(8)]
        result = check_secret_leak(_ctx(gitleaks_findings=findings))
        assert "+3 more" in result.reason

    def test_disabled(self):
        cfg = parse_config("checks:\n  secret_leak:\n    enabled: false\n")
        assert check_secret_leak(_ctx(gitleaks_findings=[_FINDING], config=cfg)) is None

    def test_fallback_to_rule_id_when_no_description(self):
        finding = {"RuleID": "some-custom-rule", "File": "main.go"}
        result = check_secret_leak(_ctx(gitleaks_findings=[finding]))
        assert result.passed is False
        assert "some-custom-rule" in result.reason


# ── CODEOWNERS parsing ────────────────────────────────────────────────────────


class TestCodeownersParsing:

    def test_basic(self):
        raw = "*.js @frontend\nsrc/core/** @core-team\n"
        rules = parse_codeowners(raw)
        assert len(rules) == 2
        assert rules[0] == CodeownersRule("*.js", ("@frontend",))
        assert rules[1] == CodeownersRule("src/core/**", ("@core-team",))

    def test_comments_and_blanks_skipped(self):
        raw = "# comment\n\n*.py @backend\n"
        rules = parse_codeowners(raw)
        assert len(rules) == 1

    def test_multiple_owners(self):
        raw = "*.go @alice @bob @org/go-team\n"
        rules = parse_codeowners(raw)
        assert rules[0].owners == ("@alice", "@bob", "@org/go-team")

    def test_section_headers_skipped(self):
        raw = "[Security]\n*.pem @security\n"
        rules = parse_codeowners(raw)
        assert len(rules) == 1
        assert rules[0].pattern == "*.pem"

    def test_no_owner_line_skipped(self):
        raw = "*.md\n*.py @dev\n"
        rules = parse_codeowners(raw)
        assert len(rules) == 1

    def test_empty_string(self):
        assert parse_codeowners("") == []

    def test_only_comments(self):
        assert parse_codeowners("# owner rules\n# more comments\n") == []


# ── CODEOWNERS matching ──────────────────────────────────────────────────────


class TestCodeownersMatch:

    def test_extension_glob_matches_any_depth(self):
        assert _codeowners_match("src/app/index.js", "*.js") is True
        assert _codeowners_match("index.js", "*.js") is True

    def test_extension_glob_no_match(self):
        assert _codeowners_match("style.css", "*.js") is False

    def test_directory_pattern(self):
        assert _codeowners_match("docs/guide.md", "docs/") is True
        assert _codeowners_match("docs/sub/page.md", "docs/") is True
        assert _codeowners_match("src/docs.py", "docs/") is False

    def test_doublestar(self):
        assert _codeowners_match("src/core/auth.py", "src/core/**") is True
        assert _codeowners_match("src/core/sub/deep.py", "src/core/**") is True
        assert _codeowners_match("src/other/auth.py", "src/core/**") is False

    def test_rooted_path(self):
        assert _codeowners_match("Makefile", "/Makefile") is True
        assert _codeowners_match("sub/Makefile", "/Makefile") is False

    def test_specific_file(self):
        assert _codeowners_match("src/main.py", "src/main.py") is True
        assert _codeowners_match("src/other.py", "src/main.py") is False

    def test_single_star_no_slash(self):
        assert _codeowners_match("src/utils/string.ts", "src/utils/*.ts") is True
        assert _codeowners_match("src/utils/sub/string.ts", "src/utils/*.ts") is False


class TestFindOwners:

    def test_last_match_wins(self):
        rules = [
            CodeownersRule("*.py", ("@general",)),
            CodeownersRule("src/core/**", ("@core-team",)),
        ]
        assert find_owners("src/core/auth.py", rules) == ["@core-team"]

    def test_no_match(self):
        rules = [CodeownersRule("*.go", ("@go-team",))]
        assert find_owners("main.py", rules) == []

    def test_multiple_owners_returned(self):
        rules = [CodeownersRule("*.py", ("@alice", "@bob"))]
        assert find_owners("main.py", rules) == ["@alice", "@bob"]


# ── CODEOWNERS check integration ─────────────────────────────────────────────


class TestCheckCodeowners:

    def test_no_codeowners_file(self):
        result = check_codeowners(_ctx(codeowners_raw=None))
        assert result.passed is True
        assert "No CODEOWNERS" in result.reason

    def test_empty_codeowners(self):
        result = check_codeowners(_ctx(codeowners_raw="# just comments\n"))
        assert result.passed is True
        assert "no rules" in result.reason

    def test_no_matching_files(self):
        files = [{"filename": "README.md"}]
        result = check_codeowners(_ctx(files=files, codeowners_raw="*.go @go-team\n"))
        assert result.passed is True
        assert "no designated owners" in result.reason.lower()

    def test_owner_is_reviewer(self):
        pr = _pr(requested_reviewers=[GitHubUser(login="alice", id=2)])
        files = [{"filename": "main.py"}]
        codeowners = "*.py @alice\n"
        result = check_codeowners(_ctx(pr=pr, files=files, codeowners_raw=codeowners))
        assert result.passed is True
        assert "Required reviewers assigned" in result.reason

    def test_owner_not_reviewing(self):
        pr = _pr(requested_reviewers=[GitHubUser(login="bob", id=3)])
        files = [{"filename": "main.py"}]
        codeowners = "*.py @alice\n"
        result = check_codeowners(_ctx(pr=pr, files=files, codeowners_raw=codeowners))
        assert result.passed is False
        assert "@alice" in result.reason

    def test_team_owner_listed(self):
        pr = _pr(requested_reviewers=[])
        files = [{"filename": "main.py"}]
        codeowners = "*.py @org/backend-team\n"
        result = check_codeowners(_ctx(pr=pr, files=files, codeowners_raw=codeowners))
        assert result.passed is True or "@org/backend-team" in result.reason

    def test_require_review_disabled(self):
        cfg = parse_config("checks:\n  codeowners:\n    require_review: false\n")
        files = [{"filename": "main.py"}]
        codeowners = "*.py @alice\n"
        result = check_codeowners(_ctx(
            pr=_pr(requested_reviewers=[]),
            files=files, codeowners_raw=codeowners, config=cfg,
        ))
        assert result.passed is True
        assert "@alice" in result.reason

    def test_disabled(self):
        cfg = parse_config("checks:\n  codeowners:\n    enabled: false\n")
        assert check_codeowners(_ctx(config=cfg)) is None


# ── Shared code heuristic check ───────────────────────────────────────────────


class TestCheckSharedCode:

    _LARGE_TREE = [f"src/feature{i}/module.py" for i in range(50)] + [
        "src/utils/format.py",
        "src/utils/string.py",
        "src/core/auth.py",
    ]

    def test_shared_file_flagged(self):
        files = [{"filename": "src/utils/format.py"}]
        result = check_shared_code(_ctx(files=files, repo_tree=self._LARGE_TREE))
        assert result.passed is False
        assert "src/utils/format.py" in result.reason

    def test_reason_says_heuristic(self):
        files = [{"filename": "src/utils/format.py"}]
        result = check_shared_code(_ctx(files=files, repo_tree=self._LARGE_TREE))
        assert "heuristic" in result.reason

    def test_leaf_file_passes(self):
        files = [{"filename": "src/feature1/module.py"}]
        result = check_shared_code(_ctx(files=files, repo_tree=self._LARGE_TREE))
        assert result.passed is True

    def test_no_source_files_passes(self):
        files = [{"filename": "README.md"}]
        result = check_shared_code(_ctx(files=files, repo_tree=self._LARGE_TREE))
        assert result.passed is True
        assert "No source files" in result.reason

    def test_empty_tree_passes(self):
        files = [{"filename": "src/utils/format.py"}]
        result = check_shared_code(_ctx(files=files, repo_tree=[]))
        assert result.passed is True
        assert "unavailable" in result.reason

    def test_multiple_shared_files(self):
        files = [
            {"filename": "src/utils/format.py"},
            {"filename": "src/core/auth.py"},
        ]
        result = check_shared_code(_ctx(files=files, repo_tree=self._LARGE_TREE))
        assert result.passed is False
        assert "src/utils/format.py" in result.reason
        assert "src/core/auth.py" in result.reason

    def test_disabled(self):
        cfg = parse_config("checks:\n  shared_code:\n    enabled: false\n")
        files = [{"filename": "src/utils/format.py"}]
        assert check_shared_code(_ctx(files=files, repo_tree=self._LARGE_TREE, config=cfg)) is None

    def test_custom_shared_paths(self):
        cfg = parse_config("checks:\n  shared_code:\n    shared_paths: [internal]\n")
        tree = ["src/internal/api.py", "src/feature/main.py"]
        files = [{"filename": "src/internal/api.py"}]
        result = check_shared_code(_ctx(files=files, repo_tree=tree, config=cfg))
        assert result.passed is False

    def test_reason_includes_repo_size(self):
        files = [{"filename": "src/utils/format.py"}]
        result = check_shared_code(_ctx(files=files, repo_tree=self._LARGE_TREE))
        assert "source files in repo" in result.reason

    def test_overflow_display(self):
        files = [{"filename": f"src/utils/f{i}.py"} for i in range(8)]
        tree = self._LARGE_TREE + [f"src/utils/f{i}.py" for i in range(8)]
        result = check_shared_code(_ctx(files=files, repo_tree=tree))
        assert result.passed is False
        assert "+3 more" in result.reason


# ── Critical path glob matching ───────────────────────────────────────────────


class TestCriticalPathMatch:

    def test_auth_glob(self):
        assert _critical_path_match("auth/login.py", "auth/**") is True
        assert _critical_path_match("auth/sub/deep.py", "auth/**") is True
        assert _critical_path_match("src/auth_utils.py", "auth/**") is False

    def test_terraform_basename_glob(self):
        # *.tf has no slash → matches basename at any depth
        assert _critical_path_match("infra/vpc/main.tf", "*.tf") is True
        assert _critical_path_match("main.tf", "*.tf") is True
        assert _critical_path_match("main.py", "*.tf") is False

    def test_migrations_glob(self):
        assert _critical_path_match("migrations/0042_users.sql", "migrations/**") is True
        assert _critical_path_match("src/migrations/0001.py", "migrations/**") is False

    def test_github_workflows(self):
        assert _critical_path_match(".github/workflows/ci.yml", ".github/workflows/**") is True
        assert _critical_path_match(".github/CODEOWNERS", ".github/workflows/**") is False

    def test_payments_glob(self):
        assert _critical_path_match("payments/stripe.py", "payments/**") is True
        assert _critical_path_match("src/payments/charge.py", "payments/**") is False

    def test_specific_file(self):
        assert _critical_path_match("alembic/env.py", "alembic/env.py") is True
        assert _critical_path_match("alembic/other.py", "alembic/env.py") is False


class TestCheckCriticalPath:

    def test_no_globs_passes(self):
        result = check_critical_path(_ctx(files=[{"filename": "auth/login.py"}]))
        assert result.passed is True
        assert "No critical-path globs configured" in result.reason

    def test_match_auth(self):
        cfg = parse_config("checks:\n  critical_path:\n    globs:\n      - 'auth/**'\n")
        files = [{"filename": "auth/login.py"}]
        result = check_critical_path(_ctx(files=files, config=cfg))
        assert result.passed is False
        assert "auth/login.py" in result.reason
        assert "[auth/**]" in result.reason

    def test_match_migrations(self):
        cfg = parse_config("checks:\n  critical_path:\n    globs:\n      - 'migrations/**'\n")
        files = [{"filename": "migrations/0042_add_users.sql"}]
        result = check_critical_path(_ctx(files=files, config=cfg))
        assert result.passed is False
        assert "migrations/0042_add_users.sql" in result.reason

    def test_match_terraform(self):
        cfg = parse_config("checks:\n  critical_path:\n    globs:\n      - '*.tf'\n")
        files = [{"filename": "infra/networking/vpc.tf"}]
        result = check_critical_path(_ctx(files=files, config=cfg))
        assert result.passed is False
        assert "infra/networking/vpc.tf" in result.reason

    def test_match_github_workflows(self):
        cfg = parse_config("checks:\n  critical_path:\n    globs:\n      - '.github/workflows/**'\n")
        files = [{"filename": ".github/workflows/deploy.yml"}]
        result = check_critical_path(_ctx(files=files, config=cfg))
        assert result.passed is False

    def test_multiple_globs_first_match_reported(self):
        yaml = "checks:\n  critical_path:\n    globs:\n      - 'auth/**'\n      - 'payments/**'\n"
        cfg = parse_config(yaml)
        files = [
            {"filename": "auth/token.py"},
            {"filename": "payments/charge.py"},
            {"filename": "src/utils/format.py"},
        ]
        result = check_critical_path(_ctx(files=files, config=cfg))
        assert result.passed is False
        assert "auth/token.py" in result.reason
        assert "payments/charge.py" in result.reason
        assert "src/utils/format.py" not in result.reason

    def test_no_files_match_passes(self):
        cfg = parse_config("checks:\n  critical_path:\n    globs:\n      - 'auth/**'\n")
        files = [{"filename": "src/feature/new_widget.py"}]
        result = check_critical_path(_ctx(files=files, config=cfg))
        assert result.passed is True
        assert "No critical-path files changed" in result.reason

    def test_overflow_display(self):
        yaml = "checks:\n  critical_path:\n    globs:\n      - 'auth/**'\n"
        cfg = parse_config(yaml)
        files = [{"filename": f"auth/module{i}.py"} for i in range(8)]
        result = check_critical_path(_ctx(files=files, config=cfg))
        assert result.passed is False
        assert "+3 more" in result.reason

    def test_disabled(self):
        cfg = parse_config("checks:\n  critical_path:\n    enabled: false\n")
        files = [{"filename": "auth/login.py"}]
        assert check_critical_path(_ctx(files=files, config=cfg)) is None


# ── run_checks integration ───────────────────────────────────────────────────


def test_stage2_checks_registered():
    from pr_warden.checks import run_checks

    tree = ["src/main.py", "src/utils/helper.py"]
    ctx = _ctx(
        files=[{"filename": "src/main.py", "patch": "+x = 1"}],
        repo_tree=tree,
        gitleaks_findings=[],
    )
    results = run_checks(ctx)
    names = {r.name for r in results}
    assert "shared_code" in names
    assert "critical_path" in names
    assert "codeowners" in names
    assert "secret_leak" in names
