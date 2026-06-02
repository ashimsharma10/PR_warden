"""The agent loop — the code we own.

The LLM only decides "what next." This loop owns iteration, the budget, tool
execution, and termination. Design choices worth naming:

- Parallel tool execution. When the model calls three tools in one turn we run
  them concurrently; the single biggest latency win.
- The `done` tool. Termination is the model calling `done` with structured args,
  not us parsing prose to guess it has finished.
- Force-finalize. On budget exhaustion we ask once more for `done` with whatever
  it has, rather than returning nothing.
- Errors are tool results. Malformed input comes back as an error tool_result so
  the model can retry; we don't crash.
- Trace everything. Every call and result is logged for debugging and evals.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import structlog
from pydantic import ValidationError

from pr_warden.agent.context import PRContext
from pr_warden.agent.schemas import AgentResult, DoneInput, ToolResult
from pr_warden.agent.tools import DONE_TOOL, build_tools, tool_to_anthropic_schema
from pr_warden.core.client import estimate_cost, get_client

log = structlog.get_logger()

MAX_TOOL_CALLS = 12
MAX_ITERATIONS = 15
MAX_TOTAL_TOKENS = 80_000
MAX_OUTPUT_TOKENS = 2048
TOOL_TIMEOUT_S = 15.0
MODEL = "claude-sonnet-4-6"

# A `send` takes the request pieces and returns an object exposing `.content`
# (a list of content blocks) and `.usage.input_tokens` / `.usage.output_tokens`.
Send = Callable[..., Awaitable[Any]]


def _is_tool_use(block: Any) -> bool:
    return getattr(block, "type", None) == "tool_use"


def _fallback_assessment(reason: str) -> DoneInput:
    return DoneInput(
        summary="The agent stopped before completing its assessment.",
        files_touched=[],
        intent_matches_diff=True,
        intent_mismatch_reason="",
        attention=[],
        open_questions=[f"Assessment incomplete: {reason}."],
        confidence=0.0,
    )


def _default_send(api_key: str) -> Send:
    client = get_client(api_key)

    async def send(*, model: str, system: str, tools: list[dict], messages: list[dict]):
        return await client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
        )

    return send


async def _execute_tool(tool_map: dict, ctx: PRContext, block: Any) -> ToolResult:
    tool = tool_map.get(block.name)
    if tool is None:
        return ToolResult(ok=False, content=f"Unknown tool: {block.name}", error="unknown_tool")
    try:
        validated = tool.input_schema.model_validate(block.input)
    except ValidationError as e:
        return ToolResult(ok=False, content=f"Input validation failed: {e}", error="validation_error")
    timeout = getattr(tool, "timeout_s", TOOL_TIMEOUT_S)
    try:
        return await asyncio.wait_for(tool.run(ctx, validated), timeout=timeout)
    except asyncio.TimeoutError:
        return ToolResult(ok=False, content=f"Tool '{tool.name}' timed out.", error="timeout")
    except Exception as e:  # noqa: BLE001 — a tool must never crash the loop
        log.exception("agent.tool_exception", tool=tool.name)
        return ToolResult(
            ok=False,
            content=f"Tool '{tool.name}' raised {type(e).__name__}: {e}",
            error="exception",
        )


async def run_agent(
    ctx: PRContext,
    *,
    api_key: str,
    model: str = MODEL,
    send: Send | None = None,
    tools: list | None = None,
) -> AgentResult:
    """Run the review agent and return its structured assessment.

    `send` and `tools` are injectable for testing; in production they default to
    a real Anthropic client and the standard toolset.
    """
    from pr_warden.agent.prompts import SYSTEM_PROMPT, render_initial_user_message

    started = time.monotonic()
    tools = tools or build_tools()
    tool_schemas = [tool_to_anthropic_schema(t) for t in tools]
    tool_map = {t.name: t for t in tools}
    if send is None:
        send = _default_send(api_key)

    messages: list[dict] = [
        {"role": "user", "content": render_initial_user_message(ctx)}
    ]
    trace: list[dict] = []
    tool_calls = 0
    in_tokens = 0
    out_tokens = 0

    def result(assessment: DoneInput, stopped_for: str) -> AgentResult:
        return AgentResult(
            assessment=assessment,
            trace=trace,
            cost_usd=estimate_cost(model, in_tokens, out_tokens),
            stopped_for=stopped_for,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            tool_call_count=tool_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def force_finalize(reason: str) -> AgentResult:
        nonlocal in_tokens, out_tokens
        messages.append({
            "role": "user",
            "content": "You've reached your budget. Call `done` now with whatever "
                       "you have — put anything unverified in open_questions.",
        })
        try:
            resp = await send(model=model, system=SYSTEM_PROMPT, tools=tool_schemas, messages=messages)
            in_tokens += resp.usage.input_tokens
            out_tokens += resp.usage.output_tokens
            done = next((b for b in resp.content if _is_tool_use(b) and b.name == DONE_TOOL), None)
            if done is not None:
                return result(DoneInput.model_validate(done.input), reason)
        except Exception:  # noqa: BLE001 — finalize must always return something
            log.exception("agent.force_finalize_failed", reason=reason)
        return result(_fallback_assessment(reason), reason)

    for iteration in range(MAX_ITERATIONS):
        if tool_calls >= MAX_TOOL_CALLS:
            log.warning("agent.tool_budget_exceeded", tool_calls=tool_calls)
            return await force_finalize("tool_call_budget")
        if in_tokens + out_tokens > MAX_TOTAL_TOKENS:
            log.warning("agent.token_budget_exceeded", tokens=in_tokens + out_tokens)
            return await force_finalize("token_budget")

        resp = await send(model=model, system=SYSTEM_PROMPT, tools=tool_schemas, messages=messages)
        in_tokens += resp.usage.input_tokens
        out_tokens += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if _is_tool_use(b)]
        if not tool_uses:
            log.warning("agent.stopped_without_done", iteration=iteration)
            return await force_finalize("no_tool_call")

        done = next((b for b in tool_uses if b.name == DONE_TOOL), None)
        if done is not None:
            try:
                assessment = DoneInput.model_validate(done.input)
            except ValidationError as e:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": done.id,
                        "content": f"Validation error in `done` args: {e}. Fix and retry.",
                        "is_error": True,
                    }],
                })
                continue
            log.info(
                "agent.done",
                iterations=iteration + 1,
                tool_calls=tool_calls,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )
            return result(assessment, "done")

        results = await asyncio.gather(
            *(_execute_tool(tool_map, ctx, b) for b in tool_uses)
        )
        tool_calls += len(tool_uses)

        result_blocks = []
        for block, tr in zip(tool_uses, results):
            trace.append({
                "tool": block.name,
                "input": block.input,
                "ok": tr.ok,
                "content_preview": tr.content[:200],
            })
            result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": tr.content,
                "is_error": not tr.ok,
            })
        messages.append({"role": "user", "content": result_blocks})

    log.warning("agent.max_iterations", iterations=MAX_ITERATIONS)
    return await force_finalize("max_iterations")
