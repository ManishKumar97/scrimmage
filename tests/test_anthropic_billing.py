"""What an Anthropic call is billed beyond the tokens of its top-level usage: each attempt of a
server-side fallback at its own model's rates, a web search at its fee, and code execution, which
a reply cannot meter.

The hand sums are written from the docs' own numbers, not read back from the code."""

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

import config_cases
from model_gate import Gate, Provider, anthropic_client
from scrimmage import config, plan, prices, provider_anthropic, state
from scrimmage.errors import ConfigError, ScrimmageRefused
from scrimmage.prices import Rates
from scrimmage.providers.impl.wire import wrap_fields
from scrimmage.providers.registry import built_in

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"
TOLERANCE = 1e-8
SONNET = Rates(3.0, 15.0, 0.3, cache_write_5m=3.75, cache_write_1h=6.0)  # the review's row
FABLE = Rates(10.0, 50.0)
OPUS = Rates(5.0, 25.0)
OUTPUT = {"model": "claude-test", "max_tokens": 100}


@pytest.fixture(autouse=True)
def gated() -> Any:
    yield
    state.install(None)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def send(run: Gate, reply: bytes, **request: Any) -> Provider:
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=reply))
    asked: Any = {**OUTPUT, "messages": [{"role": "user", "content": "hi"}], **request}
    anthropic_client(provider).messages.create(**asked)
    return provider


def settled(run: Gate) -> float:
    (_, usd), *_ = run.sink.settles
    return usd


# Server-side fallback. The refusals page: Claude Fable 5 declines a request before any output and
# Claude Opus 4.8 serves it. usage.iterations holds both attempts, and the top-level usage only
# the one that produced the message: 412 input and 264 output. Each attempt is billed at the rates
# of the model that ran it. The attempt that declined is billed only in some categories (bio,
# frontier_llm, reasoning_extraction, or a decline partway through), and this one is cyber, which
# is not: its real bill is Opus's attempt alone, 412 * 5 + 264 * 25 = 8660, $0.00866. Every
# attempt is settled, which counts the declined one too, so the figure below is the high side,
# on purpose: the reply does not say which categories were billed.
# Hand sum, at $10 and $50 for Fable and $5 and $25 for Opus, per million tokens:
#   Fable's attempt   535 * 10 + 0 * 50    =  5350
#   Opus's attempt    412 *  5 + 264 * 25  =  2060 + 6600 = 8660
FALLBACK = (535 * 10 + 412 * 5 + 264 * 25) / 1e6
TOP_LEVEL_ONLY_AT_FABLE = (412 * 10 + 264 * 50) / 1e6


def fallback_run(**settings: Any) -> Gate:
    return Gate(
        hosts=(),
        prices=(("claude-fable-5", FABLE), ("claude-opus-4-8", OPUS)),
        **settings,
    )


def test_every_attempt_of_a_server_side_fallback_is_settled_at_its_own_models_row() -> None:
    assert pytest.approx(0.01401, abs=1e-12) == FALLBACK
    run = fallback_run()
    send(run, fixture("doc_fallback.json"), model="claude-fable-5")
    assert settled(run) == pytest.approx(FALLBACK, abs=TOLERANCE)
    assert settled(run) != pytest.approx(TOP_LEVEL_ONLY_AT_FABLE, abs=TOLERANCE)
    assert run.line["tokens"] == {"input": 535 + 412, "output": 264, "cached": 0}
    assert run.line["cost_estimated"] is False


def test_the_top_level_alone_would_settle_under_when_the_declined_attempt_is_dearer() -> None:
    """The case the review named: counting the top level only is not just a different number, it
    drops a billed attempt, and under a cheap fallback model it is under the bill."""
    cheap = Gate(
        hosts=(),
        prices=(("claude-fable-5", Rates(10.0, 50.0)), ("claude-opus-4-8", Rates(1.0, 5.0))),
    )
    send(cheap, fixture("doc_fallback.json"), model="claude-fable-5")
    full = (535 * 10 + 412 * 1 + 264 * 5) / 1e6
    assert settled(cheap) == pytest.approx(full, abs=TOLERANCE)
    top_level_only_at_opus = (412 * 1 + 264 * 5) / 1e6
    assert settled(cheap) > top_level_only_at_opus + 1e-3


