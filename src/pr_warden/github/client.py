import httpx
import structlog

log = structlog.get_logger()

GH_API = "https://api.github.com"

_client = httpx.AsyncClient(timeout=30.0)


async def close() -> None:
    await _client.aclose()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def graphql(token: str, query: str, variables: dict) -> dict:
    """POST a GraphQL query to GitHub's v4 API and return the `data` payload.

    Raises on transport errors or any GraphQL `errors` in the response — the
    REST helpers raise on failure too, so callers handle both the same way.
    """
    r = await _client.post(
        f"{GH_API}/graphql",
        headers=_headers(token),
        json={"query": query, "variables": variables},
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    data: dict = payload.get("data") or {}
    return data


_BLAME_QUERY = """
query($owner: String!, $name: String!, $ref: String!, $path: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $ref) {
      ... on Commit {
        blame(path: $path) {
          ranges {
            startingLine
            endingLine
            commit {
              oid
              messageHeadline
              committedDate
              author { name user { login } }
              associatedPullRequests(first: 1) { nodes { number title } }
            }
          }
        }
      }
    }
  }
}
"""


async def get_blame(
    token: str, repo: str, path: str, ref: str
) -> list[dict] | None:
    """Return blame ranges for `path` at `ref` (blame is GraphQL-only).

    Each range is ``{startingLine, endingLine, commit: {...}}``. Returns None if
    the path or commit can't be resolved (object/blame is null).
    """
    owner, name = repo.split("/", 1)
    data = await graphql(
        token,
        _BLAME_QUERY,
        {"owner": owner, "name": name, "ref": ref, "path": path},
    )
    obj = ((data.get("repository") or {}).get("object")) or {}
    blame = obj.get("blame")
    if not blame:
        return None
    ranges: list[dict] = blame.get("ranges", [])
    return ranges


async def get_repo_file(
    token: str, repo: str, path: str, ref: str | None = None
) -> str | None:
    """Fetch a file's raw contents.

    With `ref` (a branch, tag, or commit SHA) the file is read at that point;
    without it, GitHub serves the repo's default branch. Returns None if the
    file does not exist (404). Other errors raise.
    """
    r = await _client.get(
        f"{GH_API}/repos/{repo}/contents/{path}",
        headers={**_headers(token), "Accept": "application/vnd.github.raw"},
        params={"ref": ref} if ref else None,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


async def get_issue(token: str, repo: str, number: int) -> dict | None:
    """Fetch an issue (or PR, which GitHub also exposes here) by number.

    Returns None if it does not exist (404). Other errors raise.
    """
    r = await _client.get(
        f"{GH_API}/repos/{repo}/issues/{number}",
        headers=_headers(token),
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def search_author_prs(
    token: str, repo: str, author: str, limit: int = 10
) -> list[dict]:
    """Return the author's most recent PRs on this repo (newest first).

    Uses the search API; each item carries `state` and, for merged PRs, a
    `pull_request.merged_at` timestamp that distinguishes merged from closed.
    """
    r = await _client.get(
        f"{GH_API}/search/issues",
        headers=_headers(token),
        params={
            "q": f"repo:{repo} type:pr author:{author}",
            "sort": "created",
            "order": "desc",
            "per_page": limit,
        },
    )
    r.raise_for_status()
    items: list[dict] = r.json().get("items", [])
    return items


async def list_issue_comments(
    token: str, repo: str, number: int, limit: int = 10
) -> list[dict]:
    r = await _client.get(
        f"{GH_API}/repos/{repo}/issues/{number}/comments",
        headers=_headers(token),
        params={"per_page": limit},
    )
    r.raise_for_status()
    comments: list[dict] = r.json()
    return comments


async def list_pr_commits(token: str, repo: str, pr_number: int) -> list[dict]:
    commits: list[dict] = []
    page = 1
    while True:
        r = await _client.get(
            f"{GH_API}/repos/{repo}/pulls/{pr_number}/commits",
            headers=_headers(token),
            params={"per_page": 100, "page": page},
        )
        r.raise_for_status()
        batch: list[dict] = r.json()
        commits.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return commits



async def list_pr_files(token: str, repo: str, pr_number: int) -> list[dict]:
    files: list[dict] = []
    page = 1
    while True:
        r = await _client.get(
            f"{GH_API}/repos/{repo}/pulls/{pr_number}/files",
            headers=_headers(token),
            params={"per_page": 100, "page": page},
        )
        r.raise_for_status()
        batch: list[dict] = r.json()
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


async def create_comment(token: str, repo: str, pr_number: int, body: str) -> int:
    r = await _client.post(
        f"{GH_API}/repos/{repo}/issues/{pr_number}/comments",
        headers=_headers(token),
        json={"body": body},
    )
    r.raise_for_status()
    comment_id: int = r.json()["id"]
    log.info("github.comment_created", repo=repo, pr=pr_number, comment_id=comment_id)
    return comment_id


async def update_comment(token: str, repo: str, comment_id: int, body: str) -> None:
    r = await _client.patch(
        f"{GH_API}/repos/{repo}/issues/comments/{comment_id}",
        headers=_headers(token),
        json={"body": body},
    )
    r.raise_for_status()
    log.info("github.comment_updated", repo=repo, comment_id=comment_id)


_LABEL_COLORS = {
    # Facet labels (the only ones applied) — distinct hues so a flagged PR isn't
    # a wall of red. The retired status labels are only ever removed, not added.
    "prwarden:blocker": "b60205",          # dark red
    "prwarden:security": "5319e7",         # purple
    "prwarden:ai-authored": "1d76db",      # blue
    "prwarden:intent-mismatch": "d93f0b",  # orange
}


async def ensure_label_exists(token: str, repo: str, label: str) -> None:
    color = _LABEL_COLORS.get(label, "ededed")
    r = await _client.post(
        f"{GH_API}/repos/{repo}/labels",
        headers=_headers(token),
        json={"name": label, "color": color, "description": "Managed by PRwarden"},
    )
    if r.status_code not in (201, 422):
        r.raise_for_status()


async def add_label(token: str, repo: str, pr_number: int, label: str) -> None:
    await ensure_label_exists(token, repo, label)
    r = await _client.post(
        f"{GH_API}/repos/{repo}/issues/{pr_number}/labels",
        headers=_headers(token),
        json={"labels": [label]},
    )
    r.raise_for_status()


async def remove_label(token: str, repo: str, pr_number: int, label: str) -> None:
    r = await _client.delete(
        f"{GH_API}/repos/{repo}/issues/{pr_number}/labels/{label}",
        headers=_headers(token),
    )
    if r.status_code != 404:
        r.raise_for_status()


async def list_repo_tree(token: str, repo: str, branch: str = "HEAD") -> list[str]:
    r = await _client.get(
        f"{GH_API}/repos/{repo}/git/trees/{branch}",
        headers=_headers(token),
        params={"recursive": "1"},
    )
    if r.status_code == 409:
        return []
    r.raise_for_status()
    return [item["path"] for item in r.json().get("tree", []) if item["type"] == "blob"]


async def get_commit_status(
    token: str, repo: str, sha: str
) -> tuple[str, list[str]]:
    """Return the combined CI status for a commit.

    Uses GitHub's combined status endpoint (which merges both the legacy
    Statuses API and the newer Check Runs).  Returns a tuple of
    ``(state, failed_contexts)`` where *state* is one of ``"success"``,
    ``"pending"``, ``"failure"``, or ``"error"``; *failed_contexts* lists
    the names of any check runs / statuses that are not passing.

    If the commit has no status checks at all, returns ``("success", [])``
    so the pipeline treats it the same as a green build (many repos have no
    CI configured).
    """
    r = await _client.get(
        f"{GH_API}/repos/{repo}/commits/{sha}/status",
        headers=_headers(token),
    )
    r.raise_for_status()
    data = r.json()
    state: str = data.get("state", "success")
    # total_count == 0 means no checks exist — treat as passing.
    if data.get("total_count", 0) == 0:
        return ("success", [])
    failed = [
        s["context"]
        for s in data.get("statuses", [])
        if s.get("state") not in ("success", "pending")
    ]
    return (state, failed)


async def get_codeowners(token: str, repo: str) -> str | None:
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        content = await get_repo_file(token, repo, path)
        if content is not None:
            return content
    return None
