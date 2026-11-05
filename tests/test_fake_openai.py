"""The openai library accepts the fake's replies, so fake checks use the real path."""

import json
from collections.abc import Callable

import openai
import pytest

from fakes.openai_server import FakeOpenAI

WEATHER_TOOL: openai.types.chat.ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "get_current_weather",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
        },
    },
}


def client_for(fake: FakeOpenAI) -> openai.OpenAI:
    # No retries: an error from the fake must reach the test, not be retried away.
    return openai.OpenAI(base_url=fake.base_url, api_key="test", max_retries=0)


def test_chat_with_a_tool_call_then_an_answer(
    fake_openai: Callable[[list[str]], FakeOpenAI],
) -> None:
    fake = fake_openai(["chat_tool_call", "chat_answer"])
    client = client_for(fake)
    question = {"role": "user", "content": "What's the weather like in Boston today?"}

    first = client.chat.completions.create(
        model="gpt-4o-mini", messages=[question], tools=[WEATHER_TOOL]
    )
    call = first.choices[0].message.tool_calls
    assert first.choices[0].finish_reason == "tool_calls"
    assert call is not None and call[0].type == "function"
    assert call[0].function.name == "get_current_weather"
    assert json.loads(call[0].function.arguments) == {"location": "Boston, MA"}

    second = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            question,
            first.choices[0].message.model_dump(exclude_none=True),  # type: ignore[arg-type]
            {"role": "tool", "tool_call_id": call[0].id, "content": "Sunny, 22 C"},
        ],
        tools=[WEATHER_TOOL],
    )
    assert second.choices[0].finish_reason == "stop"
    assert second.choices[0].message.content == "Hello! How can I assist you today?"
    assert [r["messages"][-1]["role"] for r in fake.requests] == ["user", "tool"]


def test_a_request_with_no_fixture_left_is_an_error() -> None:
    fake = FakeOpenAI([])
    fake.start()
    try:
        with pytest.raises(openai.InternalServerError):
            client_for(fake).chat.completions.create(model="gpt-4o-mini", messages=[])
    finally:
        fake.stop()
    assert fake.errors == ["no reply left for request 1"]


def test_an_unknown_path_is_an_error() -> None:
    fake = FakeOpenAI(["chat_answer"])
    fake.start()
    try:
        with pytest.raises(openai.InternalServerError):
            client_for(fake).embeddings.create(
                model="text-embedding-3-small", input="hi"
            )
    finally:
        fake.stop()
    assert fake.errors == ["no fixture serves /v1/embeddings"]
    assert fake.unused == ["chat_answer"]
