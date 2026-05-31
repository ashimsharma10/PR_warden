from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from pr_warden.github.schemas import PullRequest
from pr_warden.repo_config import DEFAULT_CONFIG, RepoConfig


class Severity(IntEnum):
    """How hard a failing check should pull the maintainer's attention.

    IntEnum so severities order and compare directly (`>= Severity.MEDIUM`).

    - LOW    — hygiene / slop nit. Surfaced, but on its own it must NOT flip the
               PR to needs-attention; flagging every imperfect PR is the noise
               that makes a reviewer ignore the bot.
    - MEDIUM — likely worth a human glance; may well be fine.
    - HIGH   — concrete, higher-confidence risk (e.g. a leaked secret, a touch to
               a repo-declared critical path). Always draws attention.
    """

    LOW = 1
    MEDIUM = 2
    HIGH = 3


# A failing check at or above this severity flips the PR to needs-attention.
# Below it (LOW), the finding is advisory and shown but does not change status.
ATTENTION_THRESHOLD = Severity.MEDIUM


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str
    # Stamped by run_checks from the check's registration. The default applies
    # only to results built by hand (e.g. in tests) and mirrors run_checks's
    # default so behaviour is consistent either way.
    severity: Severity = Severity.MEDIUM


@dataclass
class CheckContext:
    pr: PullRequest
    files: list[dict] = field(default_factory=list)    # GET /pulls/{n}/files
    commits: list[dict] = field(default_factory=list)  # GET /pulls/{n}/commits
    config: RepoConfig = field(default_factory=lambda: DEFAULT_CONFIG)
    repo_tree: list[str] = field(default_factory=list)
    codeowners_raw: str | None = None
    # None  → gitleaks binary not available, scan was skipped
    # []    → gitleaks ran, no secrets found
    # [...] → gitleaks ran, findings present
    gitleaks_findings: list[dict] | None = None


# A check returns None when disabled by config; otherwise a CheckResult.
CheckFn = Callable[[CheckContext], CheckResult | None]

# Each check is registered with the severity its failures carry.
_registry: list[tuple[CheckFn, Severity]] = []


def register(severity: Severity = Severity.MEDIUM) -> Callable[[CheckFn], CheckFn]:
    """Register a check and declare the severity of its failures.

    Used as `@register(Severity.LOW)`. Severity is a property of the check, not
    of any single result, so it lives here at registration — the one place that
    decides how much weight a failure gets.
    """

    def decorator(fn: CheckFn) -> CheckFn:
        _registry.append((fn, severity))
        return fn

    return decorator


def run_checks(ctx: CheckContext) -> list[CheckResult]:
    results = []
    for fn, severity in _registry:
        r = fn(ctx)
        if r is not None:
            r.severity = severity  # registration is the single source of truth
            results.append(r)
    return results
