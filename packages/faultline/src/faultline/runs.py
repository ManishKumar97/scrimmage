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

from faultline.semconv import (
    CHAT,
    ERROR_TYPE,
    EXECUTE_TOOL,
    GEN_AI_INPUT_MESSAGES,
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
    error_type: str | None = None  # None means the model call succeeded
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
    error_type: str | None = None  # the root span ended in ERROR
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


@dataclass
class Reading:
    """What a span file gave: its runs, and the lines that gave nothing."""

    runs: list[Run]
    unreadable: int  # not UTF-8, not JSON, or not a JSON object
    no_spans: int  # valid JSON with no resourceSpans, so probably not a span file


def read_runs(path: Path) -> Reading:
    traces: dict[str, list[dict]] = {}
    unreadable = no_spans = 0
    with path.open("rb") as file:
        for raw in file:
            if not raw.strip():
                continue
            try:
                request = json.loads(raw.decode("utf-8"))
            except ValueError:  # includes a bad UTF-8 byte
                unreadable += 1  # for example a cut final line after a crash
                continue
            if not isinstance(request, dict):
                unreadable += 1
                continue
            if not request.get("resourceSpans"):
                no_spans += 1
                continue
            try:
                spans = spans_in(request)
            except (TypeError, ValueError, KeyError, AttributeError):
                unreadable += 1  # a span file shape this reader does not know
                continue
            for span in spans:
                traces.setdefault(span["traceId"], []).append(span)
    runs = [run for tid, spans in traces.items() if (run := build_run(tid, spans))]
    return Reading(runs, unreadable, no_spans)


def spans_in(request: dict) -> list[dict]:
    """Every span in one request, with its attributes decoded. Raises on a bad shape."""
    spans = []
    for resource in request["resourceSpans"]:
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                span["attrs"] = attributes_of(span)
                if not span.get("traceId"):
                    raise ValueError("span has no traceId")  # it cannot join a run
                spans.append(span)
    return spans


def operation(span: dict) -> str | None:
    return span["attrs"].get(GEN_AI_OPERATION_NAME)


def started(span: dict) -> int:
    # The protobuf JSON writer leaves out a zero time.
    return int(span.get("startTimeUnixNano", 0))


def seconds_of(span: dict) -> float:
    return (int(span["endTimeUnixNano"]) - started(span)) / NANOS


def build_run(trace_id: str, spans: list[dict]) -> Run | None:
    """One run from the spans of one trace. A trace with no model call is not a run."""
    chats = sorted((s for s in spans if operation(s) == CHAT), key=started)
    if not chats:
        return None
    roots = [s for s in spans if operation(s) == INVOKE_AGENT]
    # The run's root has no parent. If that does not pick one, the earliest wins.
    parentless = [s for s in roots if not s.get("parentSpanId")]
    candidates = parentless if len(parentless) == 1 else roots
    root = min(candidates, key=started) if candidates else None
    attrs = root["attrs"] if root else {}
    run = Run(
        trace_id=trace_id,
        model=attrs.get(GEN_AI_REQUEST_MODEL)
        or chats[0]["attrs"].get(GEN_AI_REQUEST_MODEL),
        stop_reason=attrs.get("faultline.stop_reason"),
        question=attrs.get("faultline.question") or first_user_text(chats[0]),
        answer=attrs.get("faultline.answer"),
        seconds=seconds_of(root) if root else None,
        complete=root is not None,
        error_type=error_type_of(root) if root else None,
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
    final = run.steps[-1]
    # Only a last reply that ends the turn is an answer. A run cut off mid-way ends in
    # narration before tool calls, which is not one.
    if run.answer is None and is_final(final):
        run.answer = final.text
    return run


def output_parts(span: dict) -> list[dict]:
    """The parts of the model's reply. Unreadable output gives no parts."""
    try:
        messages = json.loads(span["attrs"].get(GEN_AI_OUTPUT_MESSAGES) or "[]")
        return [part for message in messages for part in message.get("parts", [])]
    except (ValueError, AttributeError, TypeError):
        return []


def is_final(step: Step) -> bool:
    asked_tools = step.finish_reason in ("tool_call", "tool_calls")
    return not (step.tool_calls or step.error_type or asked_tools)


def first_user_text(span: dict) -> str | None:
    """The task, from the first user message sent in a model call (GenAI input shape)."""
    try:
        for message in json.loads(span["attrs"].get(GEN_AI_INPUT_MESSAGES) or "[]"):
            if message.get("role") == "user":
                for part in message.get("parts", []):
                    if part.get("type") == "text":
                        return part["content"]
    except (ValueError, AttributeError, TypeError, KeyError):
        pass
    return None


def error_type_of(span: dict) -> str | None:
    """The span's error type if it ended in ERROR, else None."""
    if span.get("status", {}).get("code") != STATUS_ERROR:
        return None
    return span["attrs"].get(ERROR_TYPE) or "error"


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
        error_type=error_type_of(span),
        started=started(span),
    )


def tool_call_of(span: dict) -> ToolCall:
    attrs = span["attrs"]
    return ToolCall(
        name=attrs.get(GEN_AI_TOOL_NAME, "?"),
        call_id=attrs.get(GEN_AI_TOOL_CALL_ID, ""),
        arguments=attrs.get(GEN_AI_TOOL_CALL_ARGUMENTS, ""),
        seconds=seconds_of(span),
        result=attrs.get(GEN_AI_TOOL_CALL_RESULT, ""),
        error_type=error_type_of(span),
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
    if run.error_type:
        head += f" ERROR {run.error_type}"
    lines = [head, f"  question: {cut(run.question, 300)}"]
    for call in run.unplaced:
        lines.append("  before any step:")
        lines.append(show_call(call))
    for step in run.steps:
        line = f"  step {step.number} in={step.input_tokens} out={step.output_tokens}"
        line += f" finish={step.finish_reason}"
        if step.error_type:
            line += f" ERROR {step.error_type}"
        lines.append(line)
        if step.text:
            lines.append(f"    says: {cut(step.text, 200)}")
        lines += [show_call(call) for call in step.tool_calls]
    lines.append(f"  answer: {cut(run.answer, 300)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the runs in OTLP span files.")
    parser.add_argument("files", nargs="+", type=Path)
    for path in parser.parse_args().files:
        reading = read_runs(path)
        print(f"{path}: {len(reading.runs)} run(s)")
        for run in reading.runs:
            print(show_run(run))
        if reading.unreadable:
            print(f"{path}: skipped {reading.unreadable} unreadable line(s)")
        if reading.no_spans:
            print(f"{path}: {reading.no_spans} line(s) held no spans")


if __name__ == "__main__":
    main()
