"""The faults in an Anthropic reply: replace, garble, truncate and refuse, through the real
anthropic library and the real gate.

Every test name holds "reply", so `pytest tests/test_anthropic_faults.py -k reply` runs them
all. The replies are the real captures in tests/fixtures/anthropic, laid out again with more
blocks where a test needs them: a block keeps the bytes it was captured with."""

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from model_gate import GUARD, Gate, Provider, anthropic_client, guard
from scrimmage import changes, provider_anthropic, state
from scrimmage.config import Fault
from scrimmage.errors import ScrimmageMockError
from scrimmage.prices import Rates

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"
MODEL = "claude-test"
JSON = {"content-type": "application/json"}
SSE = {"content-type": "text/event-stream"}


@pytest.fixture(autouse=True)
def gated() -> Any:
    yield
    state.install(None)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def laid_out(value: object) -> bytes:
    """JSON as Anthropic writes it: compact, with the text as it is."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


REAL_ANSWER = json.loads(fixture("real_answer.json"))
REAL_DECLINE = json.loads(fixture("real_decline.json"))
REAL_TOOL = json.loads(fixture("real_tool_use.json"))
THINKING = REAL_DECLINE["content"][0]  # a real signed block, its signature scrubbed
REDACTED = {"type": "redacted_thinking", "data": "[scrubbed]"}
FIRST = "The order shipped on Tuesday, and it will arrive on Friday."
SECOND = "Tell me if you want the tracking number as well."
CALL = REAL_TOOL["content"][0]


def reply_of(*blocks: dict[str, Any], stop: str = "end_turn") -> bytes:
    """A real reply's fields with these content blocks."""
    return laid_out({**REAL_ANSWER, "content": list(blocks), "stop_reason": stop})


def text(words: str) -> dict[str, str]:
    return {"type": "text", "text": words}


# Thinking, then two text blocks, then a tool call: every block kind a change meets.
MIXED = reply_of(THINKING, REDACTED, text(FIRST), text(SECOND), CALL, stop="tool_use")


def new_gate(word: str, **kw: Any) -> Gate:
    return Gate(Fault("model", word, "always", **kw), hosts=(), prices=((MODEL, Rates(3.0, 15.0)),))


def asked(client: anthropic.Anthropic, **kw: Any) -> Any:
    return client.messages.with_raw_response.create(
        model=MODEL, max_tokens=64, messages=[{"role": "user", "content": "hi"}], **kw
    )


def faulted(word: str, body: bytes, **kw: Any) -> tuple[Gate, Provider, bytes, Any]:
    """One call through the gate with this fault: the gate, the provider, the bytes the agent
    got, and what the library made of them."""
    run = new_gate(word, **kw)
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=body, headers=JSON))
    raw = asked(anthropic_client(provider))
    return run, provider, raw.http_response.content, raw.parse()


def swapped(body: bytes, old: str, new: str) -> bytes:
    """The body with one text's JSON string replaced by another's, and nothing else touched."""
    before, after = laid_out(old), laid_out(new)
    assert body.count(before) == 1, "the text is in the reply once"
    return body.replace(before, after)


def blocks_of(body: bytes) -> list[dict[str, Any]]:
    return json.loads(body)["content"]


def test_a_replace_reply_changes_every_text_block_and_keeps_every_other_byte() -> None:
    run, provider, got, message = faulted("replace", MIXED, text="NEW")
    expected = swapped(swapped(MIXED, FIRST, "NEW"), SECOND, "NEW")
    assert got == expected, "only the two text strings changed"
    assert [b.type for b in message.content] == [
        "thinking",
        "redacted_thinking",
        "text",
        "text",
        "tool_use",
    ]
    assert message.content[2].text == message.content[3].text == "NEW"
    assert message.usage.input_tokens == REAL_ANSWER["usage"]["input_tokens"]
    assert message.id == REAL_ANSWER["id"] and message.model == REAL_ANSWER["model"]
    assert len(provider.sent) == 1, "the call went to the provider: replace changes the real reply"
    assert run.line["status"] == 200 and "unchanged" not in run.line
    assert run.line["changed"]["count"] == 2


