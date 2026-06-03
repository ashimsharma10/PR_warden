import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import pr_warden.log as pr_warden_log
from pr_warden.agent.context import PRContext
from pr_warden.agent.loop import run_agent
from pr_warden.agent.schemas import AgentResult
from pr_warden.checks import CheckContext, run_checks
from pr_warden.checks.impact import run_gitleaks
from pr_warden.composer import (
    MANAGED_LABELS,
    LinkContext,
    build_comment,
    format_changes,
    pick_facet_labels,
    verdict_level,
)
from pr_warden.config import settings
from pr_warden.db import async_session_factory, engine, get_session
from pr_warden.github import auth, client
from pr_warden.github.schemas import PullRequestEvent
from pr_warden.github.webhooks import verify_signature
from pr_warden.models import PRCheck, Repo
from pr_warden.repo_config import CONFIG_PATH, DEFAULT_CONFIG, RepoConfig, parse_config

log = structlog.get_logger()

_REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened"}

# Serialize the "find existing comment → create-or-update" step per PR.
# _handle_pr_event runs as a fire-and-forget background task, so two near-
# simultaneous events for the same PR (e.g. rapid pushes, now widened by the
# agent's multi-second runtime) could both find no existing comment and each
# create one. A per-PR asyncio.Lock makes that step atomic. Single-instance bot,
# so an in-process lock suffices; the dict grows by one entry per PR seen.
_pr_comment_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _pr_comment_lock(repo: str, pr_number: int) -> asyncio.Lock:
    key = (repo, pr_number)
    lock = _pr_comment_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _pr_comment_locks[key] = lock
    return lock


@asynccontextmanager
async def lifespan(app: FastAPI):
    pr_warden_log.configure_logging()
    log.info("pr_warden.startup", app_id=settings.github_app_id)
    yield
    await client.close()
    await engine.dispose()


app = FastAPI(title="PRwarden", lifespan=lifespan)


@app.post("/webhook", status_code=200)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    trace_id = pr_warden_log.new_trace_id()
    structlog.contextvars.bind_contextvars(trace_id=trace_id, event_type=x_github_event)

    if x_github_event == "pull_request":
        data = json.loads(body)
        event = PullRequestEvent.model_validate(data)
        if event.action in _REVIEWABLE_ACTIONS and not event.pull_request.draft:
            background_tasks.add_task(_handle_pr_event, event, trace_id)

    return {"ok": True}


async def _load_repo_config(token: str, repo: str) -> RepoConfig:
    try:
        raw = await client.get_repo_file(token, repo, CONFIG_PATH)
    except Exception:
        log.exception("repo_config.fetch_failed")
        return DEFAULT_CONFIG
    return parse_config(raw)


async def _build_check_context(
    token: str, repo: str, event: PullRequestEvent, config: RepoConfig
) -> CheckContext:
    base_branch = event.pull_request.base.ref
    files, commits, repo_tree, codeowners = await asyncio.gather(
        client.list_pr_files(token, repo, event.number),
        client.list_pr_commits(token, repo, event.number),
        client.list_repo_tree(token, repo, base_branch),
        client.get_codeowners(token, repo),
        return_exceptions=True,
    )

    actual_files = files if isinstance(files, list) else []

    # run_gitleaks needs the file list, so it runs after the first gather
    gitleaks_findings = await run_gitleaks(actual_files)

    return CheckContext(
        pr=event.pull_request,
        files=actual_files,
        commits=commits if isinstance(commits, list) else [],
        config=config,
        repo_tree=repo_tree if isinstance(repo_tree, list) else [],
        codeowners_raw=codeowners if isinstance(codeowners, str) else None,
        gitleaks_findings=gitleaks_findings,
    )


async def _today_agent_cost() -> float:
    """Sum of agent cost recorded so far today (UTC) — the daily-budget gate."""
    start = datetime.combine(
        datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc
    )
    async with async_session_factory() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(PRCheck.cost_usd), 0.0)).where(
                PRCheck.created_at >= start
            )
        )
    return float(total or 0.0)


