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


async def get_repo_file(token: str, repo: str, path: str) -> str | None:
    """Fetch a file's raw contents at the repo's default branch.

    Returns None if the file does not exist (404). Other errors raise.
    """
    r = await _client.get(
        f"{GH_API}/repos/{repo}/contents/{path}",
        headers={**_headers(token), "Accept": "application/vnd.github.raw"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


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
    "prwarden:clean": "2ea44f",
    "prwarden:needs-attention": "d73a4a",
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


async def get_codeowners(token: str, repo: str) -> str | None:
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        content = await get_repo_file(token, repo, path)
        if content is not None:
            return content
    return None
