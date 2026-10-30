import asyncio
from typing import Any, cast

import pytest
from fake_model import FakeClient, text_reply
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from faultline_lab.models import ModelError, OpenAIModel, Reply, ToolSpec


def complete(reply: ChatCompletion, tools: list[ToolSpec] | None = None):
    client = FakeClient(reply)
    model = OpenAIModel(cast(AsyncOpenAI, client), "fake")
    result = asyncio.run(model.complete("be brief", ["why?"], tools or []))
    return result, client


def test_tool_specs_are_sent_in_openai_form():
    spec = ToolSpec("get_logs", "Read logs.", {"type": "object"})

    _, client = complete(text_reply("ok"), [spec])

    assert client.requests[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_logs",
                "description": "Read logs.",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_a_response_without_choices_is_a_model_error():
    reply = text_reply("ok")
    reply.choices = []

    with pytest.raises(ModelError, match="no choices"):
        complete(reply)


def test_a_response_without_usage_is_a_model_error_not_zero_tokens():
    reply = text_reply("ok")
    reply.usage = None

    with pytest.raises(ModelError, match="no token usage"):
        complete(reply)


def test_an_unknown_finish_reason_is_a_model_error():
    reply = text_reply("ok")
    cast(Any, reply.choices[0]).finish_reason = "something_new"

    with pytest.raises(ModelError, match="unknown finish reason 'something_new'"):
        complete(reply)


@pytest.mark.parametrize(
    ("sent", "seen"),
    [
        ("stop", "stop"),
        ("tool_calls", "tool_calls"),
        ("function_call", "tool_calls"),
        ("length", "length"),
        ("content_filter", "content_filter"),
    ],
)
def test_known_finish_reasons_map_to_the_loops_names(sent: str, seen: str):
    result, _ = complete(text_reply("ok", finish_reason=sent))

    assert isinstance(result, Reply)
    assert result.finish_reason == seen
