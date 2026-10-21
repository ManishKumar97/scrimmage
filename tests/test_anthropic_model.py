import asyncio
from typing import cast

import anthropic
import httpx2
import pytest
from fake_tools import logs_registry
from pydantic import ValidationError

from faultline import ida
from faultline.models import (
    AnthropicModel,
    ModelError,
    Reply,
    ToolCall,
    ToolResult,
    make_model,
)
from faultline.settings import Settings


def message(
    content: list[dict], stop_reason: str, usage=(100, 20)
) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "fake-model",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
        }
    )


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def thinking(value: str) -> dict:
    return {"type": "thinking", "thinking": value, "signature": "sig"}


def tool_use(id: str, service: str) -> dict:
    return {
        "type": "tool_use",
        "id": id,
        "name": "get_logs",
        "input": {"service": service},
    }


class FakeAnthropic:
    """Implements only the part of AsyncAnthropic the adapter uses."""

    def __init__(self, *replies: anthropic.types.Message | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.messages = self

    async def create(self, **kwargs) -> anthropic.types.Message:
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def adapter(*replies) -> tuple[AnthropicModel, FakeAnthropic]:
    client = FakeAnthropic(*replies)
    return AnthropicModel(cast(anthropic.AsyncAnthropic, client), "fake"), client


def complete(model: AnthropicModel, messages, tools=None) -> Reply:
    return asyncio.run(model.complete("be brief", messages, tools or []))


def test_tool_schemas_are_sent_in_anthropic_form():
    model, client = adapter(message([text("hi")], "end_turn"))
    tools = logs_registry(str).schemas()

    complete(model, ["why?"], tools)

    request = client.requests[0]
    assert request["system"] == "be brief"
    assert request["max_tokens"] == 16_000
    assert "tool_choice" not in request
    assert request["messages"] == [{"role": "user", "content": "why?"}]
    [tool] = request["tools"]
    assert tool["name"] == "get_logs"
    assert tool["description"] == tools[0]["function"].get("description")
    assert tool["input_schema"] == tools[0]["function"].get("parameters")


def test_reply_is_normalized_and_tokens_are_input_plus_output():
    content = [thinking("hm"), text("checking"), tool_use("tu_1", "cart")]
    model, _ = adapter(message(content, "tool_use", usage=(300, 45)))

    reply = complete(model, ["why?"])

    assert reply.text == "checking"
    assert reply.tool_calls == [ToolCall("tu_1", "get_logs", '{"service": "cart"}')]
    assert reply.finish_reason == "tool_calls"
    assert reply.tokens == 345


def test_assistant_turn_is_replayed_with_thinking_blocks_unchanged():
    content = [thinking("hm"), tool_use("tu_1", "cart"), tool_use("tu_2", "redis")]
    model, client = adapter(
        message(content, "tool_use"), message([text("done")], "end_turn")
    )
    first = complete(model, ["why?"])

    complete(model, ["why?", first, [ToolResult("tu_1", "boom", False)]])

    sent = client.requests[1]["messages"][1]
    assert sent["role"] == "assistant"
    assert [b.model_dump() for b in sent["content"]] == [
        b.model_dump() for b in first.raw
    ]
    assert [b.type for b in sent["content"]] == ["thinking", "tool_use", "tool_use"]


def test_all_tool_results_go_back_in_one_user_message():
    model, client = adapter(message([text("ok")], "end_turn"))
    reply = Reply("", [], "tool_calls", 0, raw=[])
    results = [
        ToolResult("tu_1", "fine", False),
        ToolResult("tu_2", "Error: no such service", True),
    ]

    complete(model, ["why?", reply, results])

    assert client.requests[0]["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "fine"},
            {
                "type": "tool_result",
                "tool_use_id": "tu_2",
                "content": "Error: no such service",
                "is_error": True,
            },
        ],
    }


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "length"),
        ("refusal", "refusal"),
    ],
)
def test_stop_reasons_are_normalized(stop_reason, expected):
    model, _ = adapter(message([text("x")], stop_reason))

    assert complete(model, ["why?"]).finish_reason == expected


def test_a_paused_turn_is_a_model_error():
    model, _ = adapter(message([text("x")], "pause_turn"))

    with pytest.raises(ModelError):
        complete(model, ["why?"])


def test_sdk_errors_become_model_errors():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, request=request)
    error = anthropic.BadRequestError(
        "prompt is too long", response=response, body=None
    )
    model, _ = adapter(error)

    with pytest.raises(ModelError, match="prompt is too long"):
        complete(model, ["why?"])


def test_the_loop_runs_end_to_end_on_the_anthropic_adapter():
    model, client = adapter(
        message([thinking("hm"), tool_use("tu_1", "cart")], "tool_use"),
        message([text("cart is failing")], "end_turn"),
    )

    result = asyncio.run(
        ida.run("why?", model, tools=logs_registry(lambda service: f"{service} logs"))
    )

    assert result.stop_reason == "done"
    assert result.answer == "cart is failing"
    assert result.tokens == 240
    [tool_result] = client.requests[1]["messages"][2]["content"]
    assert tool_result["content"] == "cart logs"
    assert "is_error" not in tool_result


def test_a_refusal_stops_the_loop_as_refused():
    model, _ = adapter(message([text("I cannot help")], "refusal"))

    result = asyncio.run(ida.run("why?", model, tools=logs_registry(str)))

    assert result.stop_reason == "refused"
    assert result.answer == "I cannot help"


def test_the_provider_setting_picks_the_adapter_and_default_model():
    groq = Settings(provider="groq", groq_api_key="g", _env_file=None)  # pyright: ignore
    claude = Settings(provider="anthropic", anthropic_api_key="a", _env_file=None)  # pyright: ignore

    assert type(make_model(groq)).__name__ == "OpenAIModel"
    assert make_model(groq).model == "openai/gpt-oss-120b"  # pyright: ignore
    assert type(make_model(claude)).__name__ == "AnthropicModel"
    assert make_model(claude).model == "claude-haiku-5-5"  # pyright: ignore


def test_a_provider_without_its_key_is_rejected():
    with pytest.raises(ValidationError, match="PROVIDER=anthropic"):
        Settings(provider="anthropic", groq_api_key="g", _env_file=None)  # pyright: ignore
