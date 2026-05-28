import pytest

from pr_warden.checks import CheckContext
from pr_warden.checks.impact import (
    CodeownersRule,
    SecretFinding,
    _codeowners_match,
    _critical_path_match,
    check_codeowners,
    check_critical_path,
    check_secret_leak,
    check_shared_code,
    find_owners,
    parse_codeowners,
    scan_diff_secrets,
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
    repo_tree=None, codeowners_raw=None,
) -> CheckContext:
    return CheckContext(
        pr=pr or _pr(),
        files=files or [],
        commits=commits or [],
        config=config or RepoConfig(),
        repo_tree=repo_tree or [],
        codeowners_raw=codeowners_raw,
    )


# ── Secret scanning: pattern coverage ─────────────────────────────────────────


class TestSecretScanning:

    # ── Platform-specific patterns (high confidence, known prefixes) ───────

    def test_aws_access_key(self):
        files = [{"filename": "config.py", "patch": "+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'"}]
        findings = scan_diff_secrets(files)
        assert len(findings) == 1
        assert findings[0].rule == "aws-access-key-id"

    def test_github_pat(self):
        files = [{"filename": ".env", "patch": "+TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"}]
        assert any(f.rule == "github-pat" for f in scan_diff_secrets(files))

    def test_private_key(self):
        files = [{"filename": "key.pem", "patch": "+-----BEGIN RSA PRIVATE KEY-----"}]
        assert any(f.rule == "private-key" for f in scan_diff_secrets(files))

    def test_private_key_ec(self):
        files = [{"filename": "key.pem", "patch": "+-----BEGIN EC PRIVATE KEY-----"}]
        assert any(f.rule == "private-key" for f in scan_diff_secrets(files))

    def test_stripe_live_key(self):
        files = [{"filename": "pay.py", "patch": "+sk_live_ABCDEFGHIJKLMNOPQRSTUVWXYZab"}]
        assert any(f.rule == "stripe-live-secret" for f in scan_diff_secrets(files))

    def test_db_connection_string(self):
        files = [{"filename": "db.py", "patch": "+DSN = 'postgres://admin:s3cret@db.host.com:5432/prod'"}]
        assert any(f.rule == "db-connection-string" for f in scan_diff_secrets(files))

    # ── Generic patterns (catches any platform, any service) ──────────────

    def test_generic_catches_kubernetes_token(self):
        files = [{"filename": "deploy.py", "patch": "+SERVICE_ACCOUNT_KEY = 'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'"}]
        findings = scan_diff_secrets(files)
        assert len(findings) >= 1

    def test_generic_catches_datadog_api_key(self):
        files = [{"filename": "monitoring.py", "patch": "+api_key = 'dd49a1c7e3b84f6ea2c0d5e8f7a9b3c1d4e6f8a0b2c4d6e8f0a1b3c5d7e9f0a1'"}]
        findings = scan_diff_secrets(files)
        assert any(f.rule in ("generic-secret", "generic-hex-secret") for f in findings)

    def test_generic_catches_sendgrid_key(self):
        files = [{"filename": "email.py", "patch": "+api_secret = 'SG.abcdefghijklmnop.qrstuvwxyz012345'"}]
        findings = scan_diff_secrets(files)
        assert any(f.rule == "generic-secret" for f in findings)

    def test_generic_catches_arbitrary_token(self):
        files = [{"filename": "service.py", "patch": "+auth_token = 'xK9mP2nQ7rS4tU8vW1yZ3aB5cD7eF9gH'"}]
        findings = scan_diff_secrets(files)
        assert any(f.rule == "generic-secret" for f in findings)

    def test_generic_catches_password_in_config(self):
        files = [{"filename": "settings.yml", "patch": "+password: 'Sup3rS3cr3tP@ssw0rd!'"}]
        findings = scan_diff_secrets(files)
        assert any(f.rule == "generic-secret" for f in findings)

    def test_generic_catches_jwt_anywhere(self):
        files = [{"filename": "auth.go", "patch": "+hardcoded := \"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiYWRtaW4ifQ.dyt0CoTl4WoVjAHI9Q_CqSQ\""}]
        findings = scan_diff_secrets(files)
        assert any(f.rule == "jwt-token" for f in findings)

    def test_generic_catches_hex_secret(self):
        files = [{"filename": "crypto.py", "patch": "+encryption_key = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4'"}]
        findings = scan_diff_secrets(files)
        assert any(f.rule == "generic-hex-secret" for f in findings)

    def test_generic_catches_client_secret(self):
        files = [{"filename": "oauth.py", "patch": "+client_secret = 'dGhpcyBpcyBhIHJlYWwgc2VjcmV0IGtleQ=='"}]
        findings = scan_diff_secrets(files)
        assert any(f.rule == "generic-secret" for f in findings)

    def test_generic_catches_connection_string_password(self):
        files = [{"filename": "db.py", "patch": "+connection_string = 'Server=prod;Database=main;User=admin;Password=r3alP@ss!'"}]
        findings = scan_diff_secrets(files)
        assert any(f.rule == "generic-secret" for f in findings)

    # ── Placeholder filtering (no false positives) ────────────────────────

    def test_placeholder_changeme_ignored(self):
        files = [{"filename": "config.py", "patch": "+password = 'changeme_before_deploy'"}]
        findings = scan_diff_secrets(files)
        assert not any(f.rule == "generic-secret" for f in findings)

    def test_placeholder_your_key_ignored(self):
        files = [{"filename": "config.py", "patch": "+api_key = 'your-api-key-goes-here'"}]
        findings = scan_diff_secrets(files)
        assert not any(f.rule == "generic-secret" for f in findings)

    def test_placeholder_env_var_reference_ignored(self):
        files = [{"filename": "config.py", "patch": "+secret = '${MY_SECRET_FROM_VAULT}'"}]
        findings = scan_diff_secrets(files)
        assert not any(f.rule == "generic-secret" for f in findings)

    def test_placeholder_example_ignored(self):
        files = [{"filename": "docs.py", "patch": "+token = 'example-token-for-documentation'"}]
        findings = scan_diff_secrets(files)
        assert not any(f.rule == "generic-secret" for f in findings)

    def test_placeholder_test_token_ignored(self):
        files = [{"filename": "test.py", "patch": "+api_key = 'test_key_not_real_at_all'"}]
        findings = scan_diff_secrets(files)
        assert not any(f.rule == "generic-secret" for f in findings)

    # ── Edge cases ────────────────────────────────────────────────────────

    def test_clean_diff_no_findings(self):
        files = [{"filename": "main.py", "patch": "+print('hello world')"}]
        assert scan_diff_secrets(files) == []

    def test_removed_lines_ignored(self):
        files = [{"filename": "config.py", "patch": "-AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n+AWS_KEY = os.environ['AWS_KEY']"}]
        assert scan_diff_secrets(files) == []

    def test_no_patch_skipped(self):
        files = [{"filename": "image.png"}]
        assert scan_diff_secrets(files) == []

    def test_multiple_secrets_same_file(self):
        patch = "+AKIAIOSFODNN7EXAMPLE\n+-----BEGIN RSA PRIVATE KEY-----"
        files = [{"filename": "leak.py", "patch": patch}]
        findings = scan_diff_secrets(files)
        assert len(findings) >= 2


