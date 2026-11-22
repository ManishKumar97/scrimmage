"""Long-context prices: a call over `above` input tokens costs every token at the long rates,
in the settle and in the hold, in a price file and in [agent.prices]."""

import json
from pathlib import Path
from typing import Any, cast

import httpx2
import jsonschema
import pytest

import config_cases
import scrimmage
from scrimmage import cli, config, plan, prices
from scrimmage import provider_openai as table
from scrimmage.errors import ConfigError
from scrimmage.prices import Rates
from test_http_gate import BODY, URL, Budget, gated, install, make_plan

__all__ = ["gated"]  # the fixture, used by name

# Dollars per million tokens, chosen so the costs below are whole by hand.
LONG = Rates(2000.0, 3000.0, 1000.0)
FLEX_LONG = Rates(900.0, 1500.0)
ABOVE = 1000
MODEL = Rates(
    1000.0,
    2000.0,
    500.0,
    tiers=(("flex", Rates(500.0, 1000.0, above=ABOVE, long=FLEX_LONG)),),
    above=ABOVE,
    long=LONG,
)


def call(
    request: dict[str, Any], *, prompt: int, cached: int = 0, tier: str | None = None
) -> tuple[list[float], float]:
    """One call whose reply says it used `prompt` input tokens: what was held, the cost."""
    budget = Budget(limit=1000.0)
    sink, _ = install(make_plan(budget=1000.0, prices=(("gpt-4o-mini", MODEL),)), budget)
    held: list[float] = []
    real = budget.ledger.reserve

    def watch(usd: float, *rest: Any) -> object:
        held.append(usd)
        return real(usd, *rest)

    budget.ledger.reserve = watch  # type: ignore[method-assign, assignment]

    def answer(_: httpx2.Request) -> httpx2.Response:
        usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": 10}
        if cached:
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
        reply: dict[str, Any] = {"choices": [], "usage": usage}
        if tier is not None:
            reply["service_tier"] = tier
        return httpx2.Response(200, json=reply)

    body = json.dumps({**request, "max_tokens": 10}).encode()
    httpx2.Client(transport=httpx2.MockTransport(answer)).post(URL, content=body)
    return held, cast(float, sink.lines[0]["cost_usd"])


@pytest.mark.design("long.settle")
def test_a_reply_over_the_threshold_settles_every_token_at_the_long_rates(gated: None) -> None:
    _, cost = call(BODY, prompt=ABOVE + 1, cached=100)
    # The whole call, not the tokens past the threshold: 901 plain, 100 cached, 10 output.
    assert cost == pytest.approx((901 * 2000 + 100 * 1000 + 10 * 3000) / 1e6)


@pytest.mark.design("long.settle")
def test_a_reply_at_the_threshold_settles_at_the_normal_rates(gated: None) -> None:
    _, cost = call(BODY, prompt=ABOVE, cached=100)
    assert cost == pytest.approx((900 * 1000 + 100 * 500 + 10 * 2000) / 1e6)


@pytest.mark.design("long.settle")
def test_a_tier_with_its_own_long_rates_settles_at_them(gated: None) -> None:
    _, short = call(BODY, prompt=ABOVE, tier="flex")
    _, long = call(BODY, prompt=ABOVE + 1, tier="flex")
    assert short == pytest.approx((ABOVE * 500 + 10 * 1000) / 1e6)
    assert long == pytest.approx(((ABOVE + 1) * 900 + 10 * 1500) / 1e6)


@pytest.mark.design("long.settle")
def test_a_tier_with_no_long_rates_costs_its_own_rates_at_any_length(gated: None) -> None:
    flat = Rates(1000.0, 2000.0, tiers=(("priority", Rates(4000.0, 8000.0)),), above=5, long=LONG)
    budget = Budget(limit=1000.0)
    sink, _ = install(make_plan(budget=1000.0, prices=(("gpt-4o-mini", flat),)), budget)

    def answer(_: httpx2.Request) -> httpx2.Response:
        usage = {"prompt_tokens": 1000, "completion_tokens": 10}
        return httpx2.Response(
            200, json={"choices": [], "usage": usage, "service_tier": "priority"}
        )

    httpx2.Client(transport=httpx2.MockTransport(answer)).post(
        URL, content=json.dumps(BODY).encode()
    )
    assert sink.lines[0]["cost_usd"] == pytest.approx((1000 * 4000 + 10 * 8000) / 1e6)