def test_a_hop_whose_model_has_no_row_is_priced_at_the_dearest_row_there_is() -> None:
    run = Gate(hosts=(), prices=(("claude-fable-5", FABLE),))
    send(run, fixture("doc_fallback.json"), model="claude-fable-5")
    # Opus has no row here, so its attempt costs at the dearest row, Fable's.
    assert settled(run) == pytest.approx((535 * 10 + 412 * 10 + 264 * 50) / 1e6, abs=TOLERANCE)


def test_a_single_plain_iteration_is_the_top_level_and_changes_nothing() -> None:
    run = Gate(hosts=(), prices=(("claude-test", Rates(3.0, 15.0)),))
    send(run, fixture("doc_prewarm.json"))
    used = provider_anthropic.usage(fixture("doc_prewarm.json"))
    assert set(used.writes) == {"cache_write_5m", "cache_write_1h"}
    assert used.attempts == (), "no attempt was kept"
    assert settled(run) == pytest.approx(0.019224, abs=TOLERANCE)


def test_usage_reads_each_iteration_and_sums_them() -> None:
    used = provider_anthropic.usage(fixture("doc_fallback.json"))
    assert (used.input, used.output, used.cached) == (535 + 412, 264, 0)
    assert used.known
    assert [(a.model, a.input, a.output) for a in used.attempts] == [
        ("claude-fable-5", 535, 0),
        ("claude-opus-4-8", 412, 264),
    ]
    assert set(used.writes) == {"cache_write_5m", "cache_write_1h"}, (
        "writes holds cache writes only"
    )


def test_an_iteration_that_cannot_be_read_makes_the_reply_not_known() -> None:
    reply = json.loads(fixture("doc_fallback.json"))
    reply["usage"]["iterations"][0]["input_tokens"] = -1
    assert not provider_anthropic.usage(json.dumps(reply).encode()).known
    reply["usage"]["iterations"][0] = {"type": "message", "model": "x"}
    assert not provider_anthropic.usage(json.dumps(reply).encode()).known


def team_row(model: str, **rows: Rates) -> Rates:
    found = built_in().get("anthropic").price(model, rows)
    assert found is not None
    return found


def hold(row: Rates, **asked: Any) -> float:
    return provider_anthropic.most_cost(1000, 500, row, fields=wrap_fields(asked))


def test_each_attempt_of_a_request_with_fallbacks_is_held_on_its_own() -> None:
    """The attempts are added, each at its own row: the call's own, and one for each entry."""
    row = team_row("claude-opus-4-8", **{"claude-opus-4-8": OPUS, "claude-fable-5": FABLE})
    opus, fable = (1000 * 5 + 500 * 25) / 1e6, (1000 * 10 + 500 * 50) / 1e6
    assert hold(row) == pytest.approx(opus)
    assert hold(row, fallbacks=[{"model": "claude-fable-5"}]) == pytest.approx(opus + fable)
    two = [{"model": "claude-fable-5"}, {"model": "claude-opus-4-8"}]
    assert hold(row, fallbacks=two) == pytest.approx(opus + fable + opus)


def test_an_attempt_that_names_its_own_max_tokens_or_speed_is_held_at_them() -> None:
    fast = Rates(20.0, 100.0)
    row = team_row(
        "claude-opus-4-8",
        **{"claude-opus-4-8": OPUS, "claude-fable-5": Rates(10.0, 50.0, tiers=(("fast", fast),))},
    )
    opus = (1000 * 5 + 500 * 25) / 1e6
    big = [{"model": "claude-fable-5", "max_tokens": 8000}]
    assert hold(row, fallbacks=big) == pytest.approx(opus + (1000 * 10 + 8000 * 50) / 1e6)
    small = [{"model": "claude-fable-5", "max_tokens": 10}]
    assert hold(row, fallbacks=small) == pytest.approx(opus + (1000 * 10 + 500 * 50) / 1e6)
    quick = [{"model": "claude-fable-5", "speed": "fast"}]
    assert hold(row, fallbacks=quick) == pytest.approx(opus + (1000 * 20 + 500 * 100) / 1e6)
    both = [{"model": "claude-fable-5", "speed": "fast", "max_tokens": 8000}]
    assert hold(row, fallbacks=both) == pytest.approx(opus + (1000 * 20 + 8000 * 100) / 1e6)
    junk = [{"model": "claude-fable-5", "max_tokens": True, "speed": 5}]
    assert hold(row, fallbacks=junk) == pytest.approx(opus + (1000 * 10 + 500 * 50) / 1e6)


