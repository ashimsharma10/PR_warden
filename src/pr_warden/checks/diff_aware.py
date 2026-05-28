import fnmatch
import re
from pathlib import PurePosixPath

from pr_warden.checks.registry import CheckContext, CheckResult, register

# ── Helpers ────────────────────────────────────────────────────────────────────

_METADATA_STEMS = frozenset(
    {"README", "LICENSE", "SECURITY", "CODE_OF_CONDUCT", "CONTRIBUTING"}
)
_LOCK_FILES = frozenset({
    "package-lock.json", "yarn.lock", "Cargo.lock", "poetry.lock",
    "Pipfile.lock", "composer.lock", "Gemfile.lock", "pnpm-lock.yaml",
    "packages.lock.json",
})

_TEST_PATH_RE = re.compile(
    r"(^|/)(__tests__|tests?)/|"
    r"_test\.(py|go|js|ts|rb|java|cs|cpp)$|"
    r"\.(test|spec)\.(js|ts|jsx|tsx)$",
    re.IGNORECASE,
)
_SOURCE_EXT_RE = re.compile(
    r"\.(py|go|js|ts|jsx|tsx|rb|java|cs|cpp|c|rs|php|swift|kt)$",
    re.IGNORECASE,
)
_FUNC_DEF_RE = re.compile(
    r"^\+\s*(def |async def |func |function |public |private |protected )",
)
_EMPTY_BODY_RE = re.compile(
    r"^\+\s*(pass|return None|return null|return nil|"
    r"throw new \w+\(\)|// TODO.*|# TODO.*)\s*$",
)
_WEAKENED_ASSERTION_RE = re.compile(
    r"(assertEqual|assertEquals|assert_equal|assertThat|"
    r"expect.*\.toBe|expect.*\.toEqual|assert\.equal|"
    r"should\.equal|\.toBe\(|\.toEqual\()",
    re.IGNORECASE,
)


def _is_metadata_file(filename: str) -> bool:
    p = PurePosixPath(filename)
    return p.suffix == ".md" or p.stem.upper() in _METADATA_STEMS


def _is_test_file(filename: str) -> bool:
    return bool(_TEST_PATH_RE.search(filename))


def _is_source_file(filename: str) -> bool:
    return bool(_SOURCE_EXT_RE.search(filename)) and not _is_test_file(filename)


def _matches_any_glob(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


# ── Check #5: trivial metadata-only diff ──────────────────────────────────────


@register
def check_trivial_metadata_diff(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.trivial_metadata_diff
    if not cfg.enabled:
        return None
    if not ctx.files:
        return CheckResult("trivial_metadata_diff", True, "")
    if not all(_is_metadata_file(f["filename"]) for f in ctx.files):
        return CheckResult("trivial_metadata_diff", True, "")
    total = ctx.pr.additions + ctx.pr.deletions
    if total < cfg.max_lines:
        return CheckResult(
            "trivial_metadata_diff", False,
            f"Only documentation files changed ({total} lines total)",
        )
    return CheckResult("trivial_metadata_diff", True, "")


# ── Check #9: no tests for code change ────────────────────────────────────────


@register
def check_no_tests(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.no_tests
    if not cfg.enabled:
        return None
    if not ctx.files:
        return CheckResult("no_tests", True, "")

    filenames = [f["filename"] for f in ctx.files]
    # Exclude exempt paths from the source-file population
    source_files = [
        fn for fn in filenames
        if _is_source_file(fn) and not _matches_any_glob(fn, cfg.exempt_paths)
    ]
    tests_changed = any(_is_test_file(fn) for fn in filenames)

    if source_files and not tests_changed:
        return CheckResult("no_tests", False, "Source files changed with no test file changes")
    return CheckResult("no_tests", True, "")


# ── Check #10: test assertions weakened ───────────────────────────────────────


@register
def check_test_assertions_weakened(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.test_assertions_weakened.enabled:
        return None
    for f in ctx.files:
        if not _is_test_file(f["filename"]):
            continue
        patch = f.get("patch", "")
        removed = sum(
            1 for line in patch.splitlines()
            if line.startswith("-") and _WEAKENED_ASSERTION_RE.search(line)
        )
        if removed:
            return CheckResult(
                "test_assertions_weakened", False,
                f"{removed} assertion(s) removed in {f['filename']}",
            )
    return CheckResult("test_assertions_weakened", True, "")


# ── Check #12: empty function bodies ──────────────────────────────────────────


@register
def check_empty_function_bodies(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.empty_function_bodies.enabled:
        return None
    for f in ctx.files:
        patch = f.get("patch", "")
        if not patch:
            continue
        lines = patch.splitlines()
        for i, line in enumerate(lines):
            if not _FUNC_DEF_RE.match(line):
                continue
            subsequent_added = [l for l in lines[i + 1 : i + 5] if l.startswith("+")]
            if subsequent_added and _EMPTY_BODY_RE.match(subsequent_added[0]):
                return CheckResult(
                    "empty_function_bodies", False,
                    f"New function with empty/stub body in {f['filename']}",
                )
    return CheckResult("empty_function_bodies", True, "")


# ── Check #13: dependency-only churn ──────────────────────────────────────────


@register
def check_dependency_only_churn(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.dependency_only_churn.enabled:
        return None
    if not ctx.files:
        return CheckResult("dependency_only_churn", True, "")
    basenames = {PurePosixPath(f["filename"]).name for f in ctx.files}
    if basenames and basenames.issubset(_LOCK_FILES):
        return CheckResult(
            "dependency_only_churn", False,
            "Only lock files changed — no source or description justification",
        )
    return CheckResult("dependency_only_churn", True, "")