def estimated(request: dict[str, Any]) -> int:
    return len(json.dumps({**request, "max_tokens": 10}).encode()) // 4


@pytest.mark.design("long.hold")
def test_a_request_whose_estimate_is_over_the_threshold_is_held_at_the_long_rates(
    gated: None,
) -> None:
    tokens = estimated(BODY)
    low = Rates(1000.0, 2000.0, above=tokens - 1, long=LONG)
    held, _ = hold(low)
    assert held == [pytest.approx((tokens * 2000 + 10 * 3000) / 1e6)]


@pytest.mark.design("long.hold")
def test_a_request_whose_estimate_is_at_the_threshold_is_held_at_the_normal_rates(
    gated: None,
) -> None:
    tokens = estimated(BODY)
    held, _ = hold(Rates(1000.0, 2000.0, above=tokens, long=LONG))
    assert held == [pytest.approx((tokens * 1000 + 10 * 2000) / 1e6)]


def hold(rates: Rates) -> tuple[list[float], float]:
    budget = Budget(limit=1000.0)
    sink, _ = install(make_plan(budget=1000.0, prices=(("gpt-4o-mini", rates),)), budget)
    held: list[float] = []
    real = budget.ledger.reserve

    def watch(usd: float, *rest: Any) -> object:
        held.append(usd)
        return real(usd, *rest)

    budget.ledger.reserve = watch  # type: ignore[method-assign, assignment]
    body = json.dumps({**BODY, "max_tokens": 10}).encode()
    httpx2.Client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json={"choices": []}))
    ).post(URL, content=body)
    return held, cast(float, sink.lines[0]["cost_usd"])


HEAD = 'as_of = "2026-11-20"\nsource = "https://example.test/prices"\n'
MODEL_TABLE = '[models."gpt-5.5"]\ninput = 5\noutput = 30\ncached_input = 0.5\n'
LONG_TABLE = '[models."gpt-5.5".long]\n'


def long_file(long: str, base: str = MODEL_TABLE) -> str:
    return HEAD + base + LONG_TABLE + long


@pytest.mark.design("long.format")
def test_a_price_file_reads_a_long_table_and_a_tier_long_table() -> None:
    text = long_file("above = 272000\ninput = 10\noutput = 45\ncached_input = 1\n")
    text += '[models."gpt-5.5".tiers.flex]\ninput = 2.5\noutput = 15\n'
    text += '[models."gpt-5.5".tiers.flex.long]\nabove = 272000\ninput = 5\noutput = 22.5\n'
    rates = dict(prices.parse(text, "the file").models)["gpt-5.5"]
    assert rates.above == 272000
    assert rates.long == Rates(10.0, 45.0, 1.0)
    flex = rates.at("flex")
    assert flex.above == 272000 and flex.long == Rates(5.0, 22.5)
    assert rates.sized(272000) is rates
    assert rates.sized(272001) is rates.long


BAD_LONG = {
    "no above": ("input = 10\noutput = 45\n", "long above: expected a whole number"),
    "above 0": ("above = 0\ninput = 10\noutput = 45\n", "long above: expected a whole number"),
    "above negative": ("above = -5\ninput = 10\noutput = 45\n", "long above: expected a whole"),
    "above float": ("above = 272000.0\ninput = 10\noutput = 45\n", "long above: expected a whole"),
    "above text": ('above = "272K"\ninput = 10\noutput = 45\n', "long above: expected a whole"),
    "above bool": ("above = true\ninput = 10\noutput = 45\n", "long above: expected a whole"),
    "input below base": ("above = 9\ninput = 4\noutput = 45\n", "long input: 4 is below the 5"),
    "output below base": (
        "above = 9\ninput = 10\noutput = 29\n",
        "long output: 29 is below the 30",
    ),
    "cached below base": (
        "above = 9\ninput = 10\noutput = 45\ncached_input = 0.4\n",
        "long cached_input: 0.4 is below the 0.5",
    ),
    "cached left out costs input": (
        # No long cached rate costs the long input, 0.3 here, which is under the base 0.5.
        "above = 9\ninput = 0.3\noutput = 45\n",
        "long input: 0.3 is below the 5",
    ),
    "no input": ("above = 9\noutput = 45\n", "long input: expected dollars"),
    "unknown key": ("above = 9\ninput = 10\noutput = 45\nspeed = 3\n", "long speed: unknown"),
    "nested long": (
        'above = 9\ninput = 10\noutput = 45\n[models."gpt-5.5".long.long]\nabove = 9\n'
        "input = 11\noutput = 46\n",
        "long long: unknown setting",
    ),
    "tiers in long": (
        'above = 9\ninput = 10\noutput = 45\n[models."gpt-5.5".long.tiers.flex]\ninput = 1\n'
        "output = 1\n",
        "long tiers: unknown setting",
    ),
}