def _format_check_findings(results: list) -> str:
    """The deterministic check results as a compact block for the agent's context:
    failures first (with reason), then a one-line tally of what passed."""
    failed = [r for r in results if not r.passed]
    passed = [r for r in results if r.passed]
    lines = [f"- FAIL {r.name}: {r.reason}" for r in failed]
    if passed:
        lines.append(f"- ({len(passed)} other checks passed)")
    return "\n".join(lines) if lines else "(no checks reported)"


async def _maybe_run_agent(
    token: str, repo: str, event: PullRequestEvent, files: list[dict], check_findings: str = ""
) -> AgentResult | None:
    """Run the review agent if it's allowlisted, configured, and under budget.

    Every failure mode — disabled, no key, over budget, timeout, or a crash in
    the run — returns None so the deterministic checks still post. The agent is
    additive; it must never break the base pipeline.
    """
    if not settings.agent_enabled_for(repo):
        return None
    if not settings.anthropic_api_key:
        log.info("agent.skipped", reason="no_api_key")
        return None

    spent = await _today_agent_cost()
    if spent >= settings.daily_cost_limit_usd:
        log.warning("agent.skipped", reason="daily_budget", spent_usd=round(spent, 4))
        return None

    pr_ctx = PRContext(
        token=token, repo=repo, pr=event.pull_request, files=files,
        check_findings=check_findings,
    )
    try:
        result = await asyncio.wait_for(
            run_agent(
                pr_ctx,
                api_key=settings.anthropic_api_key,
                model=settings.agent_model,
            ),
            timeout=settings.agent_timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning("agent.timeout", timeout_s=settings.agent_timeout_s)
        return None
    except Exception:
        log.exception("agent.run_failed")
        return None

    log.info(
        "agent.completed",
        stopped_for=result.stopped_for,
        cost_usd=round(result.cost_usd, 4),
        tool_calls=result.tool_call_count,
        duration_ms=result.duration_ms,
    )
    return result


async def _handle_pr_event(event: PullRequestEvent, trace_id: str) -> None:
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        repo=event.repository.full_name,
        pr=event.number,
    )
    log.info("pr_event.processing", action=event.action)

    try:
        token = await auth.get_installation_token(event.installation.id)
        repo = event.repository.full_name

        config = await _load_repo_config(token, repo)
        ctx = await _build_check_context(token, repo, event, config)
        results = run_checks(ctx)
        esc = config.advisory_escalation
        advisory_threshold = esc.threshold if esc.enabled else None

        # The deterministic checks are context for the agent — its consolidated
        # review speaks for them (no separate check table in the comment).
        agent_result = await _maybe_run_agent(
            token, repo, event, ctx.files, check_findings=_format_check_findings(results)
        )
        agent_assessment = agent_result.assessment if agent_result else None
        # A force-finalized run (budget/timeout/no-tool) returns a fallback
        # assessment; flag it so the verdict reads ⚠️ Inconclusive, not a false 🟢,
        # and so an incomplete run never escalates the status label.
        agent_complete = agent_result is not None and agent_result.stopped_for == "done"

        # The verdict headline and the facet labels both derive from this concern
        # level (see _concern); we record it per run for /stats. It is no longer
        # turned into a status label — that's retired.
        level = verdict_level(
            results,
            agent_assessment,
            agent_complete=agent_complete,
            advisory_threshold=advisory_threshold,
        )
        # Cited `path:line`s link straight to the line, but only for files that
        # exist here (changed files ∪ repo tree) — never a broken link.
        link_ctx = LinkContext(
            repo=repo,
            sha=ctx.pr.head.sha,
            known_paths=frozenset({f["filename"] for f in ctx.files} | set(ctx.repo_tree)),
        )

        async with _pr_comment_lock(repo, event.number), async_session_factory() as session:
            repo_row = await _get_or_create_repo(session, event.installation.id, repo)
            repo_row.cached_config = config.model_dump()

            existing_comment_id = await session.scalar(
                select(PRCheck.comment_id)
                .where(PRCheck.repo_id == repo_row.id)
                .where(PRCheck.pr_number == event.number)
                .where(PRCheck.comment_id.isnot(None))
                .order_by(PRCheck.created_at.desc())
            )

            # "Since last review": diff this run's checks against the most recent
            # run on a different commit, so a returning reviewer sees what changed.
            prev_run = await session.scalar(
                select(PRCheck)
                .where(PRCheck.repo_id == repo_row.id)
                .where(PRCheck.pr_number == event.number)
                .where(PRCheck.sha != ctx.pr.head.sha)
                .order_by(PRCheck.created_at.desc())
            )
            changes = (
                format_changes(prev_run.check_results, results, prev_run.sha, ctx.pr.head.sha)
                if prev_run
                else None
            )
            comment_body = build_comment(
                results,
                agent=agent_assessment,
                agent_complete=agent_complete,
                advisory_threshold=advisory_threshold,
                changes=changes,
                link_ctx=link_ctx,
            )

            if existing_comment_id:
                await client.update_comment(token, repo, existing_comment_id, comment_body)
                comment_id = existing_comment_id
            else:
                comment_id = await client.create_comment(token, repo, event.number, comment_body)

            # Apply facet labels only — the overall read lives in the verdict
            # headline, not a generic status label. Reconcile against the full
            # managed set so stale facets and any retired status label from an
            # earlier version get stripped. The verdict `level` is recorded for
            # /stats analytics, never applied as a label. Both ops are idempotent.
            desired_labels = set(
                pick_facet_labels(results, agent_assessment, agent_complete=agent_complete)
            )
            for lbl in MANAGED_LABELS - desired_labels:
                await client.remove_label(token, repo, event.number, lbl)
            for lbl in desired_labels:
                await client.add_label(token, repo, event.number, lbl)

            session.add(
                PRCheck(
                    repo_id=repo_row.id,
                    pr_number=event.number,
                    sha=ctx.pr.head.sha,
                    check_results={
                        r.name: {"passed": r.passed, "reason": r.reason} for r in results
                    },
                    summary=agent_result.assessment.summary if agent_result else None,
                    cost_usd=agent_result.cost_usd if agent_result else None,
                    agent_result=(
                        agent_result.model_dump(mode="json") if agent_result else None
                    ),
                    action_taken=level.name.lower(),
                    comment_id=comment_id,
                )
            )
            await session.commit()

        log.info("pr_event.done", level=level.name.lower(), comment_id=comment_id)

    except Exception:
        log.exception("pr_event.failed")


