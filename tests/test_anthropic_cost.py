"""What an Anthropic call costs: the usage a reply says, summed as Anthropic means it, settled at
the rate of each kind of token, and held before the call from what the request asks for.

Every reply goes through the real gate and the real anthropic library over a mock transport.
The hand sums are written out here, from the rates and the counts of the documented fixtures,
not read back from the code."""

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from model_gate import Gate, Provider, anthropic_client
from scrimmage import provider_anthropic
from scrimmage.config import Fault
from scrimmage.prices import Rates
from scrimmage.providers.registry import built_in

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"
MODEL = "claude-test"
FAST = Rates(6.0, 30.0, 0.6, cache_write_5m=7.5, cache_write_1h=12.0)
RATES = Rates(
    3.0, 15.0, 0.3, cache_write_5m=3.75, cache_write_1h=6.0, tiers=(("fast", FAST),)
)  # dollars per million tokens
STREAM = {"content-type": "text/event-stream"}
ORDER: dict[str, Any] = {"name": "get_weather", "input_schema": {"type": "object"}}
ASK: dict[str, Any] = {"model": MODEL, "max_tokens": 100}
TOLERANCE = 1e-8


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def gate(*faults: Fault, **settings: Any) -> Gate:
    """A run that sends to api.anthropic.com, a host that is Anthropic's with no model_hosts."""
    return Gate(*faults, hosts=(), prices=((MODEL, RATES),), **settings)


def send(
    run: Gate, reply: bytes, *, stream: bool = False, **request: Any
) -> tuple[Provider, list[Any]]:
    """One call through the anthropic library to a transport that answers with this reply."""
    headers = STREAM if stream else {"content-type": "application/json"}
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=reply, headers=headers))
    client = anthropic_client(provider)
    asked: Any = {**ASK, "messages": [{"role": "user", "content": "hi"}], **request}
    seen: list[Any] = []
    if stream:
        for event in client.messages.create(stream=True, **asked):
            seen.append(event)
    else:
        seen.append(client.messages.create(**asked))
    return provider, seen


def settled(run: Gate) -> float:
    (_, usd), *_ = run.sink.settles
    return usd


def test_a_plain_reply_settles_to_the_hand_sum() -> None:
    run = gate()
    send(run, fixture("real_answer.json"))
    assert settled(run) == pytest.approx((16 * 3.0 + 17 * 15.0) / 1e6, abs=TOLERANCE)
    assert run.line["cost_usd"] == pytest.approx(settled(run), abs=TOLERANCE)
    assert run.line["cost_estimated"] is False
    assert run.line["tokens"] == {"input": 16, "output": 17, "cached": 0}
    assert run.line["call"] == "model:anthropic"


@pytest.mark.parametrize(
    ("name", "tokens_in", "tokens_out"),
    [("real_stream_answer.sse", 16, 14), ("real_stream_tool_use.sse", 376, 50)],
)
def test_a_streamed_reply_settles_to_the_hand_sum(
    name: str, tokens_in: int, tokens_out: int
) -> None:
    """The input is in message_start and the output, cumulative, in message_delta."""
    run = gate()
    send(run, fixture(name), stream=True)
    assert settled(run) == pytest.approx((tokens_in * 3.0 + tokens_out * 15.0) / 1e6, abs=TOLERANCE)
    assert run.line["cost_estimated"] is False
    assert run.line["tokens"]["input"] == tokens_in


# 2048 input tokens at the input rate, 1800 read, 148 written for 5 minutes, 100 for 1 hour, and
# 503 output: the reply's input_tokens, 2048, leaves out the cache, so the total input is 4096.
CACHED = (2048 * 3.0 + 1800 * 0.3 + 148 * 3.75 + 100 * 6.0 + 503 * 15.0) / 1e6


@pytest.mark.parametrize(
    ("name", "stream"), [("doc_cached.json", False), ("doc_stream_cached.sse", True)]
)
def test_a_cached_reply_settles_each_kind_of_token_at_its_own_rate(name: str, stream: bool) -> None:
    run = gate()
    send(run, fixture(name), stream=stream)
    assert settled(run) == pytest.approx(CACHED, abs=TOLERANCE)
    assert run.line["tokens"] == {"input": 4096, "output": 503, "cached": 1800}
    assert run.line["cost_estimated"] is False
    reading_input_alone = (2048 * 3.0 + 503 * 15.0) / 1e6
    assert settled(run) > reading_input_alone + 1e-3, "input_tokens alone would settle low"


