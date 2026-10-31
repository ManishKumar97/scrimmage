"""Streamed replies as OpenAI and Anthropic send them: server-sent events."""

import json


def sse(events: list[tuple[str | None, dict | str]]) -> bytes:
    """Server-sent events. A str payload is sent as is, such as [DONE]."""
    out = []
    for name, data in events:
        text = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {name}\n" if name else "") + f"data: {text}\n\n")
    return "".join(out).encode()


def openai_chunk(delta: dict, finish: str | None = None, usage: dict | None = None):
    choices = [{"index": 0, "delta": delta, "finish_reason": finish}]
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "tiny-model",
        "choices": [] if usage else choices,
    }
    if usage:
        chunk["usage"] = usage
    return (None, chunk)


OPENAI_TOOL_STREAM = sse(
    [
        openai_chunk({"role": "assistant", "content": None}),
        openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_logs", "arguments": ""},
                    }
                ]
            }
        ),
        openai_chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": '{"serv'}}]}
        ),
        openai_chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": 'ice": "cart"}'}}]}
        ),
        openai_chunk({}, "tool_calls"),
        openai_chunk(
            {}, usage={"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57}
        ),
        (None, "[DONE]"),
    ]
)
OPENAI_TEXT_STREAM = sse(
    [
        openai_chunk({"role": "assistant", "content": ""}),
        openai_chunk({"content": "cart is "}),
        openai_chunk({"content": "down"}),
        openai_chunk({}, "stop"),
        (None, "[DONE]"),
    ]
)


def anthropic_stream(blocks: list[tuple[dict, list[dict]]], stop: str) -> bytes:
    """A Messages stream: each block is its start and its deltas."""
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 90,
            "output_tokens": 1,
        },
    }
    events: list[tuple[str | None, dict | str]] = [
        ("message_start", {"type": "message_start", "message": message})
    ]
    for index, (start, deltas) in enumerate(blocks):
        events.append(
            (
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": start},
            )
        )
        events += [
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": delta},
            )
            for delta in deltas
        ]
        events.append(
            ("content_block_stop", {"type": "content_block_stop", "index": index})
        )
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 12},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return sse(events)


ANTHROPIC_TOOL_STREAM = anthropic_stream(
    [
        (
            {"type": "text", "text": ""},
            [
                {"type": "text_delta", "text": "Checking "},
                {"type": "text_delta", "text": "logs."},
            ],
        ),
        (
            {"type": "tool_use", "id": "toolu_1", "name": "get_logs", "input": {}},
            [
                {"type": "input_json_delta", "partial_json": '{"service"'},
                {"type": "input_json_delta", "partial_json": ': "cart"}'},
            ],
        ),
    ],
    "tool_use",
)
ANTHROPIC_TEXT_STREAM = anthropic_stream(
    [({"type": "text", "text": ""}, [{"type": "text_delta", "text": "cart is down"}])],
    "end_turn",
)
# Events, but in no shape the recorder knows.
UNKNOWN_STREAM = sse([("progress", {"percent": 50}), ("done", {"ok": True})])
