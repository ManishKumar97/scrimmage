"""Incident diagnosis agent: calls tools in a loop until the model stops."""

import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from opentelemetry.context import Context
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Span, SpanKind

from faultline_lab import kube_tools
from faultline_lab import tracing as spans
from faultline_lab.mcp_client import LOGS_SERVER, METRICS_SERVER, launched, mcp_tools
from faultline_lab.models import (
    BadToolCall,
    Message,
    ModelClient,
    ModelError,
    Reply,
    ToolCall,
    ToolResult,
    make_model,
)
from faultline_lab.settings import get_settings
from faultline_lab.tools import Registry
from faultline_lab.tracing import Tracing

# Stop when the model keeps making tool calls that all fail.
MAX_CONSECUTIVE_TOOL_ERRORS = 3

SYSTEM_PROMPT = (
    "You diagnose incidents in the Astronomy Shop, a microservice app on Kubernetes. "
    "Tools: list_services, list_pods (pod status), get_logs (one service per call), "
    "get_error_traces (failed spans in recent traces), get_service_metrics "
    "(request rate, error rate and latency now against the 30 minutes before) "
    "and get_error_rate_trend (error rate over time). "
    "Start with get_error_traces for the service named in the report. "
    "A service often fails because a service it calls fails, and the caller may log nothing. "
    "A span marked last failed span has no failed span below it: the failed call was made there. "
    "If it is a client span, the service it calls (the peer) may be the one that is down, "
    "so look at that service's traces, logs and pod status. "
    "Use metrics to size the symptom: whether a service's error rate is above its baseline "
    "or always like this, how large the rise is, and when it started. "
    "Check the error rate of the reported service and of the services it calls. "
    "A rise over baseline in error rate, p95 latency or calls per second that fits the symptom "
    "is evidence, even when the error rate is flat. "
    "A high value equal to its baseline can mean the problem is older than the baseline: "
    "check the trend. A low value equal to its baseline is how the service always is. "
    "Metrics say 'no data' when Prometheus has no series. That is not proof of no errors. "
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
    "If the evidence shows no fault, answer 'No fault found' and say what you checked. "
    "End your final answer with one last line, ROOT CAUSE SERVICE: <name>, "
    "where <name> is the service, as list_services spells it, whose own fault explains the symptom. "
    "A service that only passes on a failure from one it calls is not the cause. "
    "If you found no fault, end with ROOT CAUSE SERVICE: none."
)


@dataclass(frozen=True)
class Budget:
    """Hard limits on one run. A run stops at whichever it hits first."""

    max_steps: int = 15  # model calls
    # prompt plus completion tokens, summed over calls. With the metrics tools a
    # run that checks several services reaches 45k.
    max_tokens: int = 60_000
    max_seconds: float = 120.0  # wall clock for the whole run


DEFAULT_BUDGET = Budget()


@dataclass
class RunResult:
    answer: str
    stop_reason: str
    steps: int = 0
    tokens: int = 0
    seconds: float = 0.0
    cache_read_tokens: int = 0  # input tokens served from the provider's prompt cache


async def run(
    question: str,
    model: ModelClient,
    budget: Budget = DEFAULT_BUDGET,
    *,
    tools: Registry,
    tracing: Tracing | None = None,
) -> RunResult:
    """Run the loop inside one root span. The span ends on every exit path."""
    tracing = tracing or Tracing()
    attributes = {
        gen_ai.GEN_AI_OPERATION_NAME: spans.INVOKE_AGENT,
        gen_ai.GEN_AI_AGENT_NAME: spans.AGENT_NAME,
        gen_ai.GEN_AI_PROVIDER_NAME: model.provider,
        gen_ai.GEN_AI_REQUEST_MODEL: model.model,
        "faultline.budget.max_steps": budget.max_steps,
        "faultline.budget.max_tokens": budget.max_tokens,
        "faultline.budget.max_seconds": budget.max_seconds,
    }
    attributes["faultline.question"] = question
    with tracing.span(f"invoke_agent {spans.AGENT_NAME}", attributes) as root:
        result = await run_loop(question, model, budget, tools, tracing, root)
        root.set_attribute("faultline.stop_reason", result.stop_reason)
        root.set_attribute("faultline.steps", result.steps)
        root.set_attribute("faultline.tokens", result.tokens)
        root.set_attribute("faultline.seconds", result.seconds)
        root.set_attribute("faultline.answer", result.answer)
        if result.stop_reason != "done":
            tracing.fail(root, result.stop_reason, result.answer)
        return result