def test_a_one_hour_write_costs_the_one_hour_rate_not_the_five_minute_one() -> None:
    reply = json.loads(fixture("doc_cached.json"))
    reply["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 248,
    }
    run = gate()
    send(run, json.dumps(reply).encode())
    assert settled(run) == pytest.approx(
        (2048 * 3.0 + 1800 * 0.3 + 248 * 6.0 + 503 * 15.0) / 1e6, abs=TOLERANCE
    )


def test_a_total_with_no_split_is_read_as_one_hour_writes_which_cost_most() -> None:
    reply = json.loads(fixture("doc_cached.json"))
    del reply["usage"]["cache_creation"]
    run = gate()
    send(run, json.dumps(reply).encode())
    assert settled(run) == pytest.approx(
        (2048 * 3.0 + 1800 * 0.3 + 248 * 6.0 + 503 * 15.0) / 1e6, abs=TOLERANCE
    )


def test_a_split_that_does_not_add_up_is_read_as_one_hour_writes() -> None:
    reply = json.loads(fixture("doc_cached.json"))
    reply["usage"]["cache_creation"]["ephemeral_5m_input_tokens"] = 10
    run = gate()
    send(run, json.dumps(reply).encode())
    assert settled(run) == pytest.approx(
        (2048 * 3.0 + 1800 * 0.3 + 248 * 6.0 + 503 * 15.0) / 1e6, abs=TOLERANCE
    )


def test_a_stream_cut_before_its_message_delta_is_estimated_not_counted() -> None:
    """No final output count has arrived, so the call settles at the most it could cost."""
    head = fixture("doc_stream_cached.sse").split(b"event: message_delta")[0]
    run = gate()
    send(run, head, stream=True)
    assert run.line["cost_estimated"] is True
    assert settled(run) > 0
    used = provider_anthropic.usage(head)
    assert not used.known


def test_a_stream_is_never_known_from_its_message_start_alone() -> None:
    start = fixture("real_stream_answer.sse").split(b"event: content_block_start")[0]
    assert not provider_anthropic.usage(start).known
    assert provider_anthropic.usage(fixture("real_stream_answer.sse")).known


def test_a_later_count_that_is_not_null_wins_and_a_null_leaves_the_earlier_one() -> None:
    used = provider_anthropic.usage(fixture("doc_stream_cached.sse"))
    assert (used.input, used.output, used.cached) == (4096, 503, 1800)
    assert used.writes == {"cache_write_5m": 148, "cache_write_1h": 100}


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": "5", "output_tokens": 1},
        {"input_tokens": 5, "output_tokens": 1.5},
        {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": -3},
        {"input_tokens": 5},
        {"output_tokens": 5},
        {},
    ],
)
def test_a_count_that_is_not_a_whole_number_is_never_trusted(usage: dict[str, object]) -> None:
    body = json.dumps({"type": "message", "usage": usage}).encode()
    assert not provider_anthropic.usage(body).known


