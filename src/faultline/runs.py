"""Read an OTLP JSON lines span file into runs, steps and tool calls.

A run is one execution of the agent on one task.
A step is one model call plus the tool calls it asked for.
Spans are classified by the GenAI `gen_ai.operation.name` attribute, so a file from
any agent that follows the convention reads the same way.
`faultline.*` attributes are read when present and are never required.

    python -m faultline.runs FILE [FILE...]
"""

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from faultline.tracing import (
    CHAT,
    ERROR_TYPE,
    EXECUTE_TOOL,
    GEN_AI_OPERATION_NAME,
    GEN_AI_OUTPUT_MESSAGES,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_TOOL_CALL_ARGUMENTS,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_CALL_RESULT,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INVOKE_AGENT,
)

STATUS_ERROR = 2  # OTLP status code
NANOS = 1_000_000_000


@dataclass
class ToolCall:
    name: str
    call_id: str
    arguments: str
    seconds: float
    result: str
    error_type: str | None = None  # None means the call succeeded
    started: int = 0  # unix nanoseconds


@dataclass
class Step:
    number: int
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None
    text: str
    started: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


@dataclass
class Run:
    trace_id: str
    model: str | None
    stop_reason: str | None
    question: str | None
    answer: str | None
    seconds: float | None  # None when the root span is missing
    complete: bool  # False when the root span is missing: the process died first
    steps: list[Step] = field(default_factory=list)
    unplaced: list[ToolCall] = field(default_factory=list)  # before any step

    @property
    def tokens(self) -> int:
        return sum(step.tokens for step in self.steps)

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [call for step in self.steps for call in step.tool_calls] + self.unplaced


def any_value(value: dict):
    """Decode an OTLP JSON AnyValue. Kinds this reader does not use give None."""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])  # a string in OTLP JSON
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [any_value(item) for item in value["arrayValue"].get("values", [])]
    return None


def attributes_of(span: dict) -> dict:
    return {kv["key"]: any_value(kv["value"]) for kv in span.get("attributes", [])}


def read_runs(path: Path) -> tuple[list[Run], int]:
    """The runs in a span file, and how many lines were skipped as unreadable."""
    traces: dict[str, list[dict]] = {}
    skipped = 0
    with path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                skipped += 1  # a cut final line after a crash
                continue
            for resource in request.get("resourceSpans", []):
                for scope in resource.get("scopeSpans", []):
                    for span in scope.get("spans", []):
                        span["attrs"] = attributes_of(span)
                        traces.setdefault(span["traceId"], []).append(span)
    runs = [run for id, spans in traces.items() if (run := build_run(id, spans))]
    return runs, skipped


def operation(span: dict) -> str | None:
    return span["attrs"].get(GEN_AI_OPERATION_NAME)


def started(span: dict) -> int:
    return int(span["startTimeUnixNano"])


def seconds_of(span: dict) -> float:
    return (int(span["endTimeUnixNano"]) - started(span)) / NANOS


def build_run(trace_id: str, spans: list[dict]) -> Run | None:
    """One run from the spans of one trace. A trace with no model call is not a run."""
    chats = sorted((s for s in spans if operation(s) == CHAT), key=started)
    if not chats:
        return None
    roots = [s for s in spans if operation(s) == INVOKE_AGENT]
    root = roots[0] if roots else None
    attrs = root["attrs"] if root else {}
    run = Run(
        trace_id=trace_id,
        model=attrs.get(GEN_AI_REQUEST_MODEL)
        or chats[0]["attrs"].get(GEN_AI_REQUEST_MODEL),
        stop_reason=attrs.get("faultline.stop_reason"),
        question=attrs.get("faultline.question"),
        answer=attrs.get("faultline.answer"),
        seconds=seconds_of(root) if root else None,
        complete=root is not None,
    )
    # Which tool call ids each step asked for, from its output messages.
    asked: dict[str, Step] = {}
    for number, span in enumerate(chats, start=1):
        step = step_of(number, span)
        run.steps.append(step)
        for call_id in asked_ids(span):
            asked.setdefault(call_id, step)
    tools = sorted((s for s in spans if operation(s) == EXECUTE_TOOL), key=started)
    for span in tools:
        call = tool_call_of(span)
        step = asked.get(call.call_id) or last_started_before(run.steps, call.started)
        (step.tool_calls if step else run.unplaced).append(call)
    return run


