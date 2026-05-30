"""Static security analysis as a tool — deterministic pattern matching, not LLM
reasoning about whether code is "secure."

The division of labour is the whole point: Semgrep does the *finding*
(exhaustive enumeration + AST pattern matching, which LLMs are bad at); the agent
does the *judgement* (does this matter for this PR, how to explain it). Two rules
this implementation follows that most get wrong:

- Scan the new state (head SHA), full files — Semgrep is AST-based and can't
  parse bare diff fragments.
- Then filter findings to lines THIS PR changed. The author isn't responsible
  for pre-existing issues; surfacing them is noise.

Degrades like the gitleaks check: if the `semgrep` binary isn't installed, the
scan is skipped (reported, never fatal).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path

import structlog

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import ToolInput, ToolResult
from pr_warden.config import settings
from pr_warden.github import client

log = structlog.get_logger()

# Semgrep is slower than gitleaks and may download rule packs on first run, so it
# needs more than the default 15s per-tool budget.
_TOOL_TIMEOUT_S = 90.0
_SEMGREP_TIMEOUT_S = 75.0
_MAX_FILES = 50
_MAX_FINDINGS_SHOWN = 12
_SNIPPET_CAP = 160

# Curated ruleset (issue #9), tuned against this repo + 7 well-audited libraries
# (fastapi, starlette, sqlalchemy, httpx, pydantic, anthropic, uvicorn):
#   - p/security-audit is the leanest broad pack here; p/default was ~4x noisier
#     (framework rules like raw-SQL/formatted-SQL), so it was rejected.
#   - p/secrets adds credential detection at near-zero false-positive cost.
#   - p/owasp-top-ten + p/cwe-top-25 close real coverage gaps (e.g. weak-hash
#     md5, which the base packs missed on a deliberately-vulnerable corpus).
# Registry packs (not vendored): the false-positive set is small and well
# characterised, and the tool already degrades gracefully when offline, so
# vendoring would cost rule freshness for little gain. Revisit if fully-offline
# operation becomes a hard requirement.
_DEFAULT_SEMGREP_CONFIGS = (
    "p/security-audit",
    "p/secrets",
    "p/owasp-top-ten",
    "p/cwe-top-25",
)

# "Presence detector" audit rules: they flag the mere use of a construct with no
# taint/dataflow analysis, so they fire constantly on legitimate code (19 of 20
# findings across the audited-library corpus were these three). A maintainer who
# uses pickle/exec/eval deliberately does not need a bot flagging it on every PR.
# Matched as substrings of the Semgrep check_id.
_DEFAULT_EXCLUDED_RULES = (
    "avoid-pickle",
    "exec-detected",
    "eval-detected",
)

_SEVERITY = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def semgrep_configs() -> tuple[str, ...]:
    """Active rule packs: SEMGREP_CONFIGS override, else the curated defaults."""
    return _split_csv(settings.semgrep_configs) or _DEFAULT_SEMGREP_CONFIGS


# Sentinels for SEMGREP_EXCLUDE_RULES that mean "exclude nothing" — lets an
# operator turn OFF the default exclusions (e.g. when auditing a security-
# sensitive repo where pickle/exec/eval matter). Empty string keeps defaults.
_NO_EXCLUSIONS = {"none", "off", "-"}


def excluded_rules() -> tuple[str, ...]:
    """Rule-id substrings to drop: SEMGREP_EXCLUDE_RULES override, else defaults.

    Unset/empty → the curated defaults; a sentinel ("none"/"off"/"-") → no
    exclusions; anything else → exactly that list.
    """
    override = settings.semgrep_exclude_rules.strip()
    if not override:
        return _DEFAULT_EXCLUDED_RULES
    if override.lower() in _NO_EXCLUSIONS:
        return ()
    return _split_csv(settings.semgrep_exclude_rules)


def drop_excluded_rules(findings: list[dict], excluded: tuple[str, ...]) -> list[dict]:
    """Remove findings whose check_id contains any excluded substring."""
    if not excluded:
        return findings
    return [
        f for f in findings
        if not any(x in (f.get("check_id") or "") for x in excluded)
    ]

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")

_DENY_SUBSTRINGS = ("node_modules/", "vendor/", "dist/", "build/", ".min.")
_DENY_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Cargo.lock", "composer.lock",
}


def is_scannable(path: str) -> bool:
    """Skip lockfiles, vendored/generated/minified code — scan-time bloat and
    false-positive factories."""
    name = path.rsplit("/", 1)[-1]
    if name in _DENY_NAMES or path.endswith(".lock"):
        return False
    return not any(s in path for s in _DENY_SUBSTRINGS)


def added_line_numbers(patch: str) -> set[int]:
    """Line numbers in the *new* file that this patch adds (not context, not
    removals). Parsed from unified-diff hunk headers."""
    added: set[int] = set()
    new_lineno = 0
    for line in patch.splitlines():
        header = _HUNK_HEADER.match(line)
        if header:
            new_lineno = int(header.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.add(new_lineno)
            new_lineno += 1
        elif line.startswith("-"):
            pass  # present only in the old file; new-file counter doesn't move
        else:
            new_lineno += 1  # context line
    return added


def semgrep_installed() -> bool:
    return shutil.which("semgrep") is not None


async def run_semgrep(
    files: dict[str, str], *, timeout: float = _SEMGREP_TIMEOUT_S
) -> list[dict] | None:
    """Run Semgrep over the given {path: content} map at head state.

    Returns the raw `results` list (paths made repo-relative), or None if the
    scanner is unavailable or errored — the caller distinguishes that from "ran,
    found nothing" ([]).
    """
    if not semgrep_installed():
        return None
    if not files:
        return []

    with tempfile.TemporaryDirectory(prefix="prwarden_sg_") as tmp:
        for path, content in files.items():
            dest = Path(tmp) / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8", errors="replace")

        config_args: list[str] = []
        for cfg in semgrep_configs():
            config_args += ["--config", cfg]

        proc = await asyncio.create_subprocess_exec(
            "semgrep", "scan", *config_args,
            "--json", "--quiet", "--disable-version-check", "--timeout", "10",
            tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("semgrep.timeout")
            return None
        finally:
            # Kill if still running: covers the timeout above *and* outer
            # cancellation (e.g. the agent's wall-clock cap), which raises
            # CancelledError here — without this, the scan is orphaned and keeps
            # burning CPU after we've stopped waiting for it.
            if proc.returncode is None:
                proc.kill()

        raw = stdout.decode().strip()
        if not raw:
            log.error("semgrep.no_output", returncode=proc.returncode, stderr=stderr.decode()[:500])
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.error("semgrep.parse_error", output=raw[:200])
            return None

        results = data.get("results", [])
        for r in results:
            if "path" in r:
                r["path"] = r["path"].removeprefix(tmp).lstrip("/")
        return results


def filter_to_changed_lines(
    findings: list[dict], changed_by_path: dict[str, set[int]]
) -> list[dict]:
    kept = []
    for f in findings:
        lines = changed_by_path.get(f.get("path", ""))
        if not lines:
            continue
        start = (f.get("start") or {}).get("line", 0)
        end = (f.get("end") or {}).get("line", start)
        if any(ln in lines for ln in range(start, end + 1)):
            kept.append(f)
    return kept


def _format_findings(findings: list[dict]) -> str:
    out = [f"Security scan found {len(findings)} finding(s) on changed lines:\n"]
    for f in findings[:_MAX_FINDINGS_SHOWN]:
        extra = f.get("extra") or {}
        sev = _SEVERITY.get(extra.get("severity", "INFO"), "LOW")
        snippet = (extra.get("lines") or "").strip().splitlines()
        code = (snippet[0][:_SNIPPET_CAP] if snippet else "").strip()
        loc = f"{f.get('path')}:{(f.get('start') or {}).get('line', '?')}"
        out.append(
            f"[{sev}] {f.get('check_id')}\n"
            f"  Location: {loc}\n"
            f"  Code: {code}\n"
            f"  Message: {extra.get('message', '')}"
        )
    if len(findings) > _MAX_FINDINGS_SHOWN:
        out.append(f"... (+{len(findings) - _MAX_FINDINGS_SHOWN} more)")
    return "\n\n".join(out)


class CheckSecurityPatternsInput(ToolInput):
    paths: list[str] | None = None  # if None, scans all changed files


class CheckSecurityPatternsTool:
    name = "check_security_patterns"
    timeout_s = _TOOL_TIMEOUT_S
    description = """Run static security analysis on the PR's changed files.
