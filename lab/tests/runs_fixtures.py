"""Span files written by the lab's tracer, for the run reader's tests in the product.

Each builder writes one span file with the real exporter. Times are given, so order is fixed.
The product's tests read committed copies, so they never import the lab.
The lab's contract test rebuilds each file and checks the reader sees the same runs.

Regenerate the committed copies after a deliberate change to the tracer:

    uv run python lab/tests/runs_fixtures.py
"""

import json
from collections.abc import Callable
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from faultline import genai
from faultline_lab import tracing
from faultline_lab.models import Message, Reply, ToolCall

SECOND = 1_000_000_000
BASE = 1_700_000_000 * SECOND  # a zero time is left out of the OTLP JSON

FIXTURES = Path(__file__).resolve().parents[2] / "packages/faultline/tests/fixtures"


class Writer:
    """Writes spans to a file with the real exporter."""

    def __init__(self, path: Path) -> None:
        self.tracer = tracing.file_provider(path).get_tracer("test")

    def root(
        self,
        start: int,
        end: int,
        *,
        parent: Span | None = None,
        extras: bool = True,
        error: str | None = None,
    ) -> Span:
        attributes = {
            genai.GEN_AI_OPERATION_NAME: genai.INVOKE_AGENT,
            genai.GEN_AI_REQUEST_MODEL: "fake-model",
        }
        if extras:  # what only the IDA writes
            attributes["faultline.question"] = "why?"
            attributes["faultline.stop_reason"] = "done"
            attributes["faultline.answer"] = "because"
        span = self.tracer.start_span(
            "invoke_agent",
            context=trace.set_span_in_context(parent) if parent else None,
            attributes=attributes,
            start_time=BASE + start,
        )
        if error:
            span.set_attribute(genai.ERROR_TYPE, error)
            span.set_status(Status(StatusCode.ERROR, "failed"))
        span.end(BASE + end)
        return span

    def chat(
        self,
        parent: Span,
        start: int,
        calls: list[ToolCall],
        text: str = "",
        *,
        question: str | None = None,
        inputs: list[Message] | None = None,
        error: str | None = None,
    ) -> None:
        reply = Reply(
            text,
            calls,
            "tool_calls" if calls else "stop",
            tokens=30,
            input_tokens=20,
            output_tokens=10,
        )
        attributes = tracing.model_attributes("fake", "fake-model", 0)
        span = self.tracer.start_span(
            "chat",
            context=trace.set_span_in_context(parent),
            attributes=attributes,
            start_time=BASE + start,
        )
        inputs = inputs or ([question] if question else [])
        if inputs:
            span.set_attribute(
                genai.GEN_AI_INPUT_MESSAGES, tracing.messages_json(inputs)
            )
        if error:  # a failed model call has no reply
            span.set_attribute(genai.ERROR_TYPE, error)
            span.set_status(Status(StatusCode.ERROR, "failed"))
            span.end(BASE + start + SECOND)
            return
        tracing.record_reply(span, reply)
        span.set_attribute(genai.GEN_AI_OUTPUT_MESSAGES, tracing.output_json(reply))
        span.end(BASE + start + SECOND)

    def tool(
        self,
        parent: Span,
        start: int,
        call_id: str,
        name: str = "get_logs",
        error: str | None = None,
    ) -> None:
        attributes = tracing.tool_attributes(name, call_id, 0)
        attributes[genai.GEN_AI_TOOL_CALL_ARGUMENTS] = '{"service": "cart"}'
        span = self.tracer.start_span(
            "execute_tool",
            context=trace.set_span_in_context(parent),
            attributes=attributes,
            start_time=BASE + start,
        )
        span.set_attribute(genai.GEN_AI_TOOL_CALL_RESULT, error or "lines")
        if error:
            span.set_attribute(genai.ERROR_TYPE, "tool_error")
            span.set_status(Status(StatusCode.ERROR, error))
        span.end(BASE + start + SECOND // 2)


def call(call_id: str, name: str = "get_logs") -> ToolCall:
    return ToolCall(call_id, name, '{"service": "cart"}')


BUILDERS: dict[str, Callable[[Path], None]] = {}


def fixture(build: Callable[[Path], None]) -> Callable[[Path], None]:
    BUILDERS[build.__name__] = build
    return build


@fixture
def normal_run(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a"), call("b", "list_pods")], "looking")
    writer.tool(root, 2 * SECOND, "a")
    writer.tool(root, 3 * SECOND, "b", "list_pods")
    writer.chat(root, 4 * SECOND, [], "done")


@fixture
def id_wins_over_time(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.chat(root, 3 * SECOND, [call("b")])
    # Started after step 2, but step 1 asked for it: the id wins over the time.
    writer.tool(root, 5 * SECOND, "a")


@fixture
def unclaimed_tool_call(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [])
    writer.chat(root, 3 * SECOND, [])
    writer.tool(root, 4 * SECOND, "unknown")


@fixture
def tool_before_any_step(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.tool(root, 1 * SECOND, "early")
    writer.chat(root, 2 * SECOND, [])


@fixture
def no_root_span(path: Path) -> None:
    writer = Writer(path)
    # The root ends last, so a crashed process leaves it unwritten.
    unfinished = writer.tracer.start_span("invoke_agent", start_time=0)
    writer.chat(unfinished, 1 * SECOND, [call("a")])
    writer.tool(unfinished, 2 * SECOND, "a")


@fixture
def two_traces(path: Path) -> None:
    writer = Writer(path)
    first = writer.root(0, 5 * SECOND)
    writer.chat(first, 1 * SECOND, [])
    second = writer.root(10 * SECOND, 15 * SECOND)
    writer.chat(second, 11 * SECOND, [call("x")])
    writer.chat(second, 12 * SECOND, [])


@fixture
def cut_final_line(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [])
    with path.open("a") as file:
        file.write('{"resourceSpans": [{"scopeSp')  # the process died mid-write


@fixture
def tool_error(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.tool(root, 2 * SECOND, "a", error="pod not found")


@fixture
def tool_ok(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.tool(root, 2 * SECOND, "a")


@fixture
def no_model_call(path: Path) -> None:
    writer = Writer(path)
    writer.root(0, 5 * SECOND)


@fixture
def model_error(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, error="RateLimitError")
    writer.chat(root, 1 * SECOND, [], error="RateLimitError")


@fixture
def one_step(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [])


@fixture
def bad_utf8_line(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    with path.open("ab") as file:
        file.write(b'{"resourceSpans": "\xff\xfe"}\n')
    writer.chat(root, 1 * SECOND, [])


@fixture
def two_steps_ask_one_id(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.chat(root, 3 * SECOND, [call("a")])
    writer.tool(root, 5 * SECOND, "a")


@fixture
def tool_starts_with_step(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 3 * SECOND, [])
    writer.tool(root, 3 * SECOND, "unknown")


@fixture
def no_extras(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND, extras=False)
    writer.chat(root, 1 * SECOND, [call("a")], "checking", question="why down?")
    writer.chat(root, 3 * SECOND, [], "the cause is X")


@fixture
def extras_and_messages(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [], "last words", question="other")


@fixture
def narration_then_crash(path: Path) -> None:
    writer = Writer(path)
    # No root span was written: the process died after the tool calls ran.
    unfinished = writer.tracer.start_span("invoke_agent", start_time=0)
    writer.chat(unfinished, 1 * SECOND, [call("a")], "I'll check the traces")
    writer.tool(unfinished, 2 * SECOND, "a")


@fixture
def narration_no_tool_spans(path: Path) -> None:
    writer = Writer(path)
    unfinished = writer.tracer.start_span("invoke_agent", start_time=0)
    writer.chat(unfinished, 1 * SECOND, [call("a")], "I'll check the traces")


@fixture
def failed_last_step(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, extras=False)
    writer.chat(root, 1 * SECOND, [], error="RateLimitError")


@fixture
def question_after_a_reply(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, extras=False)
    earlier = Reply("earlier reply", [], "stop", tokens=1)
    writer.chat(root, 1 * SECOND, [], inputs=[earlier, "the real question"])


@fixture
def unknown_shapes(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [])
    no_trace_id = {
        "spanId": "s",
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}
        ],
        "startTimeUnixNano": str(BASE + 1),
        "endTimeUnixNano": str(BASE + 2),
    }
    request = {"resourceSpans": [{"scopeSpans": [{"spans": [no_trace_id]}]}]}
    with path.open("a") as file:
        file.write('{"resourceSpans": 5}\n')
        file.write('{"resourceSpans": [{"scopeSpans": [{"spans": [null]}]}]}\n')
        file.write(json.dumps(request) + "\n")


@fixture
def reply_then_tool(path: Path) -> None:
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, extras=False)
    # The reply asked for no tool by its own record, but a tool ran right after it.
    writer.chat(root, 1 * SECOND, [], "all done")
    writer.tool(root, 2 * SECOND, "unclaimed")


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, build in BUILDERS.items():
        path = FIXTURES / f"{name}.jsonl"
        path.unlink(missing_ok=True)
        build(path)
        print(path)


if __name__ == "__main__":
    main()