# ── Secret leak check integration ────────────────────────────────────────────


class TestCheckSecretLeak:

    def test_detects_secret(self):
        files = [{"filename": "config.py", "patch": "+AKIAIOSFODNN7EXAMPLE"}]
        result = check_secret_leak(_ctx(files=files))
        assert result.passed is False
        assert "AWS Access Key ID" in result.reason

    def test_clean(self):
        files = [{"filename": "main.py", "patch": "+x = 42"}]
        result = check_secret_leak(_ctx(files=files))
        assert result.passed is True

    def test_disabled(self):
        cfg = parse_config("checks:\n  secret_leak:\n    enabled: false\n")
        files = [{"filename": "config.py", "patch": "+AKIAIOSFODNN7EXAMPLE"}]
        assert check_secret_leak(_ctx(files=files, config=cfg)) is None

    def test_multiple_findings_reported(self):
        files = [
            {"filename": "a.py", "patch": "+AKIAIOSFODNN7EXAMPLE"},
            {"filename": "b.py", "patch": "+-----BEGIN RSA PRIVATE KEY-----"},
        ]
        result = check_secret_leak(_ctx(files=files))
        assert result.passed is False
        assert "2 secret(s)" in result.reason


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
    )
    results = run_checks(ctx)
    names = {r.name for r in results}
    assert "shared_code" in names
    assert "critical_path" in names
    assert "codeowners" in names
    assert "secret_leak" in names