def test_a_token_count_has_no_usage_of_its_own_and_costs_nothing() -> None:
    run = gate()
    provider = Provider(
        run.events, lambda r: httpx2.Response(200, content=fixture("real_count_tokens.json"))
    )
    anthropic_client(provider).messages.count_tokens(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    assert run.line["call"] == "model:anthropic/input_tokens"
    assert run.line["cost_usd"] == 0.0
    assert run.line["tokens"]["input"] == 15
    assert "reserve" not in run.events, "a free op reserves nothing"


def test_a_fast_request_is_held_and_settled_at_the_fast_row() -> None:
    run = gate()
    send(run, fixture("doc_cached.json"), speed="fast")
    fast_cost = (2048 * 6.0 + 1800 * 0.6 + 148 * 7.5 + 100 * 12.0 + 503 * 30.0) / 1e6
    assert settled(run) == pytest.approx(fast_cost, abs=TOLERANCE)
    assert run.budget.reserved[0] > 100 * 30.0 / 1e6, "the hold uses the fast output rate"


def test_a_reply_that_says_it_was_fast_settles_at_the_fast_row_whatever_was_asked() -> None:
    reply = json.loads(fixture("doc_cached.json"))
    reply["usage"]["speed"] = "fast"
    run = gate()
    send(run, json.dumps(reply).encode())
    assert settled(run) == pytest.approx(
        (2048 * 6.0 + 1800 * 0.6 + 148 * 7.5 + 100 * 12.0 + 503 * 30.0) / 1e6, abs=TOLERANCE
    )


def test_a_service_tier_of_auto_or_standard_only_names_no_tier() -> None:
    for tier in ("auto", "standard_only"):
        run = gate()
        send(run, fixture("real_answer.json"), service_tier=tier)
        assert settled(run) == pytest.approx((16 * 3.0 + 17 * 15.0) / 1e6, abs=TOLERANCE)


def test_a_dated_name_is_priced_as_its_family() -> None:
    run = Gate(hosts=(), prices=(("claude-haiku-4-5", RATES),))
    send(run, fixture("real_answer.json"), model="claude-haiku-4-5-20251001")
    assert settled(run) == pytest.approx((16 * 3.0 + 17 * 15.0) / 1e6, abs=TOLERANCE)
    assert provider_anthropic.snapshot_family("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert provider_anthropic.snapshot_family("claude-x-12345678") == "claude-x-12345678"
    assert provider_anthropic.snapshot_family("claude-haiku-4-5") == "claude-haiku-4-5"


def test_an_anthropic_call_with_no_price_is_refused_and_says_what_to_paste() -> None:
    """The shipped table has no Anthropic rows yet, so a priced call needs [agent.prices]."""
    run = Gate(hosts=(), prices=())
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=b"{}"))
    with pytest.raises(anthropic.APIConnectionError):
        anthropic_client(provider).messages.create(
            model="claude-unpriced", max_tokens=5, messages=[{"role": "user", "content": "hi"}]
        )
    assert provider.sent == [], "nothing was sent"
    line = run.line
    assert line["reason"] == "no_price"
    assert run.budget.reserved == []


# The hold: what a call could cost at most, from what the request asks for.


def expected_hold(provider: Provider, rate: float) -> float:
    tokens_in = len(provider.sent[0]) // 4
    return (tokens_in * rate + 100 * 15.0) / 1e6


@pytest.mark.parametrize(
    ("name", "extra", "rate"),
    [
        ("no cache_control: the input rate", {}, 3.0),
        ("a null cache_control is none", {"cache_control": None}, 3.0),
        (
            "cache_control with no ttl: the 5 minute rate",
            {"cache_control": {"type": "ephemeral"}},
            3.75,
        ),
        (
            "ttl 5m: the 5 minute rate",
            {"cache_control": {"type": "ephemeral", "ttl": "5m"}},
            3.75,
        ),
        (
            "ttl 1h at the top: the 1 hour rate",
            {"cache_control": {"type": "ephemeral", "ttl": "1h"}},
            6.0,
        ),
        (
            "ttl 1h on a system block: the 1 hour rate",
            {
                "system": [
                    {
                        "type": "text",
                        "text": "s",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ]
            },
            6.0,
        ),
        (
            "cache_control on a tool: the 5 minute rate",
            {"tools": [{"name": "t", "input_schema": {}, "cache_control": {"type": "ephemeral"}}]},
            3.75,
        ),
        (
            "cache_control in a message block: the 5 minute rate",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
                        ],
                    }
                ]
            },
            3.75,
        ),
    ],
)
def test_the_hold_takes_its_write_rate_from_the_cache_control_of_the_request(
    name: str, extra: dict[str, Any], rate: float
) -> None:
    del name
    run = Gate(hosts=(), prices=((MODEL, RATES),))
    tools = extra.get("tools")
    if tools:  # the gate refuses a tool no gate sees
        run.run.listed.add("t")
    provider, _ = send(run, fixture("real_answer.json"), **extra)
    assert run.budget.reserved[0] == pytest.approx(expected_hold(provider, rate), abs=1e-12)


