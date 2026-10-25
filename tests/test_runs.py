import json
import sys
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from faultline import tracing
from faultline.models import Message, Reply, ToolCall
from faultline.runs import any_value, cut, main, read_runs, show_run

SECOND = 1_000_000_000
BASE = 1_700_000_000 * SECOND  # a zero time is left out of the OTLP JSON


class Writer:
    """Writes spans to a file with the real exporter. Times are given, so order is fixed."""

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
            tracing.GEN_AI_OPERATION_NAME: tracing.INVOKE_AGENT,
            tracing.GEN_AI_REQUEST_MODEL: "fake-model",
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
            span.set_attribute(tracing.ERROR_TYPE, error)
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
                tracing.GEN_AI_INPUT_MESSAGES, tracing.messages_json(inputs)
            )
        if error:  # a failed model call has no reply
            span.set_attribute(tracing.ERROR_TYPE, error)
            span.set_status(Status(StatusCode.ERROR, "failed"))
            span.end(BASE + start + SECOND)
            return
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

    reading = read_runs(path)

    assert (reading.unreadable, reading.no_spans) == (0, 0)
    [run] = reading.runs
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

    [run] = read_runs(path).runs

    assert [c.call_id for c in run.steps[0].tool_calls] == ["a"]
    assert run.steps[1].tool_calls == []


def test_a_tool_call_no_step_claims_goes_to_the_last_step_before_it(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [])
    writer.chat(root, 3 * SECOND, [])
    writer.tool(root, 4 * SECOND, "unknown")

    [run] = read_runs(path).runs

    assert run.steps[0].tool_calls == []
    assert [c.call_id for c in run.steps[1].tool_calls] == ["unknown"]


