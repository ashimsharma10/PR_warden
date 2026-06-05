import logging
import os
import sys
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog

# Where rotating log files live. Overridable so a deploy can point at a mounted
# volume; defaults to a `.logs/` folder in the working directory.
LOG_DIR = Path(os.environ.get("PRWARDEN_LOG_DIR", ".logs"))
LOG_FILE = "pr_warden.log"
# Rotate at midnight, keep two weeks of history, then auto-delete the oldest.
LOG_RETENTION_DAYS = int(os.environ.get("PRWARDEN_LOG_RETENTION_DAYS", "14"))
# INFO by default; set PRWARDEN_LOG_LEVEL=DEBUG to see per-request I/O (every
# GitHub call, success included) when investigating a failure.
LOG_LEVEL = getattr(logging, os.environ.get("PRWARDEN_LOG_LEVEL", "INFO").upper(), logging.INFO)


def _install_stdlib_handlers() -> None:
    """Route stdlib logging to stdout *and* a rotating file.

    structlog renders each event to a JSON string (its final processor), then
    hands it to stdlib logging; these two handlers each emit that string — one to
    the console (as before), one to `.logs/pr_warden.log`, which rotates daily and
    keeps LOG_RETENTION_DAYS of history so a run survives the terminal closing.

    Idempotent: uvicorn --reload re-imports this module, and we must not stack a
    new pair of handlers (and duplicate every line) on each reload.
    """
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    if any(getattr(h, "_prwarden", False) for h in root.handlers):
        return  # already configured

    fmt = logging.Formatter("%(message)s")  # structlog already rendered the JSON

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream._prwarden = True  # type: ignore[attr-defined]
    root.addHandler(stream)

    # Clamp chatty third-party loggers so flipping to DEBUG surfaces *our* events,
    # not httpcore's per-packet wire trace. They still emit real warnings/errors.
    for noisy in ("httpcore", "httpx", "anthropic", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file = TimedRotatingFileHandler(
        LOG_DIR / LOG_FILE,
        when="midnight",
        backupCount=LOG_RETENTION_DAYS,
        encoding="utf-8",
        utc=True,
    )
    file.setFormatter(fmt)
    file._prwarden = True  # type: ignore[attr-defined]
    root.addHandler(file)


def configure_logging() -> None:
    _install_stdlib_handlers()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(LOG_LEVEL),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


log = structlog.get_logger()
