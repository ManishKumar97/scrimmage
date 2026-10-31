"""Streamed replies read back as the reply a plain call would give."""

import json
from pathlib import Path

from sse import (
    ANTHROPIC_TEXT_STREAM,
    ANTHROPIC_TOOL_STREAM,
    OPENAI_TEXT_STREAM,
    OPENAI_TOOL_STREAM,
    UNKNOWN_STREAM,
)

from faultline.proxy.record import Exchange, Recorder
from faultline.proxy.streams import reply_from_stream
from faultline.runs import read_runs

QUESTION = {"role": "user", "content": "Why does the cart fail?"}


def test_an_openai_stream_rebuilds_text_tool_calls_finish_and_usage():
    asked = reply_from_stream(OPENAI_TOOL_STREAM)
    said = reply_from_stream(OPENAI_TEXT_STREAM)

    assert asked and said
    message = asked["choices"][0]["message"]
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_logs", "arguments": '{"service": "cart"}'},
        }
    ]
    assert asked["choices"][0]["finish_reason"] == "tool_calls"
    assert asked["usage"]["prompt_tokens"] == 50
    assert said["choices"][0]["message"]["content"] == "cart is down"
    assert "usage" not in said  # the agent did not ask for it


def test_an_anthropic_stream_rebuilds_blocks_stop_reason_and_usage():
    reply = reply_from_stream(ANTHROPIC_TOOL_STREAM)

    assert reply is not None
    assert reply["content"] == [
        {"type": "text", "text": "Checking logs."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "get_logs",
            "input": {"service": "cart"},
        },
    ]
    assert reply["stop_reason"] == "tool_use"
    assert reply["usage"]["output_tokens"] == 12
    assert reply["usage"]["cache_read_input_tokens"] == 90


def test_a_stream_in_no_known_shape_gives_nothing():
    assert reply_from_stream(UNKNOWN_STREAM) is None
    assert reply_from_stream(b"data: not json\n\n") is None


def record(tmp_path: Path, path: str, replies: list[bytes]) -> Recorder:
    recorder = Recorder(tmp_path / "model.jsonl", "ab" * 16, "cd" * 8)
    for step, reply in enumerate(replies, start=1):
        body = json.dumps({"model": "m", "stream": True, "messages": [QUESTION] * step})
        recorder.exchange(
            Exchange(step, 1, path, body.encode(), 200, reply, step * 10, step * 10 + 5)
        )
    recorder.flush()
    return recorder


def test_a_streamed_openai_run_reads_back_like_a_plain_one(tmp_path):
    recorder = record(
        tmp_path, "/chat/completions", [OPENAI_TOOL_STREAM, OPENAI_TEXT_STREAM]
    )

    [run] = read_runs(recorder.path).runs
    assert [(s.input_tokens, s.finish_reason) for s in run.steps] == [
        (50, "tool_call"),
        (None, "stop"),
    ]
    assert run.answer == "cart is down"
    assert not recorder.last_unparsed


def test_a_streamed_anthropic_run_reads_back_like_a_plain_one(tmp_path):
    recorder = record(
        tmp_path, "/v1/messages", [ANTHROPIC_TOOL_STREAM, ANTHROPIC_TEXT_STREAM]
    )

    [run] = read_runs(recorder.path).runs
    assert [(s.input_tokens, s.output_tokens, s.finish_reason) for s in run.steps] == [
        (100, 12, "tool_call"),
        (100, 12, "stop"),
    ]
    assert run.steps[0].text == "Checking logs."
    assert run.answer == "cart is down"


def test_an_unknown_stream_is_marked_so_the_verdict_can_say_so(tmp_path):
    recorder = record(tmp_path, "/chat/completions", [UNKNOWN_STREAM])

    span = json.loads(recorder.path.read_text())["resourceSpans"][0]["scopeSpans"][0][
        "spans"
    ][0]
    attributes = {a["key"]: a["value"] for a in span["attributes"]}
    assert attributes["faultline.proxy.stream"] == {"stringValue": "unparsed"}
    assert recorder.last_unparsed
    assert read_runs(recorder.path).runs[0].answer is None