def test_a_replace_reply_with_find_changes_that_text_inside_each_block() -> None:
    _, _, got, _ = faulted("replace", MIXED, text="Monday", find="Tuesday")
    assert got == swapped(MIXED, FIRST, FIRST.replace("Tuesday", "Monday"))


def test_a_garble_reply_puts_the_stray_token_in_every_text_block_and_keeps_every_other_byte() -> (
    None
):
    _, _, got, message = faulted("garble", MIXED)
    expected = swapped(
        swapped(MIXED, FIRST, changes.garbled(FIRST)[0]), SECOND, changes.garbled(SECOND)[0]
    )
    assert got == expected
    assert changes.GARBLE_TOKEN in message.content[2].text
    assert changes.GARBLE_TOKEN in message.content[3].text
    assert message.usage.output_tokens == REAL_ANSWER["usage"]["output_tokens"]


@pytest.mark.parametrize("word", ["replace", "garble", "truncate"])
def test_a_changed_reply_leaves_thinking_and_redacted_thinking_blocks_as_they_are(
    word: str,
) -> None:
    _, _, got, _ = faulted(word, MIXED, text="NEW")
    assert blocks_of(got)[:2] == [THINKING, REDACTED]
    assert got.startswith(MIXED.split(b'"content":')[0]), "the fields before content are as sent"


@pytest.mark.parametrize("word", ["replace", "garble"])
def test_a_reply_with_no_text_block_is_unchanged_and_the_line_says_so(word: str) -> None:
    body = fixture("real_tool_use.json")
    run, _, got, _ = faulted(word, body, text="NEW")
    assert got == body
    assert run.line["unchanged"]["why"] == "the reply has only tool calls and no text"
    assert run.line["unchanged"]["do"] == word
    assert "mistake" not in run.line, "a reply that holds no text is the baseline's, not a mistake"


def test_a_reply_of_thinking_alone_is_unchanged_by_every_change_and_says_so() -> None:
    body = reply_of(THINKING)
    for word, why in (
        ("replace", "the reply has no text"),
        ("garble", "the reply has no text"),
        ("truncate", "the reply has no text and no tool call"),
    ):
        run, _, got, _ = faulted(word, body, text="NEW")
        assert got == body, word
        assert run.line["unchanged"]["why"] == why, word


def test_a_truncate_reply_cuts_the_first_text_block_at_its_middle_and_drops_the_rest() -> None:
    run, _, got, message = faulted("truncate", MIXED)
    cut = changes.cut(FIRST)
    assert cut == "The order shipped on Tuesday," and FIRST.startswith(cut)
    assert blocks_of(got) == [THINKING, REDACTED, text(cut)], "later text and the tool call go"
    assert message.stop_reason == "max_tokens"
    assert message.usage.output_tokens == REAL_ANSWER["usage"]["output_tokens"], "as billed"
    assert message.id == REAL_ANSWER["id"] and message.model == REAL_ANSWER["model"]
    expected = swapped(MIXED, FIRST, cut)
    assert got.startswith(expected.split(b'"content":')[0]), "the fields before content are as sent"
    assert json.loads(got)["usage"] == REAL_ANSWER["usage"]
    assert run.line["changed"]["parts"] == ["content[2].text"]


def test_a_truncate_reply_sets_stop_reason_to_max_tokens_whatever_it_was() -> None:
    real = json.loads(MIXED)
    assert real["stop_reason"] == "tool_use"
    _, _, got, _ = faulted("truncate", MIXED)
    assert json.loads(got)["stop_reason"] == "max_tokens"