async def run_loop(
    question: str,
    model: ModelClient,
    budget: Budget,
    tools: Registry,
    tracing: Tracing,
    root: Span,
) -> RunResult:
    parent = spans.context_of(root)
    messages: list[Message] = [question]
    consecutive_errors = 0
    steps = 0
    tokens = 0
    cache_read = 0
    clock = asyncio.get_running_loop()
    started = clock.time()

    def stop(answer: str, reason: str) -> RunResult:
        elapsed = round(clock.time() - started, 1)
        return RunResult(answer, reason, steps, tokens, elapsed, cache_read)

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
                    reply = await complete(
                        model, messages, tools, tracing, parent, steps + 1
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
                cache_read += reply.cache_read_tokens or 0

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
                outcomes = await asyncio.gather(
                    *(
                        call_tool(tools, call, tracing, parent, steps)
                        for call in reply.tool_calls
                    )
                )
                results = []
                for call, (output, ok) in zip(reply.tool_calls, outcomes, strict=True):
                    consecutive_errors = 0 if ok else consecutive_errors + 1
                    print(
                        f"[tool] {describe(call)}{'' if ok else ' -> error'}",
                        file=sys.stderr,
                    )
                    results.append(ToolResult(call.id, output, not ok))
                messages.append(results)
                if consecutive_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                    return stop(
                        f"Stopped after {consecutive_errors} tool errors in a row. "
                        f"Last error: {results[-1].content}",
                        "tool_errors",
                    )
    except TimeoutError:
        return stop(f"Time limit of {budget.max_seconds}s reached.", "time_limit")


async def complete(
    model: ModelClient,
    messages: list[Message],
    tools: Registry,
    tracing: Tracing,
    parent: Context,
    step: int,
) -> Reply:
    """One model call, in its own span."""
    attributes = spans.model_attributes(model.provider, model.model, step)
    specs = tools.specs()
    attributes[gen_ai.GEN_AI_SYSTEM_INSTRUCTIONS] = SYSTEM_PROMPT
    attributes[gen_ai.GEN_AI_INPUT_MESSAGES] = spans.messages_json(messages)
    attributes[gen_ai.GEN_AI_TOOL_DEFINITIONS] = json.dumps(
        [{"type": "function", **asdict(spec)} for spec in specs]
    )
    with tracing.span(
        f"chat {model.model}", attributes, parent, SpanKind.CLIENT
    ) as span:
        reply = await model.complete(SYSTEM_PROMPT, messages, specs)
        spans.record_reply(span, reply)
        span.set_attribute(gen_ai.GEN_AI_OUTPUT_MESSAGES, spans.output_json(reply))
        return reply


async def call_tool(
    tools: Registry, call: ToolCall, tracing: Tracing, parent: Context, step: int
) -> tuple[str, bool]:
    """One tool call, in its own span.

    The span is a child of the run span, not of the model call: the model call has
    ended by now, and calls from one reply run at the same time, so they are siblings.
    The step number ties each tool span to the model call that asked for it.
    """
    attributes = spans.tool_attributes(call.name, call.id, step)
    attributes[gen_ai.GEN_AI_TOOL_CALL_ARGUMENTS] = call.arguments
    with tracing.span(f"execute_tool {call.name}", attributes, parent) as span:
        output, ok = await tools.call(call.name, call.arguments)
        span.set_attribute(gen_ai.GEN_AI_TOOL_CALL_RESULT, output)
        if not ok:
            tracing.fail(span, "tool_error", output)
        return output, ok


def describe(call: ToolCall) -> str:
    return f"{call.name}({call.arguments})"


async def diagnose_async(question: str, *, trace_file: Path | None = None) -> RunResult:
    """Run one diagnosis. A trace file, when given, overrides the environment's."""
    settings = get_settings()
    model = make_model(settings)
    provider = spans.file_provider(trace_file or settings.trace_file)
    tracing = Tracing(provider.get_tracer("faultline"))
    prefix, pass_env = settings.mcp_command_prefix, settings.mcp_pass_env
    logs = launched(LOGS_SERVER, prefix, pass_env)
    metrics = launched(METRICS_SERVER, prefix, pass_env)
    try:
        # The logs and metrics tools come from MCP servers. They live for this one run.
        async with (
            mcp_tools(logs, kube_tools.registry, "logs MCP") as with_logs,
            mcp_tools(metrics, with_logs, "metrics MCP") as tools,
        ):
            return await run(question, model, tools=tools, tracing=tracing)
    finally:
        provider.shutdown()


def diagnose(question: str) -> RunResult:
    return asyncio.run(diagnose_async(question))


if __name__ == "__main__":
    result = diagnose(" ".join(sys.argv[1:]) or "Customers report checkout fails. Why?")
    print(result.answer)
    print(f"[spans] {get_settings().trace_file}", file=sys.stderr)
    print(
        f"[stop] {result.stop_reason} after {result.steps} steps, "
        f"{result.tokens} tokens, {result.seconds}s, "
        f"{result.cache_read_tokens} cache-read tokens",
        file=sys.stderr,
    )
