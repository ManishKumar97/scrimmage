"""A reply cut short at the token limit, plain and streamed, in both providers' shapes."""

import json

import pytest
from sse import (
    ANTHROPIC_TEXT_STREAM,
    ANTHROPIC_TOOL_STREAM,
    OPENAI_TEXT_STREAM,
    OPENAI_TOOL_STREAM,
    UNKNOWN_STREAM,
    anthropic_stream,
)
from upstream import ASKS

from faultline.proxy.streams import reply_from_stream
from faultline.proxy.truncate import Unchanged, truncated


def test_an_openai_reply_keeps_half_its_text_and_stops_at_the_length_limit():
    reply = {
        "id": "r",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "the cart is down"},
                "finish_reason": "stop",
            }
        ],
    }

    cut = json.loads(truncated(json.dumps(reply).encode(), anthropic=False))

    assert cut["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "the cart"},
            "finish_reason": "length",
        }
    ]


def test_an_openai_reply_loses_its_tool_calls():
    cut = json.loads(truncated(ASKS, anthropic=False))

    assert cut["choices"][0]["message"] == {"role": "assistant", "content": None}
    assert cut["choices"][0]["finish_reason"] == "length"
    assert cut["usage"] == json.loads(ASKS)["usage"]  # the rest is as it was


def test_an_anthropic_reply_keeps_half_its_text_and_stops_at_max_tokens():
    reply = {
        "type": "message",
        "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "s"},
            {"type": "text", "text": "Checking "},
            {"type": "text", "text": "logs now."},
            {"type": "tool_use", "id": "t", "name": "get_logs", "input": {}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
    }

    cut = json.loads(truncated(json.dumps(reply).encode(), anthropic=True))

    assert cut["content"] == [
        {"type": "thinking", "thinking": "hmm", "signature": "s"},
        {"type": "text", "text": "Checking "},  # 18 characters, 9 kept
        {"type": "text", "text": ""},
    ]
    assert cut["stop_reason"] == "max_tokens"


@pytest.mark.parametrize(
    ("stream", "text"),
    [(OPENAI_TEXT_STREAM, "cart i"), (OPENAI_TOOL_STREAM, None)],
)
def test_an_openai_stream_keeps_half_its_text_and_ends_at_the_length_limit(
    stream, text
):
    cut = truncated(stream, anthropic=False)

    reply = reply_from_stream(cut)
    assert reply is not None
    assert reply["choices"][0]["message"] == {"role": "assistant", "content": text}
    assert reply["choices"][0]["finish_reason"] == "length"
    assert cut.endswith(b"data: [DONE]\n\n")


def test_a_stream_sent_in_one_piece_is_still_cut():
    stream = anthropic_stream(
        [({"type": "text", "text": ""}, [{"type": "text_delta", "text": "abcdef"}])],
        "end_turn",
    )

    reply = reply_from_stream(truncated(stream, anthropic=True))

    assert reply is not None
    assert reply["content"] == [{"type": "text", "text": "abc"}]
    assert reply["stop_reason"] == "max_tokens"


@pytest.mark.parametrize(
    ("stream", "text"),
    [(ANTHROPIC_TEXT_STREAM, "cart i"), (ANTHROPIC_TOOL_STREAM, "Checkin")],
)
def test_an_anthropic_stream_drops_its_tool_use_and_stops_at_max_tokens(stream, text):
    reply = reply_from_stream(truncated(stream, anthropic=True))

    assert reply is not None
    assert reply["content"] == [{"type": "text", "text": text}]
    assert reply["stop_reason"] == "max_tokens"
    assert reply["usage"]["output_tokens"] == 12  # the usage is as it was


def test_an_openai_stream_cut_before_its_finish_still_ends_at_the_limit():
    stream = OPENAI_TEXT_STREAM.split(b'data: {"id": "chatcmpl-1"')[:3]
    partial = b'data: {"id": "chatcmpl-1"'.join(stream)

    reply = reply_from_stream(truncated(partial, anthropic=False))

    assert reply is not None
    assert reply["choices"][0]["finish_reason"] == "length"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<html>busy</html>", "not JSON"),
        (b"[1, 2]", "not a JSON object"),
        (b'{"choices": []}', "no choices"),
        (b'{"output": "x"}', "unknown shape"),
        (UNKNOWN_STREAM, "no known shape"),
    ],
)
def test_a_reply_of_an_unknown_shape_is_left_unchanged(body, reason):
    with pytest.raises(Unchanged, match=reason):
        truncated(body, anthropic=False)
