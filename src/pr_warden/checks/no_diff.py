import re

from pr_warden.checks.registry import CheckContext, CheckResult, Severity, register

# ── Existing quality gates ─────────────────────────────────────────────────────

_GENERIC_TITLES = {"fix", "update", "wip", "hotfix", "patch", "changes", "stuff", "misc"}


@register(Severity.LOW)
def check_title_quality(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.title_quality.enabled:
        return None
    title = ctx.pr.title.strip()
    if not title:
        return CheckResult("title_quality", False, "Title is empty")
    words = title.split()
    if len(words) == 1:
        if title.lower() in _GENERIC_TITLES:
            return CheckResult("title_quality", False, f'Generic single-word title: "{title}"')
        return CheckResult("title_quality", False, "Title is a single word")
    return CheckResult("title_quality", True, "")


@register(Severity.LOW)
def check_description(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.description
    if not cfg.enabled:
        return None
    body = (ctx.pr.body or "").strip()
    if len(body) < cfg.min_chars:
        return CheckResult(
            "description", False,
            f"Body is empty or too short (< {cfg.min_chars} chars)",
        )
    return CheckResult("description", True, "")


@register(Severity.LOW)
def check_pr_size(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.pr_size
    if not cfg.enabled:
        return None
    pr = ctx.pr
    if pr.changed_files > cfg.file_limit:
        return CheckResult(
            "pr_size", False,
            f"Too many files: {pr.changed_files} (limit {cfg.file_limit})",
        )
    total_lines = pr.additions + pr.deletions
    if total_lines > cfg.line_limit:
        return CheckResult(
            "pr_size", False,
            f"Too many lines: {total_lines} (limit {cfg.line_limit})",
        )
    return CheckResult(
        "pr_size", True,
        f"{pr.changed_files} files, {pr.additions + pr.deletions} lines",
    )


@register(Severity.LOW)
def check_branch_naming(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.branch_naming
    if not cfg.enabled:
        return None
    branch = ctx.pr.head.ref
    pattern = re.compile(rf"^({'|'.join(re.escape(p) for p in cfg.allowed_prefixes)})/")
    if not pattern.match(branch):
        return CheckResult(
            "branch_naming", False,
            f'"{branch}" must start with {"/".join(cfg.allowed_prefixes)}/',
        )
    return CheckResult("branch_naming", True, "")


@register(Severity.LOW)
def check_self_merge(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.self_merge.enabled:
        return None
    author = ctx.pr.user.login
    reviewers = [r.login for r in ctx.pr.requested_reviewers]
    if not reviewers:
        return CheckResult("self_merge", False, "No reviewers assigned")
    if reviewers == [author]:
        return CheckResult("self_merge", False, f"Only reviewer is the author ({author})")
    return CheckResult("self_merge", True, "")


# ── Anti-slop: AI fingerprints ─────────────────────────────────────────────────


@register(Severity.LOW)
def check_ai_branch(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.ai_branch
    if not cfg.enabled:
        return None
    branch = ctx.pr.head.ref
    pattern = re.compile(
        rf"^({'|'.join(re.escape(t) for t in cfg.tools)})[-/]",
        re.IGNORECASE,
    )
    if pattern.match(branch):
        return CheckResult("ai_branch", False, f'Branch "{branch}" looks AI-generated')
    return CheckResult("ai_branch", True, "")


_AI_FOOTER_RE = re.compile(
    r"Co-Authored-By:\s*(Claude|Cursor|Copilot|Devin|GitHub Copilot)"
    r"|Generated with\s+\S+",
    re.IGNORECASE,
)


@register(Severity.LOW)
def check_ai_commit_footer(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.ai_commit_footer.enabled:
        return None
    for commit in ctx.commits:
        message = commit.get("commit", {}).get("message", "")
        if _AI_FOOTER_RE.search(message):
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


@register(Severity.LOW)
def check_boilerplate_description(ctx: CheckContext) -> CheckResult | None:
    if not ctx.config.checks.boilerplate_description.enabled:
        return None
    body = ctx.pr.body or ""
    for pattern in _BOILERPLATE_RES:
        if pattern.search(body):
            return CheckResult(
                "boilerplate_description", False,
                "Description contains unfilled template text",
            )
    return CheckResult("boilerplate_description", True, "")


# ── Anti-slop: description too thin for the diff ──────────────────────────────


@register(Severity.MEDIUM)
def check_description_vs_diff(ctx: CheckContext) -> CheckResult | None:
    cfg = ctx.config.checks.description_vs_diff
    if not cfg.enabled:
        return None
    pr = ctx.pr
    body_len = len((pr.body or "").strip())
    diff_lines = pr.additions + pr.deletions
    if body_len < cfg.min_desc_chars and diff_lines > cfg.min_diff_lines:
        return CheckResult(
            "description_vs_diff", False,
            f"Description is {body_len} chars for a {diff_lines}-line diff",
        )
    return CheckResult("description_vs_diff", True, "")