def test_a_model_with_no_row_and_every_default_attempt_are_held_at_the_dearest_row() -> None:
    row = team_row("claude-opus-4-8", **{"claude-opus-4-8": OPUS, "claude-fable-5": FABLE})
    opus, fable = (1000 * 5 + 500 * 25) / 1e6, (1000 * 10 + 500 * 50) / 1e6
    assert hold(row, fallbacks=[{"model": "claude-nonesuch"}]) == pytest.approx(opus + fable)
    assert hold(row, fallbacks="default") == pytest.approx(opus + fable)


# The three cases the round 2 review probed: the hold of a request with fallbacks, against what
# its reply settles at, hop by hop. Each reply has an attempt on every model it names.


def reply_of(*hops: tuple[str, int, int]) -> bytes:
    iterations = [
        {"type": "message", "model": m, "input_tokens": i, "output_tokens": o} for m, i, o in hops
    ]
    last = hops[-1]
    usage = {"input_tokens": last[1], "output_tokens": last[2], "iterations": iterations}
    return json.dumps({"type": "message", "model": last[0], "usage": usage}).encode()


def settle(reply: bytes, row: Rates) -> float:
    used = provider_anthropic.usage(reply)
    assert used.known
    return built_in().get("anthropic").cost(row, used)


def held_and_settled(row: Rates, reply: bytes, **asked: Any) -> tuple[float, float]:
    fields = wrap_fields({"max_tokens": 100, **asked})
    held = provider_anthropic.most_cost(1000, 100, row, fields=fields)
    return held, settle(reply, row)


def test_the_hold_covers_the_settle_when_an_entry_names_a_bigger_max_tokens_and_fast_speed() -> (
    None
):
    cheap, dear = Rates(1.0, 5.0), Rates(10.0, 50.0, tiers=(("fast", Rates(20.0, 100.0)),))
    row = team_row("claude-a", **{"claude-a": cheap, "claude-b": dear})
    entry = {"model": "claude-b", "max_tokens": 8000, "speed": "fast"}
    reply = reply_of(("claude-a", 1000, 100), ("claude-b", 1000, 8000))
    held, settled_at = held_and_settled(row, reply, fallbacks=[entry])
    assert held >= settled_at
    assert settled_at == pytest.approx((1000 * 1 + 100 * 5 + 1000 * 10 + 8000 * 50) / 1e6)


def test_the_hold_covers_the_settle_when_the_named_model_has_no_row() -> None:
    row = team_row("claude-a", **{"claude-a": Rates(1.0, 5.0), "claude-b": Rates(10.0, 50.0)})
    reply = reply_of(("claude-a", 1000, 0), ("claude-c", 1000, 100))
    held, settled_at = held_and_settled(row, reply, fallbacks=[{"model": "claude-c"}])
    assert held >= settled_at
    # claude-c has no row, so its attempt is priced at the dearest, claude-b's.
    assert settled_at == pytest.approx((1000 * 1 + 1000 * 10 + 100 * 50) / 1e6)


def test_the_hold_covers_the_settle_when_default_falls_back_to_a_dearer_model() -> None:
    row = team_row("claude-a", **{"claude-a": Rates(1.0, 5.0), "claude-b": Rates(10.0, 50.0)})
    reply = reply_of(("claude-a", 1000, 0), ("claude-b", 1000, 100))
    held, settled_at = held_and_settled(row, reply, fallbacks="default")
    assert held >= settled_at
    assert settled_at == pytest.approx((1000 * 1 + 1000 * 10 + 100 * 50) / 1e6)


def test_the_hold_of_a_fallbacks_request_covers_its_settle() -> None:
    run = fallback_run()
    long_input = [{"role": "user", "content": "x" * 4 * 1000}]
    send(
        run,
        fixture("doc_fallback.json"),
        model="claude-fable-5",
        messages=long_input,
        extra_body={"fallbacks": [{"model": "claude-opus-4-8"}]},
    )
    assert run.budget.reserved[0] >= settled(run)
    plain = fallback_run()
    send(plain, fixture("real_answer.json"), model="claude-fable-5", messages=long_input)
    assert run.budget.reserved[0] > plain.budget.reserved[0], "the second attempt is held too"


