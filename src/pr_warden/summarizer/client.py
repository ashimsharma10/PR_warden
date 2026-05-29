"""Thin Anthropic wrapper shared by every summarizer agent.

This is deliberately small and model-agnostic. When the summarizer is split
into multiple specialised agents (intent, completeness, blast-radius, ...),
each one reuses `call_model` and only varies its system prompt / schema.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from anthropic import AsyncAnthropic

log = structlog.get_logger()

# Per-1M-token (input, output) USD pricing, matched by model-name prefix.
# Keep longest/most-specific prefixes first.
_PRICING: list[tuple[str, tuple[float, float]]] = [
    ("claude-3-5-haiku", (0.80, 4.00)),
    ("claude-haiku-4", (1.00, 5.00)),
    ("claude-3-haiku", (0.25, 1.25)),
]
_DEFAULT_PRICING = (0.80, 4.00)  # fall back to 3.5-haiku pricing


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = _DEFAULT_PRICING
    for prefix, pricing in _PRICING:
        if model.startswith(prefix):
            in_price, out_price = pricing
            break
    return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


# Cache one async client per API key; httpx connection pooling lives underneath.
_clients: dict[str, AsyncAnthropic] = {}


def _get_client(api_key: str) -> AsyncAnthropic:
    client = _clients.get(api_key)
    if client is None:
        client = AsyncAnthropic(api_key=api_key)
        _clients[api_key] = client
    return client


async def call_model(
    *,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1024,
) -> LLMResult:
    """Single-turn completion. Raises on transport/API errors — callers decide
    how to degrade (the summarizer treats any failure as 'no summary')."""
    client = _get_client(api_key)
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
    cost = estimate_cost(model, resp.usage.input_tokens, resp.usage.output_tokens)
    log.info(
        "summarizer.model_call",
        model=model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        cost_usd=round(cost, 6),
    )
    return LLMResult(
        text=text,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        cost_usd=cost,
        model=model,
    )
