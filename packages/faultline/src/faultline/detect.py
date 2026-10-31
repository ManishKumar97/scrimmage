"""Find the faults in a run: a tool or model call that went wrong.

A fault is what happened to the agent, not how it reacted.
Each function reads one run and does no I/O.
"""

import json

from faultline.config import DetectConfig
from faultline.model import Fault, FaultKind, Origin, Run, ToolCall
from faultline.runs import cut

DETAIL_LIMIT = 80


def detect(run: Run, config: DetectConfig) -> tuple[Fault, ...]:
    """Every fault in the run, in the order it happened."""
    found: list[tuple[int, Fault]] = []  # (start time, fault); sort is stable
    for call in run.unplaced:
        found += [(call.started, f) for f in call_faults(run, None, call, config)]
    for step in run.steps:
        for call in step.tool_calls:
            found += [
                (call.started, f) for f in call_faults(run, step.number, call, config)
            ]
    found.sort(key=lambda item: item[0])
    return tuple(fault for _, fault in found)


def call_faults(
    run: Run, step: int | None, call: ToolCall, config: DetectConfig
) -> list[Fault]:
    origin = Origin.INJECTED if call.injected else Origin.OBSERVED

    def fault(kind: FaultKind, detail: str) -> list[Fault]:
        return [
            Fault(kind, run.trace_id, step, call.call_id, call.name, detail, origin)
        ]

    if call.error_type:
        return fault(FaultKind.TOOL_ERROR, call.error_type)
    error = error_in_result(call.result)
    if error:
        return fault(FaultKind.TOOL_ERROR, error)
    return []


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
    if value.get("error") is not None:
        return cut(f"error: {value['error']}", DETAIL_LIMIT)
    if value.get("isError") is True:
        return "isError"
    return None
