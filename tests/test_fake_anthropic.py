"""The anthropic library accepts the fake's replies, so fake checks use the real path.

The library sends through httpx2, as the openai library does, so the gate that patches
httpx2's single attempt sees it."""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from fakes.anthropic_server import FIXTURES, FakeAnthropic

WEATHER: Any = {
    "name": "get_weather",
    "description": "The weather in a place.",
    "input_schema": {"type": "object", "properties": {"location": {"type": "string"}}},
}


def client_for(fake: FakeAnthropic) -> anthropic.Anthropic:
    # No retries: an error from the fake must reach the test, not be retried away.
    return anthropic.Anthropic(base_url=fake.base_url, api_key="test", max_retries=0)


def ask(
    client: anthropic.Anthropic, *messages: dict[str, Any], **extra: Any
) -> anthropic.types.Message:
    sent: Any = list(messages)
    return client.messages.create(model="claude-haiku-5-5", max_tokens=64, messages=sent, **extra)


def test_the_library_sends_through_httpx2() -> None:
    """The gate patches httpx and httpx2; the anthropic library is on the second."""
    client = anthropic.Anthropic(api_key="test")
    assert isinstance(client._client, httpx2.Client)  # pyright: ignore[reportPrivateUsage]


def test_a_plain_answer(fake_anthropic: Callable[..., FakeAnthropic]) -> None:
    fake = fake_anthropic(["real_answer"])
    reply = ask(client_for(fake), {"role": "user", "content": "Hello, Claude"})
    assert reply.stop_reason == "end_turn"
    assert [b.type for b in reply.content] == ["text"]
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (16, 17)
    assert fake.paths == ["/v1/messages"]


def test_a_tool_call_then_an_answer(fake_anthropic: Callable[..., FakeAnthropic]) -> None:
    fake = fake_anthropic(["real_tool_use", "real_answer"])
    client = client_for(fake)
    question = {"role": "user", "content": "What is the weather in San Francisco?"}
    first = ask(client, question, tools=[WEATHER])
    call = next(b for b in first.content if b.type == "tool_use")
    assert call.name == "get_weather"
    assert call.input == {"city": "Paris"}
    asked = {"role": "assistant", "content": [b.model_dump() for b in first.content]}
    result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": call.id, "content": "Sunny, 22 C"}],
    }
    second = ask(client, question, asked, result, tools=[WEATHER])
    assert second.stop_reason == "end_turn"
    assert [r["messages"][-1]["role"] for r in fake.requests] == ["user", "user"]


def test_a_stream_is_sent_as_server_sent_events(
    fake_anthropic: Callable[..., FakeAnthropic],
) -> None:
    fake = fake_anthropic(["real_stream_tool_use"])
    with client_for(fake).messages.stream(
        model="claude-haiku-5-5",
        max_tokens=64,
        messages=[{"role": "user", "content": "weather?"}],
        tools=[WEATHER],
    ) as stream:
        final = stream.get_final_message()
    assert final.stop_reason == "tool_use"
    call = next(b for b in final.content if b.type == "tool_use")
    assert call.input == {"city": "Paris"}
    assert (final.usage.input_tokens, final.usage.output_tokens) == (376, 50)


def test_a_saved_real_error_is_sent_with_its_status(
    fake_anthropic: Callable[..., FakeAnthropic],
) -> None:
    fake = fake_anthropic(["real_error_401"])
    with pytest.raises(anthropic.AuthenticationError) as caught:
        ask(client_for(fake), {"role": "user", "content": "hi"})
    assert caught.value.status_code == 401
    body: Any = caught.value.body
    assert body["error"]["type"] == "authentication_error"
    assert caught.value.request_id == "req_fake_scrimmage"


def test_a_failed_tool_result_gets_the_other_reply(
    fake_anthropic: Callable[..., FakeAnthropic],
) -> None:
    fake = fake_anthropic(["real_tool_use", "real_answer|doc_refusal"])
    client = client_for(fake)
    question = {"role": "user", "content": "hi"}
    first = ask(client, question, tools=[WEATHER])
    asked = {"role": "assistant", "content": [b.model_dump() for b in first.content]}
    failed = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_x",
                "content": "Error: down",
                "is_error": True,
            }
        ],
    }
    second = ask(client, question, asked, failed, tools=[WEATHER])
    assert second.stop_reason == "refusal"


def test_count_tokens_has_its_own_fixture(fake_anthropic: Callable[..., FakeAnthropic]) -> None:
    fake = fake_anthropic([], count_tokens="real_count_tokens")
    counted = client_for(fake).messages.count_tokens(
        model="claude-haiku-5-5", messages=[{"role": "user", "content": "Hello, Claude"}]
    )
    assert counted.input_tokens == 15
    assert fake.paths == ["/v1/messages/count_tokens"]


def test_by_model_answers_by_what_the_request_holds() -> None:
    from fakes.anthropic_server import Reply

    fake = FakeAnthropic([], by_model={"claude-guard": lambda request: Reply("real_answer")})
    fake.start()
    try:
        reply = client_for(fake).messages.create(
            model="claude-guard", max_tokens=8, messages=[{"role": "user", "content": "x"}]
        )
    finally:
        fake.stop()
    assert reply.content[0].type == "text"
    assert fake.errors == []


def test_an_unknown_path_and_a_turn_past_the_list_are_errors_that_are_kept() -> None:
    fake = FakeAnthropic(["real_answer"])
    fake.start()
    try:
        client = client_for(fake)
        with pytest.raises(anthropic.InternalServerError):
            client.post("/v1/other", cast_to=object, body={})
        ask(client, {"role": "user", "content": "a"})
        with pytest.raises(anthropic.InternalServerError):
            ask(
                client,
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
            )
    finally:
        fake.stop()
    assert fake.errors == ["no fixture serves /v1/other", "no reply for turn 2"]
    assert json.loads(fake.received[0][1]) == {}


def test_the_client_over_a_mock_transport_reads_a_documented_reply() -> None:
    """model_gate.anthropic_client is the library over a mock transport, for tests of the gate."""
    from model_gate import Provider, anthropic_client

    reply = (FIXTURES / "real_answer.json").read_bytes()
    seen: list[str] = []
    provider = Provider(seen, lambda request: httpx2.Response(200, content=reply))
    answer = ask(anthropic_client(provider), {"role": "user", "content": "Hello, Claude"})
    assert answer.content[0].type == "text"
    assert seen == ["sent"]
    assert json.loads(provider.sent[0])["model"] == "claude-haiku-5-5"