async def _get_or_create_repo(
    session: AsyncSession, installation_id: int, full_name: str
) -> Repo:
    repo = await session.scalar(
        select(Repo).where(Repo.installation_id == installation_id)
    )
    if not repo:
        repo = Repo(installation_id=installation_id, full_name=full_name)
        session.add(repo)
        await session.flush()
    return repo


async def _require_stats_token(
    authorization: str | None = Header(default=None),
) -> None:
    if not settings.stats_bearer_token:
        return
    if authorization != f"Bearer {settings.stats_bearer_token}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/stats")
async def stats(
    _: None = Depends(_require_stats_token),
    session: AsyncSession = Depends(get_session),
):
    total = await session.scalar(select(func.count(PRCheck.id))) or 0

    recent_rows = (
        await session.scalars(
            select(PRCheck).order_by(PRCheck.created_at.desc()).limit(10)
        )
    ).all()

    failed_by_check: dict[str, int] = {}
    for row in (await session.scalars(select(PRCheck))).all():
        for check_name, result in (row.check_results or {}).items():
            if not result.get("passed"):
                failed_by_check[check_name] = failed_by_check.get(check_name, 0) + 1

    return {
        "total_prs_checked": total,
        "failed_by_check": failed_by_check,
        "recent": [
            {
                "id": r.id,
                "pr": r.pr_number,
                "sha": r.sha[:7],
                "action": r.action_taken,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recent_rows
        ],
    }
