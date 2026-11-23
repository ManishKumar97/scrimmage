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
# of the model that ran it, and the one that declined is billed when its category is.
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
    assert set(used.writes) == {"cache_write_5m", "cache_write_1h"}, "no hop was kept"
    assert settled(run) == pytest.approx(0.019224, abs=TOLERANCE)


def test_usage_reads_each_iteration_and_sums_them() -> None:
    used = provider_anthropic.usage(fixture("doc_fallback.json"))
    assert (used.input, used.output, used.cached) == (535 + 412, 264, 0)
    assert used.known
    assert used.writes["hop0:claude-fable-5:input"] == 535
    assert used.writes["hop1:claude-opus-4-8:output"] == 264


def test_an_iteration_that_cannot_be_read_makes_the_reply_not_known() -> None:
    reply = json.loads(fixture("doc_fallback.json"))
    reply["usage"]["iterations"][0]["input_tokens"] = -1
    assert not provider_anthropic.usage(json.dumps(reply).encode()).known
    reply["usage"]["iterations"][0] = {"type": "message", "model": "x"}
    assert not provider_anthropic.usage(json.dumps(reply).encode()).known


def test_a_request_with_fallbacks_is_held_for_every_attempt_at_the_dearest_model() -> None:
    row = (
        built_in()
        .get("anthropic")
        .price("claude-opus-4-8", {"claude-opus-4-8": OPUS, "claude-fable-5": FABLE})
    )
    assert row is not None
    most = provider_anthropic.most_cost
    one = most(1000, 500, row, fields=wrap_fields({}))
    assert one == pytest.approx((1000 * 5 + 500 * 25) / 1e6)
    named = wrap_fields({"fallbacks": [{"model": "claude-fable-5"}]})
    assert most(1000, 500, row, fields=named) == pytest.approx(2 * (1000 * 10 + 500 * 50) / 1e6)
    two = wrap_fields({"fallbacks": [{"model": "claude-fable-5"}, {"model": "claude-opus-4-8"}]})
    assert most(1000, 500, row, fields=two) == pytest.approx(3 * (1000 * 10 + 500 * 50) / 1e6)
    default = wrap_fields({"fallbacks": "default"})
    assert most(1000, 500, row, fields=default) == pytest.approx(2 * one)
    unknown = wrap_fields({"fallbacks": [{"model": "claude-nonesuch"}]})
    assert most(1000, 500, row, fields=unknown) == pytest.approx(2 * one)


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
    assert run.budget.reserved[0] > 1.9 * plain.budget.reserved[0], "two attempts are held"


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
    (kind,) = found.unknown
    assert kind.startswith("code_execution_20250825") and "container time" in kind


def test_code_execution_with_web_search_or_fetch_of_the_free_version_goes_through() -> None:
    for companion in ("web_search_20260209", "web_fetch_20260209", "web_search_20260318"):
        found = offered(
            [
                {"type": "code_execution_20250825", "name": "code_execution"},
                {"type": companion, "name": "w"},
            ]
        )
        assert not found.unknown, companion
        assert "code_execution" in found.hosted_reads
    old = offered(
        [
            {"type": "code_execution_20250825", "name": "code_execution"},
            {"type": "web_search_20250305", "name": "w"},
        ]
    )
    assert old.unknown, "an older web search does not make code execution free"


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
    assert "container time" in str(caught.value.__cause__)
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
        ("web_search_request = -1", "expected dollars"),
        ('web_search_request = "x"', "expected dollars"),
        ("web_search_request = true", "expected dollars"),
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
