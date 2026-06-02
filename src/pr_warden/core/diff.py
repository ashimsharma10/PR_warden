"""Build a compact diff text from the PR file list for prompting.

No sandbox, no checkout — we only ever read the patch GitHub already gives us.
The character cap keeps token cost bounded on huge PRs.
"""

from __future__ import annotations

# ~30k chars of diff is roughly 8k tokens — plenty of context, keeps cost bounded.
MAX_DIFF_CHARS = 30_000


def build_diff_text(files: list[dict], max_chars: int = MAX_DIFF_CHARS) -> str:
    parts: list[str] = []
    for f in files:
        patch = f.get("patch")
        if not patch:
            # Binary files / files too large for GitHub to inline have no patch.
            parts.append(f"--- {f['filename']} (no inline diff) ---")
            continue
        parts.append(f"--- {f['filename']} ---\n{patch}")

    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[diff truncated for length]"
    return text