Returns findings from a curated set of security rules covering injection
attacks, credential leaks, unsafe deserialization, and similar issues. Findings
are restricted to lines this PR changed, so pre-existing issues aren't surfaced.

Call this when the PR touches code that:
- Handles user input (HTTP handlers, CLI args, parsing)
- Does authentication or authorization
- Reads/writes files based on user-controlled paths
- Executes shell commands, SQL queries, or templates
- Handles secrets, tokens, or credentials

Skip for docs, non-auth configuration, or test-only changes."""
    input_schema = CheckSecurityPatternsInput

    async def run(self, ctx: PRContext, input: CheckSecurityPatternsInput) -> ToolResult:
        if not semgrep_installed():
            return ToolResult(
                ok=True,
                content="Security scan skipped: the semgrep scanner is not available.",
                metadata={"skipped": "semgrep_not_installed"},
            )

        changed_by_path = {
            f["filename"]: added_line_numbers(f.get("patch") or "")
            for f in ctx.files
            if f.get("patch") and is_scannable(f["filename"])
        }
        candidates = list(changed_by_path)
        if input.paths is not None:
            candidates = [p for p in input.paths if p in changed_by_path]
        candidates = candidates[:_MAX_FILES]

        if not candidates:
            return ToolResult(ok=True, content="No scannable changed files in this PR.")

        fetched = await asyncio.gather(
            *(client.get_repo_file(ctx.token, ctx.repo, p, ref=ctx.head_sha) for p in candidates),
            return_exceptions=True,
        )
        contents = {
            path: content
            for path, content in zip(candidates, fetched)
            if isinstance(content, str)
        }

        findings = await run_semgrep(contents)
        if findings is None:
            return ToolResult(
                ok=True,
                content="Security scan unavailable: the scanner could not complete.",
                metadata={"skipped": "semgrep_error"},
            )

        findings = drop_excluded_rules(findings, excluded_rules())
        in_diff = filter_to_changed_lines(findings, changed_by_path)
        if not in_diff:
            note = (
                f"Security scan: {len(findings)} finding(s) in scanned files, "
                "but none on lines this PR changed."
                if findings
                else "Security scan: no findings in changed files."
            )
            return ToolResult(
                ok=True,
                content=note,
                metadata={"files_scanned": len(contents), "findings_total": len(findings)},
            )

        return ToolResult(
            ok=True,
            content=_format_findings(in_diff),
            metadata={
                "files_scanned": len(contents),
                "findings_total": len(findings),
                "findings_in_diff": len(in_diff),
            },
        )
