"""Per-repo `.github/prwarden.yml` config — schema, defaults, loader.

Each repo using PRwarden can ship a YAML config that overrides defaults
on a per-check basis. Missing file → defaults. Invalid YAML → defaults
with a logged warning. Unknown keys → silently ignored (forward-compat).
"""

from __future__ import annotations

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

log = structlog.get_logger()

CONFIG_PATH = ".github/prwarden.yml"


# ── Per-check sub-configs ──────────────────────────────────────────────────────


class CheckToggle(BaseModel):
    """Base shape: every check has at least an `enabled` flag."""
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True


class DescriptionConfig(CheckToggle):
    min_chars: int = 20


class PRSizeConfig(CheckToggle):
    file_limit: int = 25
    line_limit: int = 500


class BranchNamingConfig(CheckToggle):
    allowed_prefixes: list[str] = [
        "feat", "fix", "chore", "docs", "refactor", "test", "ci", "build",
    ]


class AIBranchConfig(CheckToggle):
    tools: list[str] = ["claude", "codex", "cursor", "copilot", "devin"]


class DescriptionVsDiffConfig(CheckToggle):
    min_desc_chars: int = 50
    min_diff_lines: int = 100


class TrivialMetadataConfig(CheckToggle):
    max_lines: int = 20


class NoTestsConfig(CheckToggle):
    exempt_paths: list[str] = []   # glob patterns; files matching skip the check


# ── Root config ────────────────────────────────────────────────────────────────


class ChecksConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title_quality: CheckToggle = CheckToggle()
    description: DescriptionConfig = DescriptionConfig()
    pr_size: PRSizeConfig = PRSizeConfig()
    branch_naming: BranchNamingConfig = BranchNamingConfig()
    self_merge: CheckToggle = CheckToggle()
    ai_branch: AIBranchConfig = AIBranchConfig()
    ai_commit_footer: CheckToggle = CheckToggle()
    boilerplate_description: CheckToggle = CheckToggle()
    description_vs_diff: DescriptionVsDiffConfig = DescriptionVsDiffConfig()
    trivial_metadata_diff: TrivialMetadataConfig = TrivialMetadataConfig()
    no_tests: NoTestsConfig = NoTestsConfig()
    test_assertions_weakened: CheckToggle = CheckToggle()
    empty_function_bodies: CheckToggle = CheckToggle()
    dependency_only_churn: CheckToggle = CheckToggle()


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    checks: ChecksConfig = ChecksConfig()


DEFAULT_CONFIG = RepoConfig()


# ── Parsing ────────────────────────────────────────────────────────────────────


def parse_config(raw_yaml: str | None) -> RepoConfig:
    """Parse a YAML string into a RepoConfig. Falls back to defaults on any error."""
    if not raw_yaml:
        return DEFAULT_CONFIG

    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as e:
        log.warning("repo_config.invalid_yaml", error=str(e))
        return DEFAULT_CONFIG

    if not isinstance(data, dict):
        log.warning("repo_config.not_a_mapping")
        return DEFAULT_CONFIG

    try:
        return RepoConfig.model_validate(data)
    except ValidationError as e:
        log.warning("repo_config.schema_error", error=str(e))
        return DEFAULT_CONFIG
