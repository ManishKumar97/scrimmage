"""Incident diagnosis agent: calls tools in a loop until the model stops."""

import asyncio
import os
import sys
from dataclasses import dataclass

from faultline import kube_tools
from faultline.mcp_client import LOGS_SERVER, mcp_tools
from faultline.models import (
    BadToolCall,
    Message,
    ModelClient,
    ModelError,
    ToolCall,
    ToolResult,
    make_model,
)
from faultline.settings import get_settings
from faultline.tools import Registry

# Stop when the model keeps making tool calls that all fail.
MAX_CONSECUTIVE_TOOL_ERRORS = 3

SYSTEM_PROMPT = (
    "You diagnose incidents in the Astronomy Shop, a microservice app on Kubernetes. "
    "Tools: list_services, list_pods (pod status), get_logs (one service per call) "
    "and get_error_traces (failed spans in recent traces). "
    "Start with get_error_traces for the service named in the report. "
    "A service often fails because a service it calls fails, and the caller may log nothing. "
    "A span marked last failed span has no failed span below it: the failed call was made there. "
    "If it is a client span, the service it calls (the peer) may be the one that is down, "
    "so look at that service's traces, logs and pod status. "
    "Confirm with that service's own logs and pod status before you answer. "
    "get_logs shows only the last 100 lines, so a busy service can hide an error. "
    "Even then, name a cause only if the failure repeats across traces "
    "or a log line or pod status fact confirms it. One failed span alone is weak evidence. "
    "If the traces show no failed spans, check pod status and logs of the reported service. "
    "If a tool returns an error, read it and try again with a fix. "
    "Each log line starts with its timestamp, and each result says the time now. "
    "Prefer errors that are recent and repeating. "
    "A single line from long ago, such as one at startup, is weak evidence. "
    "Quiet logs are not proof of a crash, because a service logs little when it gets no traffic. "
    "Before you blame a crash, check pod status: a crash shows as restarts or a not-ready pod. "
    "Normal startup lines are not a fault. "
    "Only name a cause that explains the reported symptom. "
    "Name a cause only with a quoted log line, a failed span or a pod status fact that supports it. "
    "If the evidence shows no fault, answer 'No fault found' and say what you checked."
)


@dataclass(frozen=True)
class Budget:
    """Hard limits on one run. A run stops at whichever it hits first."""

    max_steps: int = 15  # model calls
    max_tokens: int = 40_000  # prompt plus completion tokens, summed over calls
    max_seconds: float = 120.0  # wall clock for the whole run


DEFAULT_BUDGET = Budget()


@dataclass
class RunResult:
    answer: str
    stop_reason: str
    steps: int = 0
    tokens: int = 0
    seconds: float = 0.0


async def run(
    question: str,
    model: ModelClient,
    budget: Budget = DEFAULT_BUDGET,
    tools: Registry = kube_tools.registry,
) -> RunResult:
    messages: list[Message] = [question]
    consecutive_errors = 0
    steps = 0
    tokens = 0
    clock = asyncio.get_running_loop()
    started = clock.time()

    def stop(answer: str, reason: str) -> RunResult:
        elapsed = round(clock.time() - started, 1)
        return RunResult(answer, reason, steps, tokens, elapsed)

    try:
        # One deadline for the whole run. When it passes, whatever is in flight is
        # cancelled: a model call, the SDK's retry waits, or a tool call.
        async with asyncio.timeout_at(started + budget.max_seconds):
            while True:
                if steps >= budget.max_steps:
                    return stop(
                        f"Step limit of {budget.max_steps} reached.", "step_limit"
                    )
                try:
                    reply = await model.complete(
                        SYSTEM_PROMPT, messages, tools.schemas()
                    )
                except BadToolCall as exc:
                    return stop(
                        f"Model produced an invalid tool call: {exc}", "bad_call"
                    )
                except ModelError as exc:
                    # Rate limits after the SDK's own retries, oversized requests, outages.
                    return stop(f"Model API failed: {exc}", "model_error")
                steps += 1
                tokens += reply.tokens

                # A reply cut off at the output limit is incomplete, with or without tool calls.
                # Its tool calls may be half written, so none of them run.
                if reply.finish_reason == "length":
                    return stop(reply.text, "truncated")
                if reply.finish_reason == "content_filter":
                    return stop(reply.text, "content_filtered")
                if reply.finish_reason == "refusal":
                    return stop(reply.text, "refused")
                if not reply.tool_calls:
                    return stop(reply.text, "done")
                if tokens >= budget.max_tokens:
                    return stop(
                        f"Token limit of {budget.max_tokens} reached.", "token_limit"
                    )

                messages.append(reply)
                # Tool calls from one reply run at the same time.
                results = await asyncio.gather(
                    *(call_tool(tools, call) for call in reply.tool_calls)
                )
                for call, (output, ok) in zip(reply.tool_calls, results, strict=True):
                    consecutive_errors = 0 if ok else consecutive_errors + 1
                    print(
                        f"[tool] {describe(call)}{'' if ok else ' -> error'}",
                        file=sys.stderr,
                    )
                    if os.environ.get("FAULTLINE_DEBUG_TOOLS"):
                        print(f"[tool output]\n{output}\n[end]", file=sys.stderr)
                messages.append(
                    [
                        ToolResult(call.id, output, not ok)
                        for call, (output, ok) in zip(
                            reply.tool_calls, results, strict=True
                        )
                    ]
                )
                if consecutive_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                    return stop(
                        f"Stopped after {consecutive_errors} tool errors in a row. "
                        f"Last error: {results[-1][0]}",
                        "tool_errors",
                    )
    except TimeoutError:
        return stop(f"Time limit of {budget.max_seconds}s reached.", "time_limit")


async def call_tool(tools: Registry, call: ToolCall) -> tuple[str, bool]:
    if not call.function:
        return "Error: only function tools are supported.", False
    return await tools.call(call.name, call.arguments)


def describe(call: ToolCall) -> str:
    if call.function:
        return f"{call.name}({call.arguments})"
    return f"{call.name} (custom tool call)"


async def diagnose_async(question: str) -> RunResult:
    model = make_model(get_settings())
    # get_logs comes from the MCP server. The server lives for this one run.
    async with mcp_tools(LOGS_SERVER, kube_tools.registry) as tools:
        return await run(question, model, tools=tools)


def diagnose(question: str) -> RunResult:
    return asyncio.run(diagnose_async(question))


if __name__ == "__main__":
    result = diagnose(" ".join(sys.argv[1:]) or "Customers report checkout fails. Why?")
    print(result.answer)
    print(
        f"[stop] {result.stop_reason} after {result.steps} steps, "
        f"{result.tokens} tokens, {result.seconds}s",
        file=sys.stderr,
    )
