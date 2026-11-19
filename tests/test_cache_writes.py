"""Current OpenAI models are priced, and cache writes are read, priced and held for."""

import json
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest

import scrimmage_core
from scrimmage_core import plan
from scrimmage_core import provider_openai as table
from scrimmage_core.errors import ConfigError
from test_cached_tokens import priced
from test_http_gate import Budget, gated, install, make_plan

__all__ = ["gated"]  # the fixture, used by name

RESPONSES = "https://api.openai.com/v1/responses"
# OpenAI's worked example from its prompt caching guide: 1000 plain input tokens, 4000
# cached, and 7000 written, 3000 of them for 30 minutes and 4000 for 12 hours.
EXAMPLE = {
    "input_tokens": 12000,
    "input_tokens_details": {
        "cached_tokens": 4000,
        "cache_write_tokens": 7000,
        "cache_write_tokens_details": {"ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000},
    },
    "output_tokens": 10,
}
# The guide's rates for that example, per million tokens, with an output rate added.
GUIDE = table.Rates(
    input=1.0, output=2.0, cached_input=0.1, cache_write_30m=1.25, cache_write_12h=2.5
)
# A real gpt-5-nano Responses reply's usage, from 19 November 2026.
NANO = {
    "input_tokens": 14,
    "input_tokens_details": {
        "cache_write_tokens": 0,
        "cache_write_tokens_details": {"ttl_12h_tokens": 0, "ttl_30m_tokens": 0},
        "cached_tokens": 0,
    },
    "output_tokens": 65,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 79,
}


def reply_of(usage: dict[str, object]) -> bytes:
    return json.dumps({"object": "response", "output": [], "usage": usage}).encode()


@pytest.mark.design("prices.current_models")
@pytest.mark.parametrize(
    ("model", "rates"),
    [
        ("gpt-4o-mini", table.Rates(0.15, 0.60, 0.075)),
        ("gpt-4.1-mini", table.Rates(0.40, 1.60, 0.10)),
        ("gpt-5", table.Rates(1.25, 10.00, 0.125)),
        ("gpt-5-mini", table.Rates(0.25, 2.00, 0.025)),
        ("gpt-5-nano", table.Rates(0.05, 0.40, 0.005)),
        ("gpt-5-nano-2025-08-07", table.Rates(0.05, 0.40, 0.005)),  # a dated snapshot
        ("gpt-5-pro", table.Rates(15.00, 120.00)),
        ("gpt-5.1", table.Rates(1.25, 10.00, 0.125)),
        ("gpt-5.2", table.Rates(1.75, 14.00, 0.175)),
        ("gpt-5.2-pro", table.Rates(21.00, 168.00)),
        ("gpt-5.3-codex", table.Rates(1.75, 14.00, 0.175)),
        ("gpt-5.4-mini", table.Rates(0.75, 4.50, 0.075)),
        ("gpt-5.4-nano", table.Rates(0.20, 1.25, 0.02)),
    ],
)
def test_the_built_in_table_has_current_models_at_openais_prices(
    model: str, rates: table.Rates
) -> None:
    # OpenAI's pricing page, standard tier, checked on 19 November 2026.
    assert table.price(model, {}) == rates


@pytest.mark.design("prices.long_context_left_out")
@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol"])
def test_a_model_with_a_long_context_price_is_not_in_the_table(model: str) -> None:
    assert table.price(model, {}) is None


@pytest.mark.design("prices.current_model_runs")
def test_a_gpt5_nano_call_with_a_budget_needs_no_team_price(gated: None) -> None:
    sink, _ = install(make_plan(budget=1.0), Budget(limit=1.0))
    answer = httpx2.MockTransport(lambda request: httpx2.Response(200, content=reply_of(NANO)))
    sent = json.dumps({"model": "gpt-5-nano", "input": "hi"}).encode()
    reply = httpx2.Client(transport=answer).post(RESPONSES, content=sent)
    assert reply.status_code == 200  # with no price, the call is refused before it is sent
    line = sink.lines[0]
    assert line["cost_usd"] == pytest.approx((14 * 0.05 + 65 * 0.40) / 1e6)
    assert line["cost_estimated"] is False


@pytest.mark.design("cache_write.read_from_usage")
@pytest.mark.parametrize(
    ("usage", "cached", "written"),
    [
        (EXAMPLE, 4000, (3000, 4000)),  # Responses, split by lifetime
        (NANO, 0, (0, 0)),  # a real reply with no writes
        # Chat Completions: one write count and no split, so 30 minutes.
        (
            {
                "prompt_tokens": 2048,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 1000},
            },
            1024,
            (1000, 0),
        ),
        # A reply with no write count at all, as from an older model.
        ({"prompt_tokens": 100, "completion_tokens": 5}, 0, (0, 0)),
    ],
    ids=["responses split", "real nano reply", "chat", "no write count"],
)
def test_the_written_counts_are_read_from_each_api(
    usage: dict[str, object], cached: int, written: tuple[int, int]
) -> None:
    used = table.usage(reply_of(usage))
    assert used.known and used.cached == cached
    assert (used.written_30m, used.written_12h) == written