def test_the_hold_is_never_below_what_the_call_settles_at() -> None:
    """For a reply that wrote the cache, a request that asked for it holds at least the settle."""
    asked = {"cache_control": {"type": "ephemeral", "ttl": "1h"}, "max_tokens": 503}
    run = gate()
    # The hold sizes the input from the request, a quarter of a token a byte: the request is
    # as long as the 4096 tokens the reply counts.
    long_input = [{"role": "user", "content": "x" * (4096 * 4)}]
    send(run, fixture("doc_cached.json"), messages=long_input, **asked)
    assert run.budget.reserved[0] >= settled(run) - 1e-12


def test_the_hold_costs_a_cache_write_rate_below_input_as_the_input_rate() -> None:
    """A row that prices a write below its input rate is the team's: the hold stays at input."""
    cheap = Rates(3.0, 15.0, cache_write_5m=1.0)
    table = provider_anthropic.most_cost
    from scrimmage.providers.impl.wire import wrap_fields

    asked = wrap_fields({"cache_control": {"type": "ephemeral"}})
    assert table(1000, 0, cheap, fields=asked) == pytest.approx(1000 * 3.0 / 1e6)


def test_the_output_a_call_could_bill_is_max_tokens_and_there_is_one_reply() -> None:
    provider = built_in().get("anthropic")
    from scrimmage.providers.impl.wire import wrap_fields

    assert provider.output_cap(wrap_fields({"max_tokens": 77}), "/v1/messages", 4096) == 77
    assert provider.output_cap(wrap_fields({"n": 3, "max_tokens": 77}), "/v1/messages", 4096) == 77
    assert provider.output_cap(wrap_fields({"max_tokens": 0}), "/v1/messages", 4096) == 4096


# A team's row of input and output alone, as the paste template writes it, has no write rates. The
# prompt caching page's pre-warm: 8 input tokens, 5120 written for 5 minutes, no output. At $3 in a
# write for 5 minutes costs 1.25 times that, $3.75, and for 1 hour 2 times, $6.
BARE = Rates(3.0, 15.0)
PREWARM = (8 * 3.0 + 5120 * 3.75) / 1e6


def test_a_row_with_no_write_rates_settles_writes_at_anthropics_multiples_of_input() -> None:
    assert pytest.approx(0.019224, abs=1e-12) == PREWARM
    run = Gate(hosts=(), prices=((MODEL, BARE),))
    send(run, fixture("doc_prewarm.json"))
    assert settled(run) == pytest.approx(0.019224, abs=TOLERANCE)
    assert run.line["tokens"] == {"input": 5128, "output": 0, "cached": 0}
    assert run.line["cost_estimated"] is False


def test_a_one_hour_write_with_no_rate_costs_twice_the_input_rate() -> None:
    reply = json.loads(fixture("doc_prewarm.json"))
    reply["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 5120,
    }
    del reply["usage"]["iterations"]
    run = Gate(hosts=(), prices=((MODEL, BARE),))
    send(run, json.dumps(reply).encode())
    assert settled(run) == pytest.approx((8 * 3.0 + 5120 * 6.0) / 1e6, abs=TOLERANCE)


def test_a_cached_read_with_no_rate_stays_at_the_input_rate_which_is_high_and_safe() -> None:
    run = Gate(hosts=(), prices=((MODEL, BARE),))
    send(run, fixture("doc_cached.json"))
    assert settled(run) == pytest.approx(
        (2048 * 3.0 + 1800 * 3.0 + 148 * 3.75 + 100 * 6.0 + 503 * 15.0) / 1e6, abs=TOLERANCE
    )


def test_the_hold_of_a_row_with_no_write_rates_uses_the_multiples_too() -> None:
    from scrimmage.providers.impl.wire import wrap_fields

    five = wrap_fields({"cache_control": {"type": "ephemeral"}})
    hour = wrap_fields({"cache_control": {"type": "ephemeral", "ttl": "1h"}})
    most = provider_anthropic.most_cost
    assert most(1000, 0, BARE, fields=five) == pytest.approx(1000 * 3.75 / 1e6)
    assert most(1000, 0, BARE, fields=hour) == pytest.approx(1000 * 6.0 / 1e6)