def test_a_tool_call_before_any_step_is_kept_on_the_run(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.tool(root, 1 * SECOND, "early")
    writer.chat(root, 2 * SECOND, [])

    [run] = read_runs(path).runs

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

    [run] = read_runs(path).runs

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

    runs = read_runs(path).runs

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

    reading = read_runs(path)
    assert (len(reading.runs), reading.unreadable) == (1, 1)

    monkeypatch.setattr(sys, "argv", ["runs", str(path)])
    main()
    assert "skipped 1 unreadable line(s)" in capsys.readouterr().out


def test_an_error_tool_span_shows_its_error(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.tool(root, 2 * SECOND, "a", error="pod not found")

    [run] = read_runs(path).runs

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

    [run] = read_runs(path).runs

    assert run.tool_calls[0].error_type is None


def test_a_trace_without_a_model_call_is_not_a_run(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    writer.root(0, 5 * SECOND)

    reading = read_runs(path)

    assert (reading.runs, reading.unreadable) == ([], 0)


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


def test_a_failed_model_call_is_a_step_with_its_error(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, error="RateLimitError")
    writer.chat(root, 1 * SECOND, [], error="RateLimitError")

    [run] = read_runs(path).runs

    assert [s.error_type for s in run.steps] == ["RateLimitError"]
    assert run.error_type == "RateLimitError"
    text = show_run(run)
    assert "step 1 in=None out=None finish=None ERROR RateLimitError" in text
    assert "seconds=5.0 complete ERROR RateLimitError" in text


def test_a_successful_step_and_run_have_no_error(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [])

    [run] = read_runs(path).runs

    assert run.error_type is None
    assert run.steps[0].error_type is None
    assert "ERROR" not in show_run(run)


def test_a_line_with_bad_utf8_is_skipped_and_the_rest_is_read(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    with path.open("ab") as file:
        file.write(b'{"resourceSpans": "\xff\xfe"}\n')
    writer.chat(root, 1 * SECOND, [])

    reading = read_runs(path)

    assert reading.unreadable == 1
    assert len(reading.runs) == 1
    assert len(reading.runs[0].steps) == 1


def test_a_line_that_is_json_but_not_an_object_is_skipped(tmp_path):
    path = tmp_path / "spans.jsonl"
    path.write_text('[1, 2]\n"text"\n42\nnull\n')

    reading = read_runs(path)

    assert (reading.runs, reading.unreadable, reading.no_spans) == ([], 4, 0)


def test_lines_with_no_spans_are_counted_and_reported(tmp_path, capsys, monkeypatch):
    path = tmp_path / "results.jsonl"
    path.write_text('{"incident": "a", "steps": 3}\n{"incident": "b"}\n\n')

    assert read_runs(path).no_spans == 2

    monkeypatch.setattr(sys, "argv", ["runs", str(path)])
    main()
    out = capsys.readouterr().out
    assert "0 run(s)" in out
    assert "2 line(s) held no spans" in out


def raw_span(operation: str, start: int | None, end: int | None, **extra) -> dict:
    """A hand-written OTLP span in trace "t1". A missing time is left out."""
    span = {
        "traceId": "t1",
        "spanId": extra.pop("span_id", "s"),
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": operation}}
        ],
        **extra,
    }
    if start is not None:
        span["startTimeUnixNano"] = str(BASE + start)
    if end is not None:
        span["endTimeUnixNano"] = str(BASE + end)
    return span


def write_raw(path: Path, spans: list[dict]) -> None:
    request = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    path.write_text(json.dumps(request) + "\n")


def test_with_two_roots_the_one_without_a_parent_is_the_run_root(tmp_path):
    path = tmp_path / "spans.jsonl"
    write_raw(
        path,
        [
            # A nested agent run that started first, but has a parent.
            raw_span("invoke_agent", 0, 2 * SECOND, span_id="n", parentSpanId="r"),
            raw_span("invoke_agent", 1 * SECOND, 10 * SECOND, span_id="r"),
            raw_span("chat", 2 * SECOND, 3 * SECOND),
        ],
    )

    [run] = read_runs(path).runs

    assert run.seconds == 9.0


def test_with_no_single_parentless_root_the_earliest_root_is_used(tmp_path):
    path = tmp_path / "spans.jsonl"
    write_raw(
        path,
        [
            raw_span("invoke_agent", 5 * SECOND, 6 * SECOND, span_id="b"),
            raw_span("invoke_agent", 1 * SECOND, 4 * SECOND, span_id="a"),
            raw_span("invoke_agent", 7 * SECOND, 9 * SECOND, span_id="c"),
            raw_span("chat", 2 * SECOND, 3 * SECOND),
        ],
    )

    [run] = read_runs(path).runs

    assert run.seconds == 3.0


def test_a_span_with_no_times_does_not_crash(tmp_path):
    path = tmp_path / "spans.jsonl"
    write_raw(
        path,
        [
            raw_span("invoke_agent", None, None, span_id="r"),
            raw_span("chat", None, None),
            raw_span("execute_tool", None, None),
        ],
    )

    [run] = read_runs(path).runs

    assert len(run.steps) == 1
    assert len(run.tool_calls) == 1


def test_when_two_steps_ask_for_one_id_the_first_gets_the_call(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [call("a")])
    writer.chat(root, 3 * SECOND, [call("a")])
    writer.tool(root, 5 * SECOND, "a")

    [run] = read_runs(path).runs

    assert len(run.steps[0].tool_calls) == 1
    assert run.steps[1].tool_calls == []


def test_a_tool_call_that_starts_with_a_step_belongs_to_it(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 3 * SECOND, [])
    writer.tool(root, 3 * SECOND, "unknown")

    [run] = read_runs(path).runs

    assert run.unplaced == []
    assert len(run.steps[0].tool_calls) == 1


def test_question_and_answer_fall_back_to_the_messages(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND, extras=False)
    writer.chat(root, 1 * SECOND, [call("a")], "checking", question="why down?")
    writer.chat(root, 3 * SECOND, [], "the cause is X")

    [run] = read_runs(path).runs

    assert run.question == "why down?"
    assert run.answer == "the cause is X"
    assert run.stop_reason is None


def test_faultline_attributes_win_over_the_message_fallback(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 10 * SECOND)
    writer.chat(root, 1 * SECOND, [], "last words", question="other")

    [run] = read_runs(path).runs

    assert (run.question, run.answer) == ("why?", "because")


def test_narration_before_tool_calls_in_a_cut_run_is_not_the_answer(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    # No root span was written: the process died after the tool calls ran.
    unfinished = writer.tracer.start_span("invoke_agent", start_time=0)
    writer.chat(unfinished, 1 * SECOND, [call("a")], "I'll check the traces")
    writer.tool(unfinished, 2 * SECOND, "a")

    [run] = read_runs(path).runs

    assert run.complete is False
    assert run.answer is None


def test_narration_is_not_the_answer_when_the_tool_spans_were_never_written(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    unfinished = writer.tracer.start_span("invoke_agent", start_time=0)
    writer.chat(unfinished, 1 * SECOND, [call("a")], "I'll check the traces")

    [run] = read_runs(path).runs

    assert run.steps[0].tool_calls == []
    assert run.answer is None


def test_a_failed_last_step_is_not_the_answer(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, extras=False)
    writer.chat(root, 1 * SECOND, [], error="RateLimitError")

    [run] = read_runs(path).runs

    assert run.answer is None


def test_the_question_skips_messages_that_are_not_from_the_user(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, extras=False)
    earlier = Reply("earlier reply", [], "stop", tokens=1)
    writer.chat(root, 1 * SECOND, [], inputs=[earlier, "the real question"])

    [run] = read_runs(path).runs

    assert run.question == "the real question"


def test_lines_of_an_unknown_shape_are_unreadable_not_fatal(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND)
    writer.chat(root, 1 * SECOND, [])
    no_trace_id = raw_span("chat", 1, 2)
    del no_trace_id["traceId"]
    with path.open("a") as file:
        file.write('{"resourceSpans": 5}\n')
        file.write('{"resourceSpans": [{"scopeSpans": [{"spans": [null]}]}]}\n')
    write_raw_append(path, [no_trace_id])

    reading = read_runs(path)

    assert reading.unreadable == 3
    assert len(reading.runs) == 1


def write_raw_append(path: Path, spans: list[dict]) -> None:
    request = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    with path.open("a") as file:
        file.write(json.dumps(request) + "\n")


def test_a_reply_followed_by_a_tool_call_is_not_the_answer(tmp_path):
    path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, 5 * SECOND, extras=False)
    # The reply asked for no tool by its own record, but a tool ran right after it.
    writer.chat(root, 1 * SECOND, [], "all done")
    writer.tool(root, 2 * SECOND, "unclaimed")

    [run] = read_runs(path).runs

    assert run.answer is None
