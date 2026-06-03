"""Slash commands a maintainer posts as a PR comment, e.g. `/warden mute`.

Parsing is pure and lives here so it can be unit-tested without a webhook. The
webhook handler in `main.py` owns authorization and the side effects; this module
only answers "is this comment a recognized command, and which one?".
"""

from __future__ import annotations

COMMAND_PREFIX = "/warden"
KNOWN_COMMANDS = frozenset({"mute", "unmute"})


def parse_command(body: str | None) -> str | None:
    """Return the subcommand if `body`'s first line is a recognized
    `/warden <cmd>`, else None.

    Only the first non-empty line is considered, so quoting the bot or
    mentioning the command mid-sentence never triggers it. Matching is
    case-insensitive on both the prefix and the subcommand.
    """
    if not body or not body.strip():
        return None
    first = body.strip().splitlines()[0].strip()
    parts = first.split()
    if len(parts) != 2 or parts[0].lower() != COMMAND_PREFIX:
        return None
    cmd = parts[1].lower()
    return cmd if cmd in KNOWN_COMMANDS else None