# The real captures, at the same rates. Each hand sum is written from the counts in the file.
#   real_answer      16 in, 17 out:                 16*3 + 17*15                    = 303
#   real_tool_use    376 in, 50 out:                376*3 + 50*15                   = 1878
#   real_cached      13 in, 4 out, 3605 written for 5 minutes and 4508 for 1 hour:
#                    13*3 + 3605*3.75 + 4508*6 + 4*15                               = 40665.75
#   real_cache_read  13 in, 8113 read, 4 out:       13*3 + 8113*0.3 + 4*15          = 2532.9
REAL = [
    ("real_answer.json", False, 303.0, (16, 17, 0)),
    ("real_stream_answer.sse", True, 16 * 3.0 + 14 * 15.0, (16, 14, 0)),
    ("real_tool_use.json", False, 1878.0, (376, 50, 0)),
    ("real_stream_tool_use.sse", True, 1878.0, (376, 50, 0)),
    ("real_cached.json", False, 40665.75, (8126, 4, 0)),
    ("real_cache_read.json", False, 2532.9, (8126, 4, 8113)),
]


@pytest.mark.parametrize(("name", "stream", "micro", "counts"), REAL)
def test_a_real_reply_settles_to_the_hand_sum(
    name: str, stream: bool, micro: float, counts: tuple[int, int, int]
) -> None:
    run = gate()
    run.run.listed.add("get_weather")  # the one tool the request offers is seen by a gate
    send(run, fixture(name), stream=stream, **({"tools": [ORDER]} if "tool" in name else {}))
    assert settled(run) == pytest.approx(micro / 1e6, abs=TOLERANCE)
    assert (
        run.line["tokens"]["input"],
        run.line["tokens"]["output"],
        run.line["tokens"]["cached"],
    ) == counts
    assert run.line["cost_estimated"] is False


def test_a_real_reply_that_wrote_both_lifetimes_is_priced_at_each_rate_not_one() -> None:
    used = provider_anthropic.usage(fixture("real_cached.json"))
    assert used.writes == {"cache_write_5m": 3605, "cache_write_1h": 4508}
    assert used.input == 13 + 3605 + 4508


def test_a_real_search_adds_its_fee_to_its_tokens() -> None:
    """13646 input and 391 output, no cache writes (the request set no cache_control), 1 search."""
    run = gate()
    send(run, fixture("real_web_search.json"))
    assert settled(run) == pytest.approx((13646 * 3.0 + 391 * 15.0) / 1e6 + 0.01, abs=TOLERANCE)


def test_a_real_reply_with_thinking_and_a_decline_in_plain_text_settles_on_its_usage() -> None:
    run = gate()
    send(run, fixture("real_decline.json"))
    assert settled(run) == pytest.approx((45 * 3.0 + 437 * 15.0) / 1e6, abs=TOLERANCE)
    assert run.line["tokens"]["output"] == 437


def test_a_real_tool_use_cut_by_max_tokens_has_an_empty_input_and_is_still_billed() -> None:
    used = provider_anthropic.usage(fixture("real_tool_use_cut.json"))
    assert (used.known, used.input, used.output) == (True, 518, 20)
    reply = json.loads(fixture("real_tool_use_cut.json"))
    assert reply["stop_reason"] == "max_tokens"
    assert reply["content"][0]["type"] == "tool_use" and reply["content"][0]["input"] == {}


def test_a_stream_ends_with_the_input_counts_again_in_its_message_delta() -> None:
    """Anthropic's real message_delta repeats the input and cache counts, not null as the docs say
    it may, and the cumulative output: the last of each wins and the sum is the same."""
    raw = fixture("real_stream_tool_use.sse").decode()
    delta = next(
        line for line in raw.split("\n") if line.startswith("data:") and "message_delta" in line
    )
    usage = json.loads(delta[5:])["usage"]
    assert usage["input_tokens"] == 376 and usage["output_tokens"] == 50
