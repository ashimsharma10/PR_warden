from dataclasses import dataclass, field
from typing import Callable

from pr_warden.github.schemas import PullRequest


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


CheckFn = Callable[[CheckContext], CheckResult]

_registry: list[CheckFn] = []


def register(fn: CheckFn) -> CheckFn:
    _registry.append(fn)
    return fn


def run_checks(ctx: CheckContext) -> list[CheckResult]:
    return [fn(ctx) for fn in _registry]