def output_parts(span: dict) -> list[dict]:
    """The parts of the model's reply. Unreadable output gives no parts."""
    try:
        messages = json.loads(span["attrs"].get(GEN_AI_OUTPUT_MESSAGES) or "[]")
        return [part for message in messages for part in message.get("parts", [])]
    except (ValueError, AttributeError, TypeError):
        return []


def asked_ids(span: dict) -> list[str]:
    parts = output_parts(span)
    return [p["id"] for p in parts if p.get("type") == "tool_call" and "id" in p]


def step_of(number: int, span: dict) -> Step:
    attrs = span["attrs"]
    reasons = attrs.get(GEN_AI_RESPONSE_FINISH_REASONS) or [None]
    text = "".join(
        p.get("content", "") for p in output_parts(span) if p.get("type") == "text"
    )
    return Step(
        number=number,
        input_tokens=attrs.get(GEN_AI_USAGE_INPUT_TOKENS),
        output_tokens=attrs.get(GEN_AI_USAGE_OUTPUT_TOKENS),
        finish_reason=reasons[0],
        text=text,
        started=started(span),
    )


def tool_call_of(span: dict) -> ToolCall:
    attrs = span["attrs"]
    failed = span.get("status", {}).get("code") == STATUS_ERROR
    return ToolCall(
        name=attrs.get(GEN_AI_TOOL_NAME, "?"),
        call_id=attrs.get(GEN_AI_TOOL_CALL_ID, ""),
        arguments=attrs.get(GEN_AI_TOOL_CALL_ARGUMENTS, ""),
        seconds=seconds_of(span),
        result=attrs.get(GEN_AI_TOOL_CALL_RESULT, ""),
        error_type=(attrs.get(ERROR_TYPE) or "error") if failed else None,
        started=started(span),
    )


def last_started_before(steps: list[Step], time: int) -> Step | None:
    before = [step for step in steps if step.started <= time]
    return before[-1] if before else None


def cut(text: str | None, limit: int) -> str:
    """One line, at most `limit` characters, with a marker when text was dropped."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}... [cut, {len(flat)} chars]"


def show_call(call: ToolCall) -> str:
    status = "ok" if call.error_type is None else f"error {call.error_type}"
    return (
        f"    tool {call.name}({cut(call.arguments, 100)}) {status}, "
        f"{call.seconds:.2f}s -> {cut(call.result, 100)}"
    )


def show_run(run: Run) -> str:
    seconds = "?" if run.seconds is None else f"{run.seconds:.1f}"
    state = "complete" if run.complete else "INCOMPLETE (no root span)"
    head = f"run {run.trace_id} model={run.model} stop={run.stop_reason}"
    head += f" steps={len(run.steps)} tokens={run.tokens} seconds={seconds} {state}"
    lines = [head, f"  question: {cut(run.question, 300)}"]
    for call in run.unplaced:
        lines.append("  before any step:")
        lines.append(show_call(call))
    for step in run.steps:
        lines.append(
            f"  step {step.number} in={step.input_tokens} out={step.output_tokens} "
            f"finish={step.finish_reason}"
        )
        if step.text:
            lines.append(f"    says: {cut(step.text, 200)}")
        lines += [show_call(call) for call in step.tool_calls]
    lines.append(f"  answer: {cut(run.answer, 300)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the runs in OTLP span files.")
    parser.add_argument("files", nargs="+", type=Path)
    for path in parser.parse_args().files:
        runs, skipped = read_runs(path)
        print(f"{path}: {len(runs)} run(s)")
        for run in runs:
            print(show_run(run))
        if skipped:
            print(f"{path}: skipped {skipped} unreadable line(s)")


if __name__ == "__main__":
    main()