@pytest.mark.design("long.validated")
@pytest.mark.parametrize("name", BAD_LONG)
def test_a_bad_long_table_is_a_config_error_naming_the_line(name: str) -> None:
    body, message = BAD_LONG[name]
    with pytest.raises(ConfigError, match=message) as caught:
        prices.parse(long_file(body), "the file")
    assert "(line " in caught.value.problems[0]


@pytest.mark.design("long.validated")
def test_above_is_not_a_key_of_a_model_or_a_tier() -> None:
    with pytest.raises(ConfigError, match=r"models gpt-5\.5 above: unknown setting"):
        prices.parse(HEAD + MODEL_TABLE + "above = 9\n", "the file")
    tier = MODEL_TABLE + '[models."gpt-5.5".tiers.flex]\ninput = 1\noutput = 1\nabove = 9\n'
    with pytest.raises(ConfigError, match=r"tiers flex above: unknown setting"):
        prices.parse(HEAD + tier, "the file")


@pytest.mark.design("long.validated")
def test_a_tier_long_may_not_be_below_its_tier() -> None:
    tier = MODEL_TABLE + '[models."gpt-5.5".tiers.flex]\ninput = 2.5\noutput = 15\n'
    tier += '[models."gpt-5.5".tiers.flex.long]\nabove = 9\ninput = 2\noutput = 22\n'
    with pytest.raises(ConfigError, match=r"tiers flex long input: 2 is below the 2\.5"):
        prices.parse(HEAD + tier, "the file")


@pytest.mark.design("long.validated")
def test_a_long_table_cannot_get_under_the_floor_of_a_price_file() -> None:
    # The long rates are at least the base rates, so the floor on the base holds for them.
    low = '[models."gpt-5.5"]\ninput = 0.0005\noutput = 0.001\n'
    with pytest.raises(ConfigError, match=r"input: 0\.0005 is below 0\.001"):
        prices.parse(long_file("above = 9\ninput = 0.0005\noutput = 0.001\n", low), "the file")
    at = '[models."gpt-5.5"]\ninput = 0.001\noutput = 0.001\n'
    with pytest.raises(ConfigError, match=r"long input: 0\.0005 is below the 0\.001"):
        prices.parse(long_file("above = 9\ninput = 0.0005\noutput = 0.001\n", at), "the file")


@pytest.mark.design("long.validated")
def test_a_long_table_at_the_base_rates_is_accepted() -> None:
    body = "above = 9\ninput = 5\noutput = 30\ncached_input = 0.5\n"
    found = dict(prices.parse(long_file(body), "the file").models)["gpt-5.5"]
    assert found.long == Rates(5.0, 30.0, 0.5)


AGENT_LONG = (
    config_cases.AGENT
    + '\n[agent.prices."gpt-5.5"]\ninput = 5.0\noutput = 30.0\n'
    + '\n[agent.prices."gpt-5.5".long]\nabove = 272000\ninput = 10.0\noutput = 45.0\n'
    + '\n[agent.prices."gpt-5.5".tiers.flex]\ninput = 2.5\noutput = 15.0\n'
    + '\n[agent.prices."gpt-5.5".tiers.flex.long]\nabove = 272000\ninput = 5.0\noutput = 22.5\n'
    + config_cases.TEST
)


