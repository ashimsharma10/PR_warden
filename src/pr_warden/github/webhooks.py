import hashlib
import hmac

from pr_warden.config import settings


def verify_signature(payload: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.digest(
        settings.github_webhook_secret.encode(),
        payload,
        "sha256",
    ).hex()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)
