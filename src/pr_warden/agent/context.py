"""The shared object every tool receives.

PRContext holds the PR data and the auth token tools need to reach GitHub.
It is pure data plus a couple of diff helpers — it owns no budget and makes no
model calls. The loop owns the budget; tools own their own I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pr_warden.github.schemas import PullRequest
from pr_warden.core.diff import build_diff_text


@dataclass
class PRContext:
    token: str                     # GitHub installation token, threaded to client calls
    repo: str                      # "owner/name" full name (what github.client expects)
    pr: PullRequest
    files: list[dict] = field(default_factory=list)  # GET /pulls/{n}/files
    check_findings: str = ""       # deterministic check results, as context for the model

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]

    @property
    def pr_number(self) -> int:
        return self.pr.number

    @property
    def head_sha(self) -> str:
        return self.pr.head.sha

    @property
    def base_sha(self) -> str:
        return self.pr.base.sha

    def full_diff(self) -> str:
        return build_diff_text(self.files)

    def file_diff(self, path: str) -> str | None:
        for f in self.files:
            if f.get("filename") == path:
                patch = f.get("patch")
                if not patch:
                    return f"--- {path} (no inline diff) ---"
                return f"--- {path} ---\n{patch}"
        return None
