import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import pr_warden.log as pr_warden_log
from pr_warden.checks import CheckContext, run_checks
from pr_warden.checks.impact import run_gitleaks
from pr_warden.composer import LABEL_CLEAN, LABEL_NEEDS_ATTENTION, build_comment, pick_label
from pr_warden.config import settings
from pr_warden.db import async_session_factory, engine, get_session
from pr_warden.github import auth, client
from pr_warden.github.schemas import PullRequestEvent
from pr_warden.github.webhooks import verify_signature
from pr_warden.models import PRCheck, Repo
from pr_warden.repo_config import CONFIG_PATH, DEFAULT_CONFIG, RepoConfig, parse_config
from pr_warden.summarizer import format_summary, summarize_pr

log = structlog.get_logger()

_REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened"}


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


async def _today_cost(session: AsyncSession) -> float:
    """Total summarizer spend (USD) since 00:00 UTC today."""
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.coalesce(func.sum(PRCheck.cost_usd), 0.0)).where(
            PRCheck.created_at >= since
        )
    )
    return float(total or 0.0)


async def _maybe_summarize(session: AsyncSession, ctx: CheckContext):
    """Run the LLM summarizer if configured and under the daily cost ceiling.

    Returns a PRSummary or None. Never raises — a missing key, budget cap, or
    model failure just means the comment ships with the deterministic table only.
    """
    if not settings.anthropic_api_key:
        return None

    spent = await _today_cost(session)
    if spent >= settings.daily_cost_limit_usd:
        log.warning("summarizer.budget_exhausted", spent_usd=round(spent, 4))
        return None

    return await summarize_pr(
        ctx,
        api_key=settings.anthropic_api_key,
        model=settings.summarizer_model,
    )


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
        label = pick_label(results)

        async with async_session_factory() as session:
            repo_row = await _get_or_create_repo(session, event.installation.id, repo)
            repo_row.cached_config = config.model_dump()

            summary = await _maybe_summarize(session, ctx)
            summary_md = format_summary(summary) if summary else None
            comment_body = build_comment(results, summary=summary_md)

            existing_comment_id = await session.scalar(
                select(PRCheck.comment_id)
                .where(PRCheck.repo_id == repo_row.id)
                .where(PRCheck.pr_number == event.number)
                .where(PRCheck.comment_id.isnot(None))
                .order_by(PRCheck.created_at.desc())
            )

            if existing_comment_id:
                await client.update_comment(token, repo, existing_comment_id, comment_body)
                comment_id = existing_comment_id
            else:
                comment_id = await client.create_comment(token, repo, event.number, comment_body)

            for lbl in [LABEL_CLEAN, LABEL_NEEDS_ATTENTION]:
                await client.remove_label(token, repo, event.number, lbl)
            await client.add_label(token, repo, event.number, label)

            session.add(
                PRCheck(
                    repo_id=repo_row.id,
                    pr_number=event.number,
                    sha=ctx.pr.head.sha,
                    check_results={
                        r.name: {"passed": r.passed, "reason": r.reason} for r in results
                    },
                    summary=summary.summary if summary else None,
                    cost_usd=summary.cost_usd if summary else None,
                    action_taken=label,
                    comment_id=comment_id,
                )
            )
            await session.commit()

        log.info("pr_event.done", label=label, comment_id=comment_id)

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