# Web search: $10 for each 1,000 searches on top of the tokens (the pricing page). A real reply to a
# request with one search tool and no cache_control: 13646 input, 391 output, no cache writes and
# 1 search. At the review's Sonnet-like row, $3 and $15:
#   13646 * 3 + 391 * 15 = 40938 + 5865 = 46803, so $0.046803 in tokens, and $0.01 for the search
SEARCH_TOKENS = (13646 * 3.0 + 391 * 15.0) / 1e6


def test_a_web_search_the_reply_counts_is_settled_at_ten_dollars_a_thousand() -> None:
    assert pytest.approx(0.046803, abs=1e-9) == SEARCH_TOKENS
    run = Gate(hosts=(), prices=(("claude-test", SONNET),))
    send(run, fixture("real_web_search.json"))
    assert settled(run) == pytest.approx(SEARCH_TOKENS + 0.01, abs=TOLERANCE)


def test_three_searches_cost_three_fees_and_none_cost_nothing() -> None:
    reply = json.loads(fixture("real_web_search.json"))
    reply["usage"]["server_tool_use"]["web_search_requests"] = 3
    run = Gate(hosts=(), prices=(("claude-test", SONNET),))
    send(run, json.dumps(reply).encode())
    assert settled(run) == pytest.approx(SEARCH_TOKENS + 0.03, abs=TOLERANCE)
    reply["usage"]["server_tool_use"] = {"web_fetch_requests": 4}
    free = Gate(hosts=(), prices=(("claude-test", SONNET),))
    send(free, json.dumps(reply).encode())
    assert settled(free) == pytest.approx(SEARCH_TOKENS, abs=TOLERANCE), "a fetch has no fee"


def test_a_rows_own_fee_beats_the_default() -> None:
    dear = Rates(
        3.0,
        15.0,
        0.3,
        cache_write_5m=3.75,
        cache_write_1h=6.0,
        fees=(("web_search_request", 0.02),),
    )
    run = Gate(hosts=(), prices=(("claude-test", dear),))
    send(run, fixture("real_web_search.json"))
    assert settled(run) == pytest.approx(SEARCH_TOKENS + 0.02, abs=TOLERANCE)


def test_a_price_files_fee_beats_the_default_and_a_rows_beats_the_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        '[anthropic]\nurl = "https://x"\nas_of = "2026-11-23"\n'
        "[anthropic.fees]\nweb_search_request = 0.05\n"
        '[anthropic.models."claude-file"]\ninput = 3\noutput = 15\n'
    )
    monkeypatch.setattr(prices, "shipped", lambda: prices.parse(text, "test"))
    table = built_in().get("anthropic")
    file_row = table.price("claude-file", {})
    assert file_row is not None and dict(file_row.fees) == {"web_search_request": 0.05}
    mine = Rates(3.0, 15.0, fees=(("web_search_request", 0.07),))
    team = table.price("claude-file", {"claude-file": mine})
    assert team is not None and dict(team.fees) == {"web_search_request": 0.07}
    nothing = table.price("claude-team", {"claude-team": Rates(1.0, 5.0)})
    assert nothing is not None and dict(nothing.fees) == {"web_search_request": 0.05}


def test_with_no_file_section_the_fee_is_anthropics_documented_one() -> None:
    row = built_in().get("anthropic").price("m", {"m": Rates(3.0, 15.0)})
    assert row is not None and dict(row.fees) == {"web_search_request": 0.01}