@pytest.mark.design("long.agent_prices")
def test_agent_prices_take_a_long_table_and_it_survives_the_plan(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(AGENT_LONG)
    cfg = config.load(tmp_path / "scrimmage.toml")
    rates = dict(cfg.agent.prices)["gpt-5.5"]
    assert rates.sized(272001) == Rates(10.0, 45.0)
    sent = plan.plan_for(cfg, plan.rows(cfg)[0], "run-1").to_json()
    again = dict(plan.Plan.from_json(sent).prices)["gpt-5.5"]
    assert again == rates
    assert again.at("flex").sized(272001) == Rates(5.0, 22.5)
    root = Path(scrimmage.__file__).parent / "schemas"
    schema = json.loads((root / "plan.v1.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(sent)


@pytest.mark.design("long.agent_prices")
def test_the_config_schema_accepts_a_long_table_and_refuses_a_bad_one() -> None:
    root = Path(scrimmage.__file__).parent / "schemas"
    schema = json.loads((root / "scrimmage-toml.v1.json").read_text())
    price = jsonschema.Draft202012Validator({"$ref": "#/$defs/price", "$defs": schema["$defs"]})
    long = {"above": 9, "input": 2, "output": 3}
    price.validate({"input": 1, "output": 2, "long": long})
    price.validate(
        {"input": 1, "output": 2, "tiers": {"flex": {"input": 1, "output": 2, "long": long}}}
    )
    for bad in ({"input": 2, "output": 3}, {"above": 0, "input": 2, "output": 3}):  # no above, 0
        with pytest.raises(jsonschema.ValidationError):
            price.validate({"input": 1, "output": 2, "long": bad})


@pytest.mark.design("long.agent_prices")
def test_a_long_below_base_in_agent_prices_stops_the_load(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(AGENT_LONG.replace("input = 10.0", "input = 4.0"))
    with pytest.raises(ConfigError, match=r"long input: 4 is below the 5"):
        config.load(tmp_path / "scrimmage.toml")


@pytest.mark.design("long.agent_prices")
def test_check_json_shows_the_long_rates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "scrimmage.toml").write_text(AGENT_LONG)
    assert cli.main(["check", "--json", str(tmp_path / "scrimmage.toml")]) == 0
    doc = json.loads(capsys.readouterr().out)
    shown = doc["summary"]["prices"]["gpt-5.5"]
    assert shown["long"]["above"] == 272000 and shown["long"]["input"] == 10.0
    assert shown["tiers"]["flex"]["long"]["output"] == 22.5


@pytest.mark.design("long.cost")
def test_cost_and_most_cost_use_the_long_rates_only_over_the_threshold() -> None:
    assert table.cost(ABOVE, 10, MODEL) == pytest.approx((ABOVE * 1000 + 10 * 2000) / 1e6)
    assert table.cost(ABOVE + 1, 10, MODEL) == pytest.approx(((ABOVE + 1) * 2000 + 10 * 3000) / 1e6)
    assert table.most_cost(ABOVE, 10, MODEL, responses=False) == pytest.approx(
        (ABOVE * 1000 + 10 * 2000) / 1e6
    )
    assert table.most_cost(ABOVE + 1, 10, MODEL, responses=False) == pytest.approx(
        ((ABOVE + 1) * 2000 + 10 * 3000) / 1e6
    )


@pytest.mark.design("long.update_diff")
def test_prices_update_prints_a_changed_long_rate() -> None:
    from scrimmage import prices_update

    plain = Rates(1000.0, 2000.0, 500.0)
    old = prices.PriceFile("2026-11-20", "x", (("gpt-5.5", plain),))
    longer = Rates(1000.0, 2000.0, 500.0, above=ABOVE, long=Rates(2000.0, 3000.0, 1000.0))
    again = Rates(1000.0, 2000.0, 500.0, above=ABOVE, long=Rates(2500.0, 3000.0, 1000.0))
    added = prices_update.diff(old, prices.PriceFile("2026-11-21", "x", (("gpt-5.5", longer),)))
    assert added == [
        "changed gpt-5.5: above added at 1000; long.cached_input added at 1000; "
        "long.input added at 2000; long.output added at 3000"
    ]
    moved = prices_update.diff(
        prices.PriceFile("2026-11-21", "x", (("gpt-5.5", longer),)),
        prices.PriceFile("2026-11-22", "x", (("gpt-5.5", again),)),
    )
    assert moved == ["changed gpt-5.5: long.input 2000 to 2500"]


@pytest.mark.design("long.update_diff")
def test_prices_update_prints_the_threshold_as_a_whole_number() -> None:
    from scrimmage import prices_update

    big = Rates(1000.0, 2000.0, above=1000001, long=Rates(2000.0, 3000.0))
    bigger = Rates(1000.0, 2000.0, above=999999999, long=Rates(2000.0, 3000.0))
    old = prices.PriceFile("2026-11-20", "x", (("gpt-5.5", big),))
    new = prices.PriceFile("2026-11-21", "x", (("gpt-5.5", bigger),))
    assert prices_update.diff(old, new) == ["changed gpt-5.5: above 1000001 to 999999999"]
    assert (
        prices_update.fmt(big)
        == "input 1000, output 2000, above 1000001, long.input 2000, long.output 3000"
    )
