"""Incident diagnosis agent: calls tools in a loop until the model stops."""

import asyncio
import sys
from dataclasses import dataclass
from typing import cast

import openai
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallUnion,
    ChatCompletionMessageToolCallUnionParam,
)

from faultline import kube_tools
from faultline.settings import get_settings
from faultline.tools import Registry

# Stop when the model keeps making tool calls that all fail.
MAX_CONSECUTIVE_TOOL_ERRORS = 3

SYSTEM_PROMPT = (
    "You diagnose incidents in the Astronomy Shop, a microservice app on Kubernetes. "
    "Use list_services to see which services exist, and get_logs to read their logs. "
    "Call tools as many times as you need, one service per get_logs call. "
    "If a tool returns an error, read it and try again with a fix. "
    "Each log line starts with its timestamp, and each result says the time now. "
    "Prefer errors that are recent and repeating. "
    "A single line from long ago, such as one at startup, is weak evidence. "
    "Quote the exact log line that supports your diagnosis."
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
    client: AsyncOpenAI,
    model: str,
    budget: Budget = DEFAULT_BUDGET,
    tools: Registry = kube_tools.registry,
) -> RunResult:
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
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
                    response = await client.chat.completions.create(
                        model=model, messages=messages, tools=tools.schemas()
                    )
                except openai.BadRequestError as exc:
                    # Groq rejects a reply whose tool call it cannot parse.
                    return stop(
                        f"Model produced an invalid tool call: {exc}", "bad_call"
                    )
                except openai.APIError as exc:
                    # Rate limits after the SDK's own retries, oversized requests, outages.
                    return stop(f"Model API failed: {exc}", "model_error")
                steps += 1
                tokens += response.usage.total_tokens if response.usage else 0

                reply = response.choices[0].message
                if not reply.tool_calls:
                    return stop(reply.content or "", "done")
                if tokens >= budget.max_tokens:
                    return stop(
                        f"Token limit of {budget.max_tokens} reached.", "token_limit"
                    )

                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.content,
                        # The response and request types describe the same JSON.
                        "tool_calls": [
                            cast(
                                ChatCompletionMessageToolCallUnionParam,
                                call.model_dump(),
                            )
                            for call in reply.tool_calls
                        ],
                    }
                )
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
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": output}
                    )
                if consecutive_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                    return stop(
                        f"Stopped after {consecutive_errors} tool errors in a row. "
                        f"Last error: {results[-1][0]}",
                        "tool_errors",
                    )
    except TimeoutError:
        return stop(f"Time limit of {budget.max_seconds}s reached.", "time_limit")


async def call_tool(
    tools: Registry, call: ChatCompletionMessageToolCallUnion
) -> tuple[str, bool]:
    if not isinstance(call, ChatCompletionMessageFunctionToolCall):
        return "Error: only function tools are supported.", False
    return await tools.call(call.function.name, call.function.arguments)


def describe(call: ChatCompletionMessageToolCallUnion) -> str:
    if isinstance(call, ChatCompletionMessageFunctionToolCall):
        return f"{call.function.name}({call.function.arguments})"
    return f"{call.custom.name} (custom tool call)"


def diagnose(question: str) -> RunResult:
    settings = get_settings()
    # The SDK retries rate limits and outages with backoff. The run's deadline
    # cancels those retries too, so they cannot push a run past its time budget.
    client = AsyncOpenAI(
        api_key=settings.groq_api_key.get_secret_value(),
        base_url=settings.groq_base_url,
        max_retries=5,
    )
    return asyncio.run(run(question, client, settings.model))


if __name__ == "__main__":
    result = diagnose(" ".join(sys.argv[1:]) or "Customers report checkout fails. Why?")
    print(result.answer)
    print(
        f"[stop] {result.stop_reason} after {result.steps} steps, "
        f"{result.tokens} tokens, {result.seconds}s",
        file=sys.stderr,
    )
