import json
import sys
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from faultline import tracing
from faultline.models import Reply, ToolCall
from faultline.runs import any_value, cut, main, read_runs, show_run

SECOND = 1_000_000_000
BASE = 1_700_000_000 * SECOND  # a zero time is left out of the OTLP JSON


class Writer:
    """Writes spans to a file with the real exporter. Times are given, so order is fixed."""

    def __init__(self, path: Path) -> None:
        self.tracer = tracing.file_provider(path).get_tracer("test")

    def root(self, start: int, end: int, *, trace_of: Span | None = None) -> Span:
        attributes = {
            tracing.GEN_AI_OPERATION_NAME: tracing.INVOKE_AGENT,
            tracing.GEN_AI_REQUEST_MODEL: "fake-model",
            "faultline.question": "why?",
            "faultline.stop_reason": "done",
            "faultline.answer": "because",
        }
        span = self.tracer.start_span(
            "invoke_agent", attributes=attributes, start_time=BASE + start
        )
        span.end(BASE + end)
        return span

    def chat(
        self, parent: Span, start: int, calls: list[ToolCall], text: str = ""
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
        tracing.record_reply(span, reply)
        span.set_attribute(tracing.GEN_AI_OUTPUT_MESSAGES, tracing.output_json(reply))
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
        attributes[tracing.GEN_AI_TOOL_CALL_ARGUMENTS] = '{"service": "cart"}'
        span = self.tracer.start_span(
            "execute_tool",
            context=trace.set_span_in_context(parent),
            attributes=attributes,
            start_time=BASE + start,
        )
        span.set_attribute(tracing.GEN_AI_TOOL_CALL_RESULT, error or "lines")
        if error:
            span.set_attribute(tracing.ERROR_TYPE, "tool_error")
            span.set_status(Status(StatusCode.ERROR, error))
        span.end(BASE + start + SECOND // 2)


def call(call_id: str, name: str = "get_logs") -> ToolCall:
    return ToolCall(call_id, name, '{"service": "cart"}')


def test_a_normal_run_has_its_steps_and_tool_calls(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a"), call("b", "list_pods")], "looking")
    writer.tool(root, 2 * SECOND, "a")
    writer.tool(root, 3 * SECOND, "b", "list_pods")
    writer.chat(root, 4 * SECOND, [], "done")

    runs, skipped = read_runs(path)

    assert skipped == 0
    [run] = runs
    assert (run.complete, run.model, run.stop_reason) == (True, "fake-model", "done")
    assert (run.question, run.answer, run.seconds) == ("why?", "because", 10.0)
    assert [s.number for s in run.steps] == [1, 2]
    assert run.steps[0].text == "looking"
    assert run.steps[0].finish_reason == "tool_calls"
    assert run.steps[1].finish_reason == "stop"
    assert (run.steps[0].input_tokens, run.steps[0].output_tokens) == (20, 10)
    assert run.tokens == 60
    assert [c.name for c in run.steps[0].tool_calls] == ["get_logs", "list_pods"]
    assert run.steps[0].tool_calls[0].arguments == '{"service": "cart"}'
    assert run.steps[0].tool_calls[0].seconds == 0.5
    assert run.steps[1].tool_calls == []


def test_a_tool_call_belongs_to_the_step_that_asked_for_its_id(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.chat(root, 3 * SECOND, [call("b")])
    # Started after step 2, but step 1 asked for it: the id wins over the time.
    writer.tool(root, 5 * SECOND, "a")

    [run], _ = read_runs(path)

    assert [c.call_id for c in run.steps[0].tool_calls] == ["a"]
    assert run.steps[1].tool_calls == []


def test_a_tool_call_no_step_claims_goes_to_the_last_step_before_it(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [])
    writer.chat(root, 3 * SECOND, [])
    writer.tool(root, 4 * SECOND, "unknown")

    [run], _ = read_runs(path)

    assert run.steps[0].tool_calls == []
    assert [c.call_id for c in run.steps[1].tool_calls] == ["unknown"]


def test_a_tool_call_before_any_step_is_kept_on_the_run(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.tool(root, 1 * SECOND, "early")
    writer.chat(root, 2 * SECOND, [])

    [run], _ = read_runs(path)

    assert [c.call_id for c in run.unplaced] == ["early"]
    assert run.steps[0].tool_calls == []
    assert len(run.tool_calls) == 1
    assert "before any step" in show_run(run)


def test_a_trace_with_no_root_span_is_an_incomplete_run(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    # The root ends last, so a crashed process leaves it unwritten.
    unfinished = writer.tracer.start_span("invoke_agent", start_time=0)
    writer.chat(unfinished, 1 * SECOND, [call("a")])
    writer.tool(unfinished, 2 * SECOND, "a")

    [run], _ = read_runs(path)

    assert run.complete is False
    assert run.seconds is None
    assert run.stop_reason is None
    assert run.model == "fake-model"
    assert len(run.steps) == 1
    assert len(run.tool_calls) == 1
    assert "INCOMPLETE" in show_run(run)


def test_two_traces_in_one_file_are_two_runs(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    first = writer.root(0, 5 * SECOND)
    writer.chat(first, 1 * SECOND, [])
    second = writer.root(10 * SECOND, 15 * SECOND)
    writer.chat(second, 11 * SECOND, [call("x")])
    writer.chat(second, 12 * SECOND, [])

    runs, _ = read_runs(path)

    assert len(runs) == 2
    assert runs[0].trace_id != runs[1].trace_id
    assert sorted(len(run.steps) for run in runs) == [1, 2]


def test_a_cut_final_line_is_skipped_and_reported(tmp_path, capsys, monkeypatch):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [])
    with path.open("a") as file:
        file.write('{"resourceSpans": [{"scopeSp')  # the process died mid-write

    runs, skipped = read_runs(path)
    assert (len(runs), skipped) == (1, 1)

    monkeypatch.setattr(sys, "argv", ["runs", str(path)])
    main()
    assert "skipped 1 unreadable line(s)" in capsys.readouterr().out


def test_an_error_tool_span_shows_its_error(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.tool(root, 2 * SECOND, "a", error="pod not found")

    [run], _ = read_runs(path)

    [failed] = run.tool_calls
    assert failed.error_type == "tool_error"
    assert failed.result == "pod not found"
    assert "error tool_error" in show_run(run)


def test_a_successful_tool_span_has_no_error(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.tool(root, 2 * SECOND, "a")

    [run], _ = read_runs(path)

    assert run.tool_calls[0].error_type is None


def test_a_trace_without_a_model_call_is_not_a_run(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    writer.root(0, 5 * SECOND)

    runs, skipped = read_runs(path)

    assert (runs, skipped) == ([], 0)


def test_any_value_decodes_every_kind_the_exporter_writes():
    assert any_value({"stringValue": "x"}) == "x"
    assert any_value({"intValue": "42"}) == 42
    assert any_value({"doubleValue": 1.5}) == 1.5
    assert any_value({"boolValue": False}) is False
    assert any_value({"arrayValue": {"values": [{"stringValue": "stop"}]}}) == ["stop"]
    assert any_value({"kvlistValue": {}}) is None


def test_long_text_is_cut_with_a_visible_marker():
    assert cut("short", 10) == "short"
    assert cut("a" * 10, 10) == "a" * 10
    assert cut("a" * 30, 10) == "a" * 10 + "... [cut, 30 chars]"
    assert cut("two\nlines  here", 50) == "two lines here"


def test_written_lines_are_the_otlp_shape_the_reader_expects(tmp_path):
    # Guards the fixtures: if the exporter changes shape, this fails before the others mislead.
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, SECOND)
    writer.chat(root, 1, [])

    request = json.loads(path.read_text().splitlines()[0])
    span = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32
    assert isinstance(span["startTimeUnixNano"], str)
