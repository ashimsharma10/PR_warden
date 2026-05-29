from dataclasses import dataclass, field
from typing import Callable

from pr_warden.github.schemas import PullRequest
from pr_warden.repo_config import DEFAULT_CONFIG, RepoConfig


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str


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

_registry: list[CheckFn] = []


def register(fn: CheckFn) -> CheckFn:
    _registry.append(fn)
    return fn


def run_checks(ctx: CheckContext) -> list[CheckResult]:
    results = []
    for fn in _registry:
        r = fn(ctx)
        if r is not None:
            results.append(r)
    return results
