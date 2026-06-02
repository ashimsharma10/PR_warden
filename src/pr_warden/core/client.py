"""Thin Anthropic plumbing shared across pr_warden.

Deliberately small and model-agnostic: a per-API-key client cache plus a
prefix-matched cost estimator. The review agent uses these today; anything
else that needs to talk to Anthropic can reuse them.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

# Per-1M-token (input, output) USD pricing, matched by model-name prefix.
# Keep longest/most-specific prefixes first.
_PRICING: list[tuple[str, tuple[float, float]]] = [
    ("claude-3-5-haiku", (0.80, 4.00)),
    ("claude-haiku-4", (1.00, 5.00)),
    ("claude-3-haiku", (0.25, 1.25)),
    ("claude-sonnet-4", (3.00, 15.00)),
]
_DEFAULT_PRICING = (0.80, 4.00)  # fall back to 3.5-haiku pricing


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = _DEFAULT_PRICING
    for prefix, pricing in _PRICING:
        if model.startswith(prefix):
            in_price, out_price = pricing
            break
    return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price


# Cache one async client per API key; httpx connection pooling lives underneath.
_clients: dict[str, AsyncAnthropic] = {}


def get_client(api_key: str) -> AsyncAnthropic:
    client = _clients.get(api_key)
    if client is None:
        client = AsyncAnthropic(api_key=api_key)
        _clients[api_key] = client
    return client
