import hmac

import pytest

from pr_warden.github.webhooks import verify_signature


def _sign(payload: bytes, secret: str) -> str:
    digest = hmac.digest(secret.encode(), payload, "sha256").hex()
    return f"sha256={digest}"


@pytest.fixture(autouse=True)
def patch_secret(monkeypatch):
    from pr_warden.config import settings
    monkeypatch.setattr(settings, "github_webhook_secret", "test-secret")


def test_valid_signature():
    payload = b'{"action": "opened"}'
    header = _sign(payload, "test-secret")
    assert verify_signature(payload, header) is True


def test_wrong_secret():
    payload = b'{"action": "opened"}'
    header = _sign(payload, "wrong-secret")
    assert verify_signature(payload, header) is False


def test_tampered_payload():
    original = b'{"action": "opened"}'
    header = _sign(original, "test-secret")
    tampered = b'{"action": "closed"}'
    assert verify_signature(tampered, header) is False


def test_missing_header():
    assert verify_signature(b"payload", None) is False


def test_malformed_header():
    assert verify_signature(b"payload", "md5=deadbeef") is False


def test_empty_payload_valid():
    payload = b""
    header = _sign(payload, "test-secret")
    assert verify_signature(payload, header) is True