def test_a_truncate_reply_of_a_tool_call_alone_is_the_form_a_real_cut_has() -> None:
    """A real reply cut inside a tool call keeps the block with an empty input
    (real_tool_use_cut.json), so that is the form truncate builds: nothing is made up."""
    real_cut = json.loads(fixture("real_tool_use_cut.json"))
    body = fixture("real_tool_use.json")
    _, _, got, message = faulted("truncate", body)
    made = json.loads(got)
    assert made["content"] == [{**CALL, "input": {}}]
    assert list(made["content"][0]) == list(real_cut["content"][0]), "the same keys, in order"
    assert made["content"][0]["input"] == real_cut["content"][0]["input"] == {}
    assert made["stop_reason"] == real_cut["stop_reason"] == "max_tokens"
    assert list(made) == list(real_cut), "the same fields of the reply, in order"
    assert made["usage"] == REAL_TOOL["usage"], "as billed"
    assert message.stop_reason == "max_tokens"


def test_a_truncate_reply_cuts_the_first_tool_call_when_there_is_no_text() -> None:
    second = {**CALL, "id": "toolu_second", "input": {"city": "Rome"}}
    body = reply_of(THINKING, CALL, second, stop="tool_use")
    _, _, got, _ = faulted("truncate", body)
    assert blocks_of(got) == [THINKING, {**CALL, "input": {}}]


def test_a_changed_reply_to_a_streamed_request_is_left_as_it_is_with_the_mistake_on_the_line() -> (
    None
):
    """The change needs a reply that is not streamed. A replace is carried: the real call runs and
    its stream reaches the agent whole, and the line records the mistake in the test. This is
    what OpenAI's replace does on a stream."""
    run = new_gate("replace", text="NEW")
    stream = fixture("real_stream_answer.sse")
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=stream, headers=SSE))
    client = anthropic_client(provider)
    with client.messages.stream(
        model=MODEL, max_tokens=64, messages=[{"role": "user", "content": "hi"}]
    ) as events:
        said = "".join(events.text_stream)
    assert said and "NEW" not in said, "the agent got the real stream"
    assert run.line["status"] == 200 and run.line["action"] == "real"
    assert "needs a reply that is not streamed" in run.line["mistake"]
    assert "not yet" not in json.dumps(run.line)


def test_a_refuse_reply_is_a_plain_decline_by_default_at_cost_zero_and_sends_nothing() -> None:
    run = new_gate("refuse")
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=b"{}"))
    raw = asked(anthropic_client(provider))
    message, got = raw.parse(), raw.http_response.content
    assert provider.sent == [], "nothing was sent"
    assert message.stop_reason == "end_turn"
    [block] = message.content
    assert block.type == "text" and block.text == provider_anthropic.PLAIN_REFUSAL
    real = json.loads(fixture("real_decline_address.json"))
    assert real["content"][0]["text"].startswith(block.text), "a real Claude decline's own words"
    assert real["stop_reason"] == "end_turn"
    made = json.loads(got)
    assert list(made) == list(real), "a real reply's fields, in its order"
    assert list(made["usage"]) == list(real["usage"])
    assert made["stop_details"] is None and made["model"] == MODEL
    assert made["id"].startswith("msg_") and len(made["id"]) == len(real["id"])
    assert made["usage"]["input_tokens"] > 0 and made["usage"]["output_tokens"] > 0
    line = run.line
    assert line["status"] == 200 and line["action"] == "fault"
    assert "cost_usd" not in line, "a built reply costs nothing"
    assert run.budget.reserved == [] and run.sink.settles == [], "nothing was held or settled"


