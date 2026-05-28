"""Stage 2: Static change impact analysis (~10s, $0).

Three checks that run alongside Stage 1 deterministic checks:

1. Blast radius   — flags changes to shared/utility files in large repos.
2. CODEOWNERS     — ensures required code owners are assigned as reviewers.
3. Secret scanning — hard gate that blocks PRs leaking credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pr_warden.checks.registry import CheckContext, CheckResult, register

# ── Source file detection ─────────────────────────────────────────────────────

_SOURCE_EXT = re.compile(
    r"\.(py|go|js|ts|jsx|tsx|rb|java|cs|cpp|c|rs|php|swift|kt)$",
    re.IGNORECASE,
)


# ── Secret scanning ──────────────────────────────────────────────────────────

_SENSITIVE_VAR = (
    r"(?:secret|password|passwd|pwd|token|api[_-]?key|apikey|"
    r"api[_-]?secret|private[_-]?key|access[_-]?key|auth[_-]?token|"
    r"credentials?|signing[_-]?key|encryption[_-]?key|client[_-]?secret|"
    r"service[_-]?account[_-]?key|master[_-]?key|bearer[_-]?token|"
    r"connection[_-]?string|db[_-]?pass|jwt[_-]?secret)"
)

_SECRET_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    # ── Platform-specific (high-confidence, zero false positives) ──────────
    ("aws-access-key-id", "AWS Access Key ID",
     re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-pat", "GitHub Personal Access Token",
     re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github-oauth", "GitHub OAuth Token",
     re.compile(r"gho_[A-Za-z0-9]{36}")),
    ("github-app-token", "GitHub App Token",
     re.compile(r"ghs_[A-Za-z0-9]{36}")),
    ("github-fine-pat", "GitHub Fine-Grained PAT",
     re.compile(r"github_pat_[A-Za-z0-9_]{82}")),
    ("github-refresh", "GitHub Refresh Token",
     re.compile(r"ghr_[A-Za-z0-9]{36}")),
    ("private-key", "Private Key",
     re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")),
    ("slack-bot-token", "Slack Bot Token",
     re.compile(r"xoxb-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24}")),
    ("slack-user-token", "Slack User Token",
     re.compile(r"xoxp-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24}")),
    ("stripe-live-secret", "Stripe Live Secret Key",
     re.compile(r"sk_live_[A-Za-z0-9]{24,}")),
    ("stripe-live-restricted", "Stripe Live Restricted Key",
     re.compile(r"rk_live_[A-Za-z0-9]{24,}")),
    ("google-api-key", "Google API Key",
     re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("db-connection-string", "Database Connection String",
     re.compile(
         r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|amqp)://"
         r"[^:\s]+:[^@\s]+@[^\s'\"]+",
         re.IGNORECASE,
     )),

    # ── Generic (catches any platform, any service) ───────────────────────
    ("generic-secret", "Hardcoded Secret",
     re.compile(
         r"(?i)" + _SENSITIVE_VAR + r"""\s*[=:]\s*['"]([^'"]{16,})['"]""",
     )),
    ("jwt-token", "JSON Web Token",
     re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")),
    ("generic-hex-secret", "Hex-Encoded Secret",
     re.compile(
         r"(?i)" + _SENSITIVE_VAR + r"""\s*[=:]\s*['"]([0-9a-f]{32,})['"]""",
     )),
]

_GENERIC_RULE_IDS = frozenset({"generic-secret", "generic-hex-secret"})

_PLACEHOLDER_RE = re.compile(
    r"(?i)(?:example|changeme|change[_-]me|placeholder|your[_-]|TODO|"
    r"xxxx|test[_-]?(?:key|token|secret|pass)|dummy|sample|"
    r"replace[_-]?me|insert[_-]|<[^>]+>|\$\{[^}]+\}|_{8,}|\.{8,}|\*{8,})",
)


@dataclass(frozen=True, slots=True)
class SecretFinding:
    file: str
    rule: str
    description: str


