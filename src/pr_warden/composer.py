from pr_warden.checks.registry import CheckResult

LABEL_CLEAN = "prwarden:clean"
LABEL_NEEDS_ATTENTION = "prwarden:needs-attention"


def build_comment(results: list[CheckResult], summary: str | None = None) -> str:
    rows = []
    for r in results:
        icon = "✅" if r.passed else "❌"
        detail = r.reason if not r.passed else "—"
        name = r.name.replace("_", " ").title()
        rows.append(f"| {name} | {icon} | {detail} |")

    table = "\n".join(
        [
            "| Check | Status | Detail |",
            "|-------|--------|--------|",
            *rows,
        ]
    )

    parts = ["## PRwarden Review\n", table]

    if summary:
        parts.append(f"\n### Summary\n{summary}")

    parts.append("\n\n---\n*Powered by PRwarden · `/prwarden recheck` to re-run*")
    return "\n".join(parts)


def pick_label(results: list[CheckResult]) -> str:
    return LABEL_CLEAN if all(r.passed for r in results) else LABEL_NEEDS_ATTENTION