def test_a_refuse_reply_of_the_classifier_form_is_stop_reason_refusal_with_stop_details() -> None:
    run = new_gate("refuse", form="classifier")
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=b"{}"))
    raw = asked(anthropic_client(provider))
    message, made = raw.parse(), json.loads(raw.http_response.content)
    assert provider.sent == [], "nothing was sent"
    docs = json.loads(fixture("doc_refusal.json"))
    assert message.stop_reason == "refusal"
    assert made["content"] == docs["content"] == []
    assert made["stop_details"] == {"type": "refusal", "category": None, "explanation": None}
    assert list(made["stop_details"]) == list(docs["stop_details"]), "the Refusals page's keys"
    assert message.stop_details is not None and message.stop_details.category is None
    assert made["usage"]["output_tokens"] == 0 == docs["usage"]["output_tokens"]
    assert run.budget.reserved == [] and run.sink.settles == []


def test_a_refuse_reply_of_the_plain_form_written_out_is_the_default() -> None:
    plain = json.loads(faulted("refuse", b"{}", form="plain")[2])
    default = json.loads(faulted("refuse", b"{}")[2])
    assert {**plain, "id": ""} == {**default, "id": ""}


def test_a_refuse_reply_form_on_an_openai_call_is_a_mistake_at_the_call() -> None:
    run = Gate(Fault("model", "refuse", "always", model=GUARD, form="classifier"))
    provider = Provider(run.events)
    client = guard(provider)
    with pytest.raises(ScrimmageMockError, match="is an OpenAI call, and refuse has one form"):
        client.chat.completions.create(model=GUARD, messages=[{"role": "user", "content": "hi"}])
    assert provider.sent == [], "nothing was sent"
    assert run.line["reason"] == "mistake"


def test_a_refuse_reply_without_a_form_on_an_openai_call_is_built_as_before() -> None:
    run = Gate(Fault("model", "refuse", "always", model=GUARD))
    reply = guard(Provider(run.events)).chat.completions.create(
        model=GUARD, messages=[{"role": "user", "content": "hi"}]
    )
    assert reply.choices[0].finish_reason == "stop"


def test_a_form_on_an_anthropic_refuse_reply_that_is_not_a_form_is_a_mistake_at_the_call() -> None:
    run = new_gate("refuse", form="loud")
    provider = Provider(run.events)
    with pytest.raises(anthropic.APIConnectionError) as caught:
        asked(anthropic_client(provider))
    assert 'refuse has the forms "plain" and "classifier"' in str(caught.value.__cause__)
    assert provider.sent == []


# What a later request carries of a faulted reply.
ERROR_400 = laid_out(
    {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "messages: bad"},
        "request_id": "req_1",
    }
)


def two_calls(word: str, first: bytes, second: Any, **kw: Any) -> tuple[Gate, int]:
    """A faulted first call, then the agent's second request, which the provider answers with its
    own 400. Returns the gate and the number of requests the provider got."""
    run = Gate(Fault("model", word, "first 1", **kw), hosts=(), prices=((MODEL, Rates(3.0, 15.0)),))
    replies = [httpx2.Response(200, content=first, headers=JSON)]
    replies.append(httpx2.Response(400, content=ERROR_400, headers=JSON))
    provider = Provider(run.events, lambda r: replies[len(provider.sent) - 1])
    client = anthropic_client(provider)
    asked(client)
    with pytest.raises(anthropic.BadRequestError):
        client.messages.create(model=MODEL, max_tokens=64, messages=second)
    return run, len(provider.sent)


def model_lines(run: Gate) -> list[dict[str, Any]]:
    return [x for x in run.sink.lines if x.get("kind") == "model"]


def result_of(id_: str) -> list[dict[str, Any]]:
    """The turn that answers a tool call by its id, with no earlier history."""
    block = {"type": "tool_result", "tool_use_id": id_, "content": "ok"}
    return [{"role": "user", "content": [block]}]


def test_a_400_to_a_request_that_carries_a_faulted_reply_toolu_id_is_marked() -> None:
    """Only the id ties this request to the faulted reply: it holds none of its history."""
    run, sent = two_calls("truncate", fixture("real_tool_use.json"), result_of(CALL["id"]))
    first, second = model_lines(run)
    assert sent == 2
    assert (first["action"], first["status"]) == ("real", 200) or first["status"] == 200
    assert "after_reply_fault" not in first
    assert (second["action"], second["status"]) == ("real", 400)
    assert second["after_reply_fault"] is True


