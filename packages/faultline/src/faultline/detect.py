"""Find the faults in a run: a tool or model call that went wrong.

A fault is what happened to the agent, not how it reacted.
Each function reads one run and does no I/O.
"""

import json

from faultline.config import DetectConfig
from faultline.model import Fault, FaultKind, Origin, Run, Step, ToolCall
from faultline.runs import cut

TIMEOUT_WORDS = ("timeout", "timedout", "timed out", "timed_out", "deadline")
RATE_LIMIT_WORDS = ("ratelimit", "rate_limit", "rate limit", "429")
TOKEN_LIMIT_REASONS = ("length", "max_tokens", "max_output_tokens")
DETAIL_LIMIT = 80


def detect(run: Run, config: DetectConfig) -> tuple[Fault, ...]:
    """Every fault in the run, in the order it happened."""
    found: list[tuple[int, Fault]] = []  # (start time, fault); sort is stable
    for call in run.unplaced:
        found += [(call.started, f) for f in call_faults(run, None, call, config)]
    for step in run.steps:
        found += [(step.started, f) for f in step_faults(run, step)]
        for call in step.tool_calls:
            found += [
                (call.started, f) for f in call_faults(run, step.number, call, config)
            ]
    found.sort(key=lambda item: item[0])
    return tuple(fault for _, fault in found)


def step_faults(run: Run, step: Step) -> list[Fault]:
    origin = Origin.INJECTED if step.injected else Origin.OBSERVED
    kinds: list[tuple[FaultKind, str]] = []
    if step.error_type:
        lowered = step.error_type.lower()
        limited = any(word in lowered for word in RATE_LIMIT_WORDS)
        kind = FaultKind.MODEL_RATE_LIMIT if limited else FaultKind.MODEL_ERROR
        kinds.append((kind, step.error_type))
    if (step.finish_reason or "").lower() in TOKEN_LIMIT_REASONS:
        kinds.append((FaultKind.MODEL_TOKEN_LIMIT, f"finish {step.finish_reason}"))
    return [
        Fault(kind, run.trace_id, step.number, None, None, detail, origin)
        for kind, detail in kinds
    ]


def call_faults(
    run: Run, step: int | None, call: ToolCall, config: DetectConfig
) -> list[Fault]:
    origin = Origin.INJECTED if call.injected else Origin.OBSERVED

    def fault(kind: FaultKind, detail: str) -> list[Fault]:
        return [
            Fault(kind, run.trace_id, step, call.call_id, call.name, detail, origin)
        ]

    timed_out = timeout_detail(call, config)
    if timed_out:  # a timeout is not also an error
        return fault(FaultKind.TOOL_TIMEOUT, timed_out)
    if call.error_type:
        return fault(FaultKind.TOOL_ERROR, call.error_type)
    # Without recorded content every result reads as empty, which says nothing.
    if not run.content_recorded:
        return []
    error = error_in_result(call.result)
    if error:
        return fault(FaultKind.TOOL_ERROR, error)
    if is_empty(call.result):
        return fault(
            FaultKind.EMPTY_RESULT, cut(call.result, DETAIL_LIMIT) or "(blank)"
        )
    return []


def timeout_detail(call: ToolCall, config: DetectConfig) -> str | None:
    """Why the call counts as a timeout: its error type says so, or it ran too long."""
    lowered = (call.error_type or "").lower()
    if any(word in lowered for word in TIMEOUT_WORDS):
        return call.error_type
    if call.seconds > config.tool_timeout_seconds:
        return f"ran {call.seconds:.1f}s, limit {config.tool_timeout_seconds:g}s"
    return None


def parse_json(text: str):
    """The JSON value in `text`, or a marker that it is not JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return NOT_JSON


NOT_JSON = object()


def error_in_result(result: str) -> str | None:
    """Why a call that did not raise still reports an error, else None.

    A tool may return an error as data: {"error": ...} or MCP's {"isError": true}.
    """
    value = parse_json(result)
    if not isinstance(value, dict):
        return None
    if value.get("error"):  # false, "", null, 0, {} and [] mean no error
        return cut(f"error: {json.dumps(value['error'])}", DETAIL_LIMIT)
    if value.get("isError") is True:
        return "isError"
    return None


def is_empty(result: str) -> bool:
    """True for a blank result or JSON that holds nothing: null, "", [], {}, or an
    object whose every value is null or an empty string, list or object."""
    text = result.strip()
    if not text:
        return True
    value = parse_json(text)
    if value is NOT_JSON:
        return False
    if isinstance(value, dict):
        return all(blank(v) for v in value.values())
    return blank(value)


def blank(value) -> bool:
    """None, or an empty string, list or object."""
    return value is None or (isinstance(value, str | list | dict) and not value)