def test_a_request_that_offers_web_search_is_held_for_its_searches() -> None:
    table = built_in().get("anthropic")
    row = table.price("m", {"m": SONNET})
    assert row is not None
    base = table.most_cost(1000, 0, row, path="/v1/messages", fields=wrap_fields({}))
    five = wrap_fields(
        {"tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]}
    )
    held = table.most_cost(1000, 0, row, path="/v1/messages", fields=five)
    # A server tool writes the cache by itself, so the input is held at the 5 minute write rate.
    assert held == pytest.approx(1000 * 3.75 / 1e6 + 5 * 0.01)
    assert held > base
    default = wrap_fields({"tools": [{"type": "web_search_20250305", "name": "web_search"}]})
    assert table.most_cost(1000, 0, row, path="/v1/messages", fields=default) == pytest.approx(
        1000 * 3.75 / 1e6 + provider_anthropic.DEFAULT_SEARCHES * 0.01
    )
    junk = wrap_fields({"tools": [{"type": "web_search_20250305", "max_uses": True}]})
    assert table.most_cost(1000, 0, row, path="/v1/messages", fields=junk) == pytest.approx(
        1000 * 3.75 / 1e6 + provider_anthropic.DEFAULT_SEARCHES * 0.01
    )


@pytest.mark.parametrize(
    "kind", ["web_search_20250305", "web_fetch_20250910", "code_execution_20260120"]
)
def test_a_server_tool_holds_at_least_the_five_minute_write_rate(kind: str) -> None:
    """A server tool caches its results by itself, with no cache_control in the request."""
    table = built_in().get("anthropic")
    row = table.price("m", {"m": Rates(3.0, 15.0)})
    assert row is not None
    tools: list[dict[str, Any]] = [
        {"type": kind, "name": "web_search" if kind.startswith("web_search") else "t"}
    ]
    if kind.startswith("code"):
        tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 1})
    held = table.most_cost(1000, 0, row, path="/v1/messages", fields=wrap_fields({"tools": tools}))
    searches = 0.0
    for tool in tools:
        if tool["name"] == "web_search":
            searches += 0.01 * (tool.get("max_uses") or provider_anthropic.DEFAULT_SEARCHES)
    assert held == pytest.approx(1000 * 3.75 / 1e6 + searches)
    none = table.most_cost(1000, 0, row, path="/v1/messages", fields=wrap_fields({}))
    assert none == pytest.approx(1000 * 3.0 / 1e6)


# Code execution is billed by container time, which a reply never reports.


def offered(tools: list[dict[str, Any]]) -> Any:
    fields = wrap_fields({**OUTPUT, "messages": [], "tools": tools})
    return built_in().get("anthropic").offered("/v1/messages", fields)


def test_code_execution_alone_is_refused_with_its_reason() -> None:
    found = offered([{"type": "code_execution_20250825", "name": "code_execution"}])
    assert not found.hosted_reads
    assert not found.unknown, "Scrimmage knows code execution; it cannot budget it"
    ((kind, why),) = found.refused
    assert kind == "code_execution_20250825" and "container time" in why


def test_code_execution_with_web_search_or_fetch_of_the_free_version_goes_through() -> None:
    for companion in ("web_search_20260209", "web_fetch_20260209", "web_search_20260318"):
        found = offered(
            [
                {"type": "code_execution_20250825", "name": "code_execution"},
                {"type": companion, "name": "w"},
            ]
        )
        assert not found.unknown and not found.refused, companion
        assert "code_execution" in found.hosted_reads
    old = offered(
        [
            {"type": "code_execution_20250825", "name": "code_execution"},
            {"type": "web_search_20250305", "name": "w"},
        ]
    )
    assert old.refused, "an older web search does not make code execution free"


def test_code_execution_alone_stops_a_run_before_anything_is_sent() -> None:
    run = Gate(hosts=(), prices=(("claude-test", SONNET),))
    provider = Provider(
        run.events, lambda r: httpx2.Response(200, content=fixture("real_answer.json"))
    )
    asked: Any = {
        **OUTPUT,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "code_execution_20250825", "name": "code_execution"}],
    }
    with pytest.raises(anthropic.APIConnectionError) as caught:
        anthropic_client(provider).messages.create(**asked)
    assert isinstance(caught.value.__cause__, ScrimmageRefused)
    said = str(caught.value.__cause__)
    assert said.startswith(
        "the model request offers code_execution_20250825, which Scrimmage cannot budget: "
        "code execution is billed by container time"
    )
    assert "does not know" not in said
    assert run.line["reason"] == "unmetered_tool"
    assert provider.sent == []


# The price format's fees.


def test_a_price_file_reads_a_providers_fees_and_a_rows_own() -> None:
    text = (
        '[anthropic]\nurl = "https://x"\nas_of = "2026-11-23"\n'
        "[anthropic.fees]\nweb_search_request = 0.01\n"
        '[anthropic.models."m"]\ninput = 3\noutput = 15\nweb_search_request = 0.02\n'
    )
    parsed = prices.parse(text, "t")
    section = parsed.section("anthropic")
    assert section is not None and section.fees == (("web_search_request", 0.01),)
    assert dict(section.models)["m"].fees == (("web_search_request", 0.02),)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("web_search_request = -1", "expected dollars for each request"),
        ('web_search_request = "x"', "expected dollars for each request"),
        ("web_search_request = true", "expected dollars for each request"),
        ("web_search_request = 0", r"below 0\.0001"),
        ("web_search_request = 0.00001", "may not make it free"),
        ("web_crawl_request = 1", "unknown fee"),
    ],
)
def test_a_fee_is_checked_like_a_rate(line: str, message: str) -> None:
    text = (
        '[anthropic]\nurl = "https://x"\nas_of = "2026-11-23"\n'
        f'[anthropic.fees]\n{line}\n[anthropic.models."m"]\ninput = 3\noutput = 15\n'
    )
    with pytest.raises(ConfigError, match=message):
        prices.parse(text, "t")