def test_a_400_to_a_request_that_echoes_a_tool_use_block_of_a_faulted_reply_is_marked() -> None:
    echoed = [
        {"role": "assistant", "content": [{**CALL, "input": {}}]},
        {"role": "user", "content": "go on"},
    ]
    run, _ = two_calls("truncate", fixture("real_tool_use.json"), echoed)
    assert model_lines(run)[1]["after_reply_fault"] is True


def test_a_400_to_a_request_that_carries_the_changed_text_of_a_reply_is_marked() -> None:
    """A garbled text has no toolu_ id. Sent back as the agent's own assistant turn after the same
    history, it is what ties the next request to the faulted reply."""
    history = [{"role": "user", "content": "hi"}]
    garbled = changes.garbled(REAL_ANSWER["content"][0]["text"])[0]
    for content in ([{"type": "text", "text": garbled, "citations": None}], garbled):
        turn = [
            *history,
            {"role": "assistant", "content": content},
            {"role": "user", "content": "go"},
        ]
        run, _ = two_calls("garble", fixture("real_answer.json"), turn)
        assert model_lines(run)[1]["after_reply_fault"] is True, content
    # The same turn with the real text, which the agent never got, is not the faulted reply's.
    real = REAL_ANSWER["content"][0]["text"]
    turn = [*history, {"role": "assistant", "content": real}, {"role": "user", "content": "go"}]
    run, _ = two_calls("garble", fixture("real_answer.json"), turn)
    assert "after_reply_fault" not in model_lines(run)[1]


def test_a_400_to_a_request_with_none_of_a_faulted_reply_is_not_marked() -> None:
    unrelated = [{"role": "user", "content": "something else entirely"}]
    run, _ = two_calls("garble", fixture("real_answer.json"), unrelated)
    assert "after_reply_fault" not in model_lines(run)[1]
    run, _ = two_calls("truncate", fixture("real_tool_use.json"), result_of("toolu_other"))
    assert "after_reply_fault" not in model_lines(run)[1]


def test_what_a_faulted_reply_gave_is_its_toolu_ids_and_the_message_it_sent() -> None:
    from scrimmage.providers.impl.wire import wrap_fields

    wire = wrap_fields({"messages": [{"role": "user", "content": "hi"}]})
    gave = provider_anthropic.given(wire, fixture("real_tool_use.json"), ids_only=False)
    assert gave.ids == {CALL["id"]} and len(gave.prefixes) == 1
    only = provider_anthropic.given(wire, fixture("real_tool_use.json"), ids_only=True)
    assert only.ids == {CALL["id"]} and not only.prefixes
    nothing = provider_anthropic.given(wire, b"not a reply", ids_only=False)
    assert not (nothing.ids or nothing.prefixes)
    assert not provider_anthropic.carries(wire, [])


def test_the_thinking_check_behind_leaving_thinking_blocks_alone_is_the_saved_capture() -> None:
    """A real 200 came back to a request whose text block was changed and whose signed thinking
    block was sent as it came: the claim the docs make for leaving those blocks alone."""
    first = json.loads(fixture("real_thinking_reply.json"))
    sent = json.loads(fixture("real_thinking_resent_request.json"))
    again = json.loads(fixture("real_thinking_resent_reply.json"))
    [thinking, said] = first["content"]
    [_, turn, _] = sent["messages"]
    assert turn["content"][0] == thinking and thinking["type"] == "thinking"
    assert turn["content"][1]["text"] != said["text"]
    assert changes.GARBLE_TOKEN in turn["content"][1]["text"]
    assert again["type"] == "message" and again["stop_reason"] == "end_turn"
