import re

from pr_warden.checks.registry import CheckContext, CheckResult, register

# ── Existing quality gates ─────────────────────────────────────────────────────

_GENERIC_TITLES = {"fix", "update", "wip", "hotfix", "patch", "changes", "stuff", "misc"}
_BRANCH_PREFIX_RE = re.compile(r"^(feat|fix|chore|docs|refactor|test|ci|build)/")

PR_SIZE_FILE_LIMIT = 25
PR_SIZE_LINE_LIMIT = 500


@register
def check_title_quality(ctx: CheckContext) -> CheckResult:
    title = ctx.pr.title.strip()
    if not title:
        return CheckResult("title_quality", False, "Title is empty")
    words = title.split()
    if len(words) == 1:
        if title.lower() in _GENERIC_TITLES:
            return CheckResult("title_quality", False, f'Generic single-word title: "{title}"')
        return CheckResult("title_quality", False, "Title is a single word")
    return CheckResult("title_quality", True, "")


@register
def check_description(ctx: CheckContext) -> CheckResult:
    body = (ctx.pr.body or "").strip()
    if len(body) < 20:
        return CheckResult("description", False, "Body is empty or too short (< 20 chars)")
    return CheckResult("description", True, "")


@register
def check_pr_size(ctx: CheckContext) -> CheckResult:
    pr = ctx.pr
    if pr.changed_files > PR_SIZE_FILE_LIMIT:
        return CheckResult(
            "pr_size", False,
            f"Too many files: {pr.changed_files} (limit {PR_SIZE_FILE_LIMIT})",
        )
    total_lines = pr.additions + pr.deletions
    if total_lines > PR_SIZE_LINE_LIMIT:
        return CheckResult(
            "pr_size", False,
            f"Too many lines: {total_lines} (limit {PR_SIZE_LINE_LIMIT})",
        )
    return CheckResult(
        "pr_size", True,
        f"{pr.changed_files} files, {pr.additions + pr.deletions} lines",
    )


@register
def check_branch_naming(ctx: CheckContext) -> CheckResult:
    branch = ctx.pr.head.ref
    if not _BRANCH_PREFIX_RE.match(branch):
        return CheckResult(
            "branch_naming", False,
            f'"{branch}" must start with feat/fix/chore/docs/refactor/test/ci/build/',
        )
    return CheckResult("branch_naming", True, "")


@register
def check_self_merge(ctx: CheckContext) -> CheckResult:
    author = ctx.pr.user.login
    reviewers = [r.login for r in ctx.pr.requested_reviewers]
    if not reviewers:
        return CheckResult("self_merge", False, "No reviewers assigned")
    if reviewers == [author]:
        return CheckResult("self_merge", False, f"Only reviewer is the author ({author})")
    return CheckResult("self_merge", True, "")


# ── Anti-slop: AI fingerprints ─────────────────────────────────────────────────

_AI_BRANCH_RE = re.compile(
    r"^(claude|codex|cursor|copilot|devin)[-/]", re.IGNORECASE
)


@register
def check_ai_branch(ctx: CheckContext) -> CheckResult:
    branch = ctx.pr.head.ref
    if _AI_BRANCH_RE.match(branch):
        return CheckResult("ai_branch", False, f'Branch "{branch}" looks AI-generated')
    return CheckResult("ai_branch", True, "")


_AI_FOOTER_RE = re.compile(
    r"Co-Authored-By:\s*(Claude|Cursor|Copilot|Devin|GitHub Copilot)"
    r"|Generated with\s+\S+",
    re.IGNORECASE,
)


@register
def check_ai_commit_footer(ctx: CheckContext) -> CheckResult:
    for commit in ctx.commits:
        message = commit.get("commit", {}).get("message", "")
        m = _AI_FOOTER_RE.search(message)
        if m:
            short = message.split("\n")[0][:72]
            return CheckResult(
                "ai_commit_footer", False,
                f'AI-generated commit detected: "{short}"',
            )
    return CheckResult("ai_commit_footer", True, "")


# ── Anti-slop: boilerplate description ────────────────────────────────────────

_BOILERPLATE_RES = [
    re.compile(r"add\s+a\s+description", re.IGNORECASE),
    re.compile(r"describe\s+(your\s+)?changes", re.IGNORECASE),
    re.compile(r"please\s+include\s+a\s+summary", re.IGNORECASE),
    re.compile(r"what\s+does\s+this\s+(pr|pull\s+request)\s+do", re.IGNORECASE),
    re.compile(r"\[describe[^\]]{0,40}\]", re.IGNORECASE),
    re.compile(r"\[your[^\]]{0,40}here\]", re.IGNORECASE),
    re.compile(r"<!--[^>]{0,80}(what|describe|explain|add|provide)[^>]{0,80}-->", re.IGNORECASE),
]


@register
def check_boilerplate_description(ctx: CheckContext) -> CheckResult:
    body = ctx.pr.body or ""
    for pattern in _BOILERPLATE_RES:
        if pattern.search(body):
            return CheckResult(
                "boilerplate_description", False,
                "Description contains unfilled template text",
            )
    return CheckResult("boilerplate_description", True, "")


# ── Anti-slop: description too thin for the diff ──────────────────────────────

_DESC_MIN_CHARS = 50
_DIFF_MIN_LINES = 100


@register
def check_description_vs_diff(ctx: CheckContext) -> CheckResult:
    pr = ctx.pr
    body_len = len((pr.body or "").strip())
    diff_lines = pr.additions + pr.deletions
    if body_len < _DESC_MIN_CHARS and diff_lines > _DIFF_MIN_LINES:
        return CheckResult(
            "description_vs_diff", False,
            f"Description is {body_len} chars for a {diff_lines}-line diff",
        )
    return CheckResult("description_vs_diff", True, "")
