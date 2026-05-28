import time

import structlog

from pr_warden.config import settings
from pr_warden.github.client import GH_API, _client

log = structlog.get_logger()

# Maps installation_id → (token, expires_at_timestamp)
_token_cache: dict[int, tuple[str, float]] = {}


def _generate_app_jwt() -> str:
    import jwt

    now = int(time.time())
    payload = {
        "iat": now - 60,  # absorb clock skew
        "exp": now + 600,
        "iss": str(settings.github_app_id),
    }
    return jwt.encode(payload, settings.private_key(), algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    token, expires_at = _token_cache.get(installation_id, ("", 0.0))
    if token and time.time() < expires_at - 60:
        return token

    app_jwt = _generate_app_jwt()
    r = await _client.post(
        f"{GH_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    r.raise_for_status()
    data = r.json()

    new_token: str = data["token"]
    _token_cache[installation_id] = (new_token, time.time() + 55 * 60)
    log.info("github.token_refreshed", installation_id=installation_id)
    return new_token