def test_the_written_counts_are_read_from_a_streamed_reply() -> None:
    done = {"type": "response.completed", "response": {"usage": EXAMPLE}}
    body = f"event: response.completed\ndata: {json.dumps(done)}\n\n".encode()
    used = table.usage(body)
    assert (used.input, used.cached, used.written_30m, used.written_12h) == (
        12000,
        4000,
        3000,
        4000,
    )


@pytest.mark.design("cache_write.bad_count_reads_high")
@pytest.mark.parametrize(
    ("details", "written"),
    [
        # More writes than the 8000 tokens not cached: every one of those written for 12h.
        ({"cached_tokens": 4000, "cache_write_tokens": 8001}, (0, 8000)),
        ({"cached_tokens": 4000, "cache_write_tokens": -1}, (0, 8000)),
        ({"cached_tokens": 4000, "cache_write_tokens": "7000"}, (0, 8000)),
        ({"cached_tokens": 4000, "cache_write_tokens": True}, (0, 8000)),
        # A 12-hour count above the writes: every write for 12 hours.
        (
            {
                "cached_tokens": 4000,
                "cache_write_tokens": 7000,
                "cache_write_tokens_details": {"ttl_12h_tokens": 7001},
            },
            (0, 7000),
        ),
        (
            {
                "cached_tokens": 4000,
                "cache_write_tokens": 7000,
                "cache_write_tokens_details": {"ttl_12h_tokens": 1.5},
            },
            (0, 7000),
        ),
    ],
)
def test_a_written_count_that_cannot_be_trusted_is_read_high(
    details: dict[str, object], written: tuple[int, int]
) -> None:
    usage = {"input_tokens": 12000, "output_tokens": 10, "input_tokens_details": details}
    used = table.usage(reply_of(usage))
    assert (used.written_30m, used.written_12h) == written


@pytest.mark.design("cache_write.cost_by_hand")
def test_openais_worked_example_costs_what_its_guide_says() -> None:
    used = table.usage(reply_of(EXAMPLE))
    usd = table.cost(
        used.input, used.output, GUIDE, used.cached, used.written_30m, used.written_12h
    )
    # The guide: (1000 + 4000 x 0.1 + 3000 x 1.25 + 4000 x 2.5) / 1000000 = $0.01515 input.
    assert usd == pytest.approx(0.01515 + 10 * 2.0 / 1e6)


@pytest.mark.design("cache_write.no_double_count")
def test_cached_and_written_tokens_are_parts_of_the_input_count() -> None:
    # Only plain input costs anything here, and every input token is cached or written.
    rates = table.Rates(1000.0, 0.0, cached_input=0.0, cache_write_30m=0.0, cache_write_12h=0.0)
    assert table.cost(100, 0, rates, cached=40, written_30m=50, written_12h=10) == 0.0
    # And with every token plain, each is charged once at the input rate.
    assert table.cost(100, 0, rates) == pytest.approx(0.1)


@pytest.mark.design("cache_write.no_rate_is_input")
def test_with_no_write_rate_a_written_token_costs_the_input_rate() -> None:
    rates = table.Rates(1000.0, 2000.0, cached_input=250.0)
    by_parts = table.cost(12000, 10, rates, cached=4000, written_30m=3000, written_12h=4000)
    assert by_parts == table.cost(12000, 10, rates, cached=4000)  # as before cache writes