def test_a_fee_survives_the_plan_a_child_run_reads() -> None:
    row = Rates(3.0, 15.0, fees=(("web_search_request", 0.02),))
    doc = plan.price_json(row)
    assert doc["web_search_request"] == 0.02
    assert plan.price_from(doc).fees == (("web_search_request", 0.02),)
    assert "web_search_request" not in plan.price_json(Rates(3.0, 15.0))


def test_a_team_overrides_the_fee_in_agent_prices(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(
        '[agent]\nfunction = "acme.agent:answer"\n'
        '\n[agent.prices."claude-x"]\ninput = 3.0\noutput = 15.0\nweb_search_request = 0.03\n'
        '\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["ok"]\n'
    )
    loaded = dict(config.load(tmp_path / "scrimmage.toml").agent.prices)["claude-x"]
    assert loaded.fees == (("web_search_request", 0.03),)
    table = built_in().get("anthropic")
    held = table.price("claude-x", {"claude-x": loaded})
    assert held is not None and dict(held.fees) == {"web_search_request": 0.03}


def test_an_update_prints_a_changed_fee() -> None:
    from scrimmage import prices_update

    def section(fee: float) -> prices.Section:
        text = (
            f'[anthropic]\nurl = "https://x"\nas_of = "2026-11-23"\n[anthropic.fees]\n'
            f'web_search_request = {fee}\n[anthropic.models."m"]\ninput = 3\noutput = 15\n'
        )
        found = prices.parse(text, "t").section("anthropic")
        assert found is not None
        return found

    assert prices_update.diff(section(0.01), section(0.02)) == [
        "changed fee web_search_request: 0.01 to 0.02"
    ]


def test_a_fee_in_a_models_row_of_a_file_has_the_same_floor() -> None:
    text = (
        '[anthropic]\nurl = "https://x"\nas_of = "2026-11-23"\n'
        '[anthropic.models."m"]\ninput = 3\noutput = 15\nweb_search_request = 0\n'
    )
    with pytest.raises(ConfigError, match=r"below 0\.0001"):
        prices.parse(text, "t")
    assert prices.parse(text.replace("= 0\n", "= 0.02\n"), "t")


def test_a_team_may_set_a_fee_of_zero_in_agent_prices_as_it_may_set_a_price_of_zero() -> None:
    row = Rates(3.0, 15.0, fees=(("web_search_request", 0.0),))
    run = Gate(hosts=(), prices=(("claude-test", row),))
    send(run, fixture("real_web_search.json"))
    assert settled(run) == pytest.approx((13646 * 3.0 + 391 * 15.0) / 1e6, abs=TOLERANCE)


def test_check_warns_of_a_fee_a_provider_does_not_charge() -> None:
    text = (
        '[openai]\nurl = "https://x"\nas_of = "2026-11-23"\n[openai.fees]\n'
        'web_search_request = 0.01\n[openai.models."m"]\ninput = 3\noutput = 15\n'
        '[anthropic]\nurl = "https://x"\nas_of = "2026-11-23"\n[anthropic.fees]\n'
        'web_search_request = 0.01\n[anthropic.models."m"]\ninput = 3\noutput = 15\n'
    )
    assert prices.unused_fees(prices.parse(text, "t")) == ["openai web_search_request"]
    assert prices.unused_fees(None) == []
    assert built_in().get("openai").fees == ()
    assert built_in().get("anthropic").fees == ("web_search_request",)


def test_a_call_with_no_price_is_told_where_anthropics_rates_are_and_given_no_number() -> None:
    """Scrimmage ships no Anthropic prices, so the line to paste names the pricing page and holds
    no rate. A line with OpenAI's example rates would price a Claude model 20 to 25 times low."""
    example = built_in().get("anthropic").price_example
    assert "https://platform.claude.com/docs/en/about-claude/pricing" in example
    assert not any(ch.isdigit() for ch in example.split("(")[0]), "no rate is suggested"
    pasted = f'[anthropic.models."claude-x"]\nprice = {example.split(" (")[0]}\n'
    with pytest.raises(ConfigError, match="Invalid value"):  # not a price until filled in
        prices.parse(pasted, "pasted")


def test_openais_price_example_is_the_line_it_always_gave() -> None:
    assert built_in().get("openai").price_example == (
        "{input = 0.15, output = 0.60} (dollars per million tokens)"
    )


def test_the_team_files_search_fee_reaches_the_bill() -> None:
    """scrimmage-runs/prices.toml's [anthropic.fees] prices a search the reply counts, through the
    gate: before, the plan carried the file's rows and dropped its fees, so a team that set $0.05
    was billed Anthropic's $0.01."""
    run = Gate(
        hosts=(),
        prices=(),
        file_prices=(("anthropic", (("claude-test", SONNET),)),),
        file_fees=(("anthropic", (("web_search_request", 0.05),)),),
    )
    send(run, fixture("real_web_search.json"))
    assert settled(run) == pytest.approx(SEARCH_TOKENS + 0.05, abs=TOLERANCE)


def test_a_rows_fee_beats_the_team_files_which_beats_the_shipped_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        '[anthropic]\nurl = "https://x"\nas_of = "2026-11-23"\n'
        "[anthropic.fees]\nweb_search_request = 0.03\n"
        '[anthropic.models."claude-file"]\ninput = 3\noutput = 15\n'
    )
    monkeypatch.setattr(prices, "shipped", lambda: prices.parse(text, "test"))
    table = built_in().get("anthropic")
    team = {"web_search_request": 0.05}
    plain = table.price("m", {"m": Rates(3.0, 15.0)}, team)
    assert plain is not None and dict(plain.fees) == {"web_search_request": 0.05}
    shipped = table.price("m", {"m": Rates(3.0, 15.0)})
    assert shipped is not None and dict(shipped.fees) == {"web_search_request": 0.03}
    own = Rates(3.0, 15.0, fees=(("web_search_request", 0.07),))
    row = table.price("m", {"m": own}, team)
    assert row is not None and dict(row.fees) == {"web_search_request": 0.07}