def scan_diff_secrets(files: list[dict]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for f in files:
        patch = f.get("patch", "")
        if not patch:
            continue
        added_lines = [
            line[1:]
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        text = "\n".join(added_lines)
        for rule_id, description, pattern in _SECRET_RULES:
            match = pattern.search(text)
            if not match:
                continue
            if rule_id in _GENERIC_RULE_IDS:
                value = match.group(1) if match.lastindex else match.group(0)
                if _PLACEHOLDER_RE.search(value):
                    continue
            findings.append(SecretFinding(f["filename"], rule_id, description))
    return findings


# ── CODEOWNERS parsing ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CodeownersRule:
    pattern: str
    owners: tuple[str, ...]


def parse_codeowners(raw: str) -> list[CodeownersRule]:
    rules: list[CodeownersRule] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owners = tuple(p for p in parts[1:] if p.startswith("@"))
        if owners:
            rules.append(CodeownersRule(pattern, owners))
    return rules


def _glob_to_regex(pattern: str) -> str:
    i, result = 0, []
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            result.append(".*")
            i += 3 if (i + 2 < len(pattern) and pattern[i + 2] == "/") else 2
        elif c == "*":
            result.append("[^/]*")
            i += 1
        elif c == "?":
            result.append("[^/]")
            i += 1
        elif c in r"\.+^${}|()\\":
            result.append("\\" + c)
            i += 1
        else:
            result.append(c)
            i += 1
    return "".join(result)


def _codeowners_match(filepath: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        prefix = pattern.lstrip("/")
        return filepath.startswith(prefix) or filepath == prefix.rstrip("/")

    rooted = pattern.startswith("/")
    stripped = pattern.lstrip("/")

    if "/" not in stripped:
        if rooted:
            return bool(re.fullmatch(_glob_to_regex(stripped), filepath))
        return bool(re.fullmatch(_glob_to_regex(stripped), PurePosixPath(filepath).name))

    return bool(re.fullmatch(_glob_to_regex(stripped), filepath))


def find_owners(filepath: str, rules: list[CodeownersRule]) -> list[str]:
    matched: list[str] = []
    for rule in rules:
        if _codeowners_match(filepath, rule.pattern):
            matched = list(rule.owners)
    return matched


# ── Critical-path glob matching ───────────────────────────────────────────────


def _critical_path_match(filepath: str, glob: str) -> bool:
    """Match filepath against a critical-path glob.

    Globs without '/' match against the basename (any depth in the tree).
    Globs with '/' match against the full path from the repo root.
    '**' matches any sequence of path components.
    """
    if "/" not in glob:
        return bool(re.fullmatch(_glob_to_regex(glob), PurePosixPath(filepath).name))
    return bool(re.fullmatch(_glob_to_regex(glob), filepath))


# ── Checks ────────────────────────────────────────────────────────────────────


@register
def check_shared_code(ctx: CheckContext) -> CheckResult | None:
    """Heuristic: flags source files living in conventionally-shared directories.

    This is a naming signal, not a dependency graph. It suggests extra scrutiny
    but cannot measure actual downstream impact. Use critical_path for
    deterministic coverage of declared high-risk areas.
    """
    cfg = ctx.config.checks.shared_code
    if not cfg.enabled:
        return None

    if not ctx.repo_tree:
        return CheckResult("shared_code", True, "Repository tree unavailable")

    total_source = sum(1 for f in ctx.repo_tree if _SOURCE_EXT.search(f))
    changed_source = [f["filename"] for f in ctx.files if _SOURCE_EXT.search(f["filename"])]
    if not changed_source:
        return CheckResult("shared_code", True, "No source files changed")

    shared_segments = set(cfg.shared_paths)
    in_shared = [
        fp for fp in changed_source
        if shared_segments.intersection(PurePosixPath(fp).parts[:-1])
    ]

    if not in_shared:
        return CheckResult(
            "shared_code", True,
            f"{len(changed_source)} source files changed ({total_source} in repo)",
        )

    names = ", ".join(in_shared[:5])
    overflow = f" (+{len(in_shared) - 5} more)" if len(in_shared) > 5 else ""
    return CheckResult(
        "shared_code", False,
        f"Files in shared directories (heuristic): {names}{overflow}"
        f" ({total_source} source files in repo)",
    )


@register
def check_critical_path(ctx: CheckContext) -> CheckResult | None:
    """Deterministic: flags any PR that touches repo-declared critical-path globs.

    Configure in .github/prwarden.yml:

        checks:
          critical_path:
            globs:
              - "auth/**"
              - "payments/**"
              - "migrations/**"
              - "*.tf"
              - ".github/workflows/**"
    """
    cfg = ctx.config.checks.critical_path
    if not cfg.enabled:
        return None

    if not cfg.globs:
        return CheckResult("critical_path", True, "No critical-path globs configured")

    hits: list[tuple[str, str]] = []
    for f in ctx.files:
        filepath = f["filename"]
        for glob in cfg.globs:
            if _critical_path_match(filepath, glob):
                hits.append((filepath, glob))
                break  # one match per file is enough

    if not hits:
        return CheckResult("critical_path", True, "No critical-path files changed")

    detail = ", ".join(f"{fp} [{g}]" for fp, g in hits[:5])
    overflow = f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""
    return CheckResult(
        "critical_path", False,
        f"Critical-path files changed: {detail}{overflow}",
    )


@register
def check_codeowners(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.codeowners
    if not cfg.enabled:
        return None

    if not ctx.codeowners_raw:
        return CheckResult("codeowners", True, "No CODEOWNERS file")

    rules = parse_codeowners(ctx.codeowners_raw)
    if not rules:
        return CheckResult("codeowners", True, "CODEOWNERS file has no rules")

    all_owners: set[str] = set()
    for f in ctx.files:
        all_owners.update(find_owners(f["filename"], rules))

    if not all_owners:
        return CheckResult("codeowners", True, "Changed files have no designated owners")

    if not cfg.require_review:
        return CheckResult(
            "codeowners", True,
            f"Owners: {', '.join(sorted(all_owners))}",
        )

    reviewer_logins = {r.login for r in ctx.pr.requested_reviewers}
    individual = {o.lstrip("@") for o in all_owners if "/" not in o}
    teams = sorted(o for o in all_owners if "/" in o)
    missing = sorted(individual - reviewer_logins)

    parts: list[str] = []
    if missing:
        parts.append(f"missing reviewers: {', '.join('@' + m for m in missing)}")
    if teams:
        parts.append(f"team reviews needed: {', '.join(teams)}")

    if missing:
        return CheckResult("codeowners", False, "; ".join(parts).capitalize())

    info = f"Required reviewers assigned"
    if teams:
        info += f"; {parts[-1]}"
    return CheckResult("codeowners", True, info)


@register
def check_secret_leak(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.secret_leak.enabled:
        return None

    findings = scan_diff_secrets(ctx.files)
    if not findings:
        return CheckResult("secret_leak", True, "No secrets detected")

    seen: list[str] = []
    for f in findings:
        seen.append(f"{f.description} in {f.file}")
    return CheckResult(
        "secret_leak", False,
        f"{len(findings)} secret(s) detected: {'; '.join(seen[:5])}",
    )