@pytest.mark.design("cache_write.reservation_dearest")
@pytest.mark.parametrize(
    ("rates", "per_token"),
    [
        (GUIDE, 2.5),  # the 12-hour write is the dearest input rate
        (table.Rates(1.0, 2.0, 0.1), 1.0),  # no write rate: the input rate, as before
    ],
)
def test_the_reservation_holds_input_at_its_dearest_rate(
    gated: None, rates: table.Rates, per_token: float
) -> None:
    budget = Budget(limit=1000.0)
    sink, _ = install(make_plan(budget=1000.0, prices=(("gpt-5.6-luna", rates),)), budget)
    sent = json.dumps({"model": "gpt-5.6-luna", "input": "hi", "max_output_tokens": 10}).encode()
    held: list[float] = []
    real = budget.ledger.reserve

    def watch(usd: float, *rest: Any) -> object:
        held.append(usd)
        return real(usd, *rest)

    budget.ledger.reserve = watch  # type: ignore[method-assign, assignment]
    answer = httpx2.MockTransport(lambda request: httpx2.Response(200, content=reply_of(EXAMPLE)))
    httpx2.Client(transport=answer).post(RESPONSES, content=sent)
    assert held == [pytest.approx((len(sent) // 4 * per_token + 10 * 2.0) / 1e6)]
    usd = table.cost(12000, 10, rates, 4000, 3000, 4000)
    assert sink.lines[0]["cost_usd"] == pytest.approx(round(usd, 8))
    assert sink.lines[0]["tokens"] == {"input": 12000, "output": 10, "cached": 4000}


@pytest.mark.design("cache_write.recorded_cost")
def test_a_reply_with_cache_writes_records_its_exact_cost(gated: None) -> None:
    # gpt-5.6-luna's standard prices, per million tokens, on 19 November 2026.
    luna = table.Rates(0.20, 1.20, 0.02, 0.25, 0.50)
    sink, _ = install(make_plan(budget=5.0, prices=(("gpt-5.6-luna", luna),)), Budget(5.0))
    answer = httpx2.MockTransport(lambda request: httpx2.Response(200, content=reply_of(EXAMPLE)))
    sent = json.dumps({"model": "gpt-5.6-luna", "input": "hi"}).encode()
    httpx2.Client(transport=answer).post(RESPONSES, content=sent)
    # 1000 at $0.20, 4000 at $0.02, 3000 at $0.25, 4000 at $0.50 and 10 out at $1.20.
    exact = (1000 * 0.20 + 4000 * 0.02 + 3000 * 0.25 + 4000 * 0.50 + 10 * 1.20) / 1e6
    assert sink.lines[0]["cost_usd"] == pytest.approx(exact, rel=1e-6)


@pytest.mark.design("cache_write.setting")
def test_a_team_price_takes_optional_cache_write_rates(tmp_path: Path) -> None:
    cfg = priced(
        tmp_path, "{input = 0.2, cache_write_30m = 0.25, cache_write_12h = 0.5, output = 1}"
    )
    assert cfg.agent.prices == (("acme", table.Rates(0.2, 1.0, None, 0.25, 0.5)),)
    only = priced(tmp_path, "{input = 0.2, cache_write_30m = 0.25, output = 1}")
    assert only.agent.prices == (("acme", table.Rates(0.2, 1.0, None, 0.25, None)),)


@pytest.mark.design("cache_write.setting_checked")
@pytest.mark.parametrize("key", ["cache_write_30m", "cache_write_12h"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("-1", "expected dollars per million tokens"),
        ('"x"', "expected dollars per million tokens"),
        ("0.1", "0.1 is below input, 0.2"),
    ],
)
def test_a_bad_cache_write_rate_is_rejected_with_its_key(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=f"{key}: {message}") as caught:
        priced(tmp_path, f"{{input = 0.2, {key} = {value}, output = 1}}")
    assert "[agent].prices acme" in str(caught.value)


@pytest.mark.design("cache_write.plan_round_trip")
def test_the_cache_write_rates_survive_the_plan_and_fit_its_schema(tmp_path: Path) -> None:
    cfg = priced(
        tmp_path, "{input = 0.2, cache_write_30m = 0.25, cache_write_12h = 0.5, output = 1}"
    )
    sent = plan.plan_for(cfg, plan.rows(cfg)[0], "run-1").to_json()
    budget = sent["budget"]
    assert isinstance(budget, dict)
    want = {"input": 0.2, "output": 1.0, "cache_write_30m": 0.25, "cache_write_12h": 0.5}
    assert budget["prices"] == {"acme": want}
    assert plan.Plan.from_json(sent).prices == cfg.agent.prices
    schema = json.loads(
        (Path(scrimmage_core.__file__).parent / "schemas" / "plan.v1.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(sent)
