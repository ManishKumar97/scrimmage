"""Read an OTLP JSON lines span file into runs, steps and tool calls.

A run is one execution of the agent on one task.
A step is one model call plus the tool calls it asked for.
Spans are classified by the GenAI `gen_ai.operation.name` attribute, so a file from
any agent that follows the convention reads the same way.
The question, answer and stop reason may come from span attributes the user names
in the config. They are never required.

    python -m faultline.runs [--config PATH] FILE [FILE...]
"""

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path

from faultline.config import Attributes, ConfigError, load_config
from faultline.model import Run, Step, ToolCall
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


def read_runs(path: Path, attributes: Attributes = Attributes()) -> Reading:
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
    runs = [
        run
        for tid, spans in traces.items()
        if (run := build_run(tid, spans, attributes))
    ]
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


def build_run(
    trace_id: str, spans: list[dict], attributes: Attributes = Attributes()
) -> Run | None:
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
    steps = [step_of(number, span) for number, span in enumerate(chats, start=1)]
    # Which step asked for each tool call id, from its output messages.
    asked: dict[str, int] = {}
    for index, span in enumerate(chats):
        for call_id in asked_ids(span):
            asked.setdefault(call_id, index)
    # Collect each step's calls in lists, then freeze them into the steps.
    placed: list[list[ToolCall]] = [[] for _ in steps]
    unplaced: list[ToolCall] = []
    tools = sorted((s for s in spans if operation(s) == EXECUTE_TOOL), key=started)
    for span in tools:
        call = tool_call_of(span)
        index = asked.get(call.call_id)
        if index is None:
            index = last_started_before(steps, call.started)
        (placed[index] if index is not None else unplaced).append(call)
    steps = [
        replace(step, tool_calls=tuple(calls))
        for step, calls in zip(steps, placed, strict=True)
    ]
    answer = mapped(attrs, attributes.answer)
    # Only a last reply that ends the turn is an answer. A run cut off mid-way ends in
    # narration before tool calls, which is not one.
    if answer is None and is_final(steps[-1]):
        answer = steps[-1].text
    return Run(
        trace_id=trace_id,
        model=attrs.get(GEN_AI_REQUEST_MODEL)
        or chats[0]["attrs"].get(GEN_AI_REQUEST_MODEL),
        stop_reason=mapped(attrs, attributes.stop_reason),
        question=mapped(attrs, attributes.question) or first_user_text(chats[0]),
        answer=answer,
        seconds=seconds_of(root) if root else None,
        complete=root is not None,
        error_type=error_type_of(root) if root else None,
        steps=tuple(steps),
        unplaced=tuple(unplaced),
        content_recorded=any(carries_content(span) for span in spans),
    )


def mapped(attrs: dict, name: str | None):
    """The value of the attribute the user mapped, None when not mapped or not set."""
    return attrs.get(name) if name else None


def parts_of(messages: str | None) -> list[dict]:
    """The parts of GenAI messages in JSON. Raises on a shape this reader does not know."""
    return [
        part
        for message in json.loads(messages or "[]")
        for part in message.get("parts", [])
    ]


def output_parts(span: dict) -> list[dict]:
    """The parts of the model's reply. Unreadable output gives no parts."""
    try:
        return parts_of(span["attrs"].get(GEN_AI_OUTPUT_MESSAGES))
    except (ValueError, AttributeError, TypeError):
        return []


# Message part fields that hold content: text, tool call arguments, tool results,
# and a file by uri or id. An agent that does not record content may still write the
# messages, with parts that keep only their type, id and name, or empty fields.
CONTENT_FIELDS = ("content", "arguments", "response", "uri", "file_id")


def holds_content(part: dict) -> bool:
    return any(part.get(field) not in (None, "") for field in CONTENT_FIELDS)


def carries_content(span: dict) -> bool:
    """True when the span records content, not only the shape of the messages."""
    attrs = span["attrs"]
    if GEN_AI_TOOL_CALL_ARGUMENTS in attrs or GEN_AI_TOOL_CALL_RESULT in attrs:
        return True
    for key in (GEN_AI_INPUT_MESSAGES, GEN_AI_OUTPUT_MESSAGES):
        if key not in attrs:
            continue
        try:
            parts = parts_of(attrs[key])
            # No parts is an empty message, recorded as it was. Parts that all lack
            # their content fields had the content stripped.
            if not parts or any(holds_content(part) for part in parts):
                return True
        except (ValueError, AttributeError, TypeError):
            return True  # something is there, in a shape this reader does not know
    return False


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


def last_started_before(steps: list[Step], time: int) -> int | None:
    """The index of the last step that started at or before `time`, else None."""
    before = [index for index, step in enumerate(steps) if step.started <= time]
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
    if not run.content_recorded:
        lines.append("  content: not recorded")
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


def skip_notes(path: Path | str, reading: Reading) -> list[str]:
    """The lines of a file that gave nothing, as a report line each."""
    notes = []
    if reading.unreadable:
        notes.append(f"{path}: skipped {reading.unreadable} unreadable line(s)")
    if reading.no_spans:
        notes.append(f"{path}: {reading.no_spans} line(s) held no spans")
    return notes


def show_reading(path: Path | str, reading: Reading) -> str:
    """What `python -m faultline.runs` prints for one file."""
    lines = [f"{path}: {len(reading.runs)} run(s)"]
    lines += [show_run(run) for run in reading.runs]
    return "\n".join(lines + skip_notes(path, reading))


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the runs in OTLP span files.")
    parser.add_argument(
        "--config", type=Path, help="TOML file, default ./faultline.toml"
    )
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        attributes = load_config(args.config).attributes
    except ConfigError as error:
        parser.exit(2, f"runs: {error}\n")
    for path in args.files:
        print(show_reading(path, read_runs(path, attributes)))


if __name__ == "__main__":
    main()