def test_the_team_files_fees_survive_the_plans_trip_to_the_sdk() -> None:
    sent = plan.Plan(
        run="r",
        test="t",
        row="w",
        mocks=(),
        faults=(),
        tools=(),
        real=(),
        time_limit_s=60.0,
        file_fees=(("anthropic", (("web_search_request", 0.05),)),),
    )
    back = plan.Plan.from_json(json.loads(json.dumps(sent.to_json())))
    assert back.file_fees == sent.file_fees
    assert back.fees_for("anthropic") == {"web_search_request": 0.05}
    assert back.fees_for("openai") == {}


def test_a_team_price_files_search_fee_goes_from_the_file_to_the_bill(tmp_path: Path) -> None:
    """The whole way: scrimmage-runs/prices.toml, config.load, plan_for, the plan's JSON trip to
    the SDK, then the gate's settle. AN-C2-4's bug was plan_for keeping the file's rows only."""
    (tmp_path / "scrimmage.toml").write_text(config_cases.AGENT + config_cases.TEST)
    (tmp_path / "scrimmage-runs").mkdir()
    (tmp_path / "scrimmage-runs" / "prices.toml").write_text(
        '[anthropic]\nurl = "https://example.test/prices"\nas_of = "2026-11-24"\n\n'
        "[anthropic.fees]\nweb_search_request = 0.05\n\n"
        '[anthropic.models."claude-test"]\ninput = 3.0\noutput = 15.0\ncached_input = 0.3\n'
        "cache_write_5m = 3.75\ncache_write_1h = 6.0\n"
    )
    cfg = config.load(tmp_path / "scrimmage.toml")
    made = plan.plan_for(cfg, plan.rows(cfg)[0], "run-1")
    sent = plan.Plan.from_json(json.loads(json.dumps(made.to_json())))
    assert sent.fees_for("anthropic") == {"web_search_request": 0.05}
    run = Gate(hosts=(), prices=(), file_prices=sent.file_prices, file_fees=sent.file_fees)
    send(run, fixture("real_web_search.json"))
    assert settled(run) == pytest.approx(SEARCH_TOKENS + 0.05, abs=TOLERANCE)
