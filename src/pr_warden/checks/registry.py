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
