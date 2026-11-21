"""Service tiers: a call is held at the tier its request names, else the file's service_tier,
else the default tier, and settled at the tier its reply says. The request is never changed."""

import json
from pathlib import Path
from typing import Any, cast

import httpx2
import jsonschema
import pytest

import config_cases
import scrimmage
from scrimmage import config, plan, prices
from scrimmage import provider_openai as table
from scrimmage.errors import ConfigError
from scrimmage.prices import Rates
from test_http_gate import BODY, URL, Budget, gated, install, make_plan

__all__ = ["gated"]  # the fixture, used by name

# Dollars per million tokens, chosen so the costs below are whole by hand.
DEFAULT = Rates(1000.0, 2000.0)
FLEX = Rates(500.0, 1000.0)
PRIORITY = Rates(2000.0, 4000.0)
TEAM = Rates(1000.0, 2000.0, tiers=(("flex", FLEX), ("priority", PRIORITY)))
USAGE = {"prompt_tokens": 1000, "completion_tokens": 10}


def call(
    request: dict[str, Any], *, tier: str | None = None, setting: str | None = None
) -> tuple[list[float], float, bytes, bytes]:
    """One call: what was held, the cost on the line, the bytes sent and the bytes at the wire."""
    budget = Budget(limit=1000.0)
    sink, _ = install(
        make_plan(budget=1000.0, prices=(("gpt-4o-mini", TEAM),), service_tier=setting), budget
    )
    held: list[float] = []
    real = budget.ledger.reserve

    def watch(usd: float, *rest: Any) -> object:
        held.append(usd)
        return real(usd, *rest)

    budget.ledger.reserve = watch  # type: ignore[method-assign, assignment]
    seen: list[bytes] = []

    def answer(sent: httpx2.Request) -> httpx2.Response:
        seen.append(sent.content)
        reply: dict[str, Any] = {"choices": [], "usage": USAGE}
        if tier is not None:
            reply["service_tier"] = tier
        return httpx2.Response(200, json=reply)

    body = json.dumps({**request, "max_tokens": 10}).encode()
    httpx2.Client(transport=httpx2.MockTransport(answer)).post(URL, content=body)
    return held, cast(float, sink.lines[0]["cost_usd"]), body, seen[0]


def held_at(rates: Rates, body: bytes) -> float:
    return (len(body) // 4 * rates.input + 10 * rates.output) / 1e6


@pytest.mark.design("tier.settle")
def test_a_reply_that_says_flex_settles_at_the_flex_rate(gated: None) -> None:
    _, cost, _, _ = call(BODY, tier="flex")
    assert cost == pytest.approx((1000 * 500 + 10 * 1000) / 1e6)


@pytest.mark.design("tier.settle")
@pytest.mark.parametrize("tier", ["default", "auto", "scale", None])
def test_a_reply_on_the_default_or_an_unlisted_tier_settles_at_the_model_rate(
    gated: None, tier: str | None
) -> None:
    _, cost, _, _ = call(BODY, tier=tier)
    assert cost == pytest.approx((1000 * 1000 + 10 * 2000) / 1e6)


@pytest.mark.design("tier.hold")
def test_a_request_that_names_priority_is_held_at_priority(gated: None) -> None:
    held, _, body, _ = call({**BODY, "service_tier": "priority"}, tier="priority")
    assert held == [pytest.approx(held_at(PRIORITY, body))]


@pytest.mark.design("tier.hold")
@pytest.mark.parametrize("named", [None, "auto"])
def test_a_request_with_no_tier_and_no_setting_is_held_at_the_default(
    gated: None, named: str | None
) -> None:
    request = BODY if named is None else {**BODY, "service_tier": named}  # auto names no tier
    held, _, body, _ = call(request)
    assert held == [pytest.approx(held_at(DEFAULT, body))]


@pytest.mark.design("tier.hold")
def test_the_services_tier_setting_holds_a_request_that_names_none(gated: None) -> None:
    held, _, body, _ = call(BODY, setting="priority")
    assert held == [pytest.approx(held_at(PRIORITY, body))]


@pytest.mark.design("tier.hold")
def test_the_requests_own_tier_comes_before_the_services_setting(gated: None) -> None:
    held, _, body, _ = call({**BODY, "service_tier": "flex"}, setting="priority")
    assert held == [pytest.approx(held_at(FLEX, body))]


@pytest.mark.design("tier.request_unchanged")
@pytest.mark.parametrize("setting", [None, "priority"])
def test_the_request_bytes_are_never_changed(gated: None, setting: str | None) -> None:
    _, _, body, wire = call(BODY, tier="flex", setting=setting)
    assert wire == body
    assert b"service_tier" not in wire


@pytest.mark.design("tier.read_from_reply")
@pytest.mark.parametrize(
    "reply",
    [
        {"usage": USAGE, "service_tier": "flex"},  # Chat
        {"response": {"usage": {"input_tokens": 5, "output_tokens": 5}, "service_tier": "flex"}},
    ],
    ids=["chat", "responses"],
)
def test_the_tier_is_read_from_a_reply_and_a_streamed_one(reply: dict[str, Any]) -> None:
    assert table.usage(json.dumps(reply).encode()).tier == "flex"
    stream = f"data: {json.dumps(reply)}\n\ndata: [DONE]\n\n".encode()
    assert table.usage(stream).tier == "flex"
    assert table.usage(json.dumps({"usage": USAGE}).encode()).tier is None


@pytest.mark.design("tier.agent_prices_win")
def test_a_row_in_agent_prices_wins_whole_over_the_team_files_row() -> None:
    mine = Rates(1.0, 2.0, tiers=(("flex", Rates(0.5, 1.0)),))
    theirs = Rates(7.0, 7.0, tiers=(("flex", Rates(3.0, 3.0)), ("priority", Rates(9.0, 9.0))))
    file = prices.PriceFile("2026-11-20", "x", (("gpt-4.1", theirs),))
    held = dict(prices.in_order((("gpt-4.1", mine),), file))
    found = table.price("gpt-4.1", held)
    assert found == mine
    assert found is not None and found.at("priority") == mine  # no tier of the file's leaks in


TIERED = (
    config_cases.AGENT
    + 'service_tier = "priority"\n'
    + '\n[agent.prices."gpt-4o-mini"]\ninput = 1000.0\noutput = 2000.0\n'
    + '\n[agent.prices."gpt-4o-mini".tiers.flex]\ninput = 500.0\noutput = 1000.0\n'
    + config_cases.TEST
)


@pytest.mark.design("tier.setting")
def test_the_setting_and_a_tier_table_are_read_and_survive_the_plan(tmp_path: Path) -> None:
    (tmp_path / "scrimmage.toml").write_text(TIERED)
    cfg = config.load(tmp_path / "scrimmage.toml")
    assert cfg.agent.service_tier == "priority"
    assert dict(cfg.agent.prices)["gpt-4o-mini"] == Rates(1000.0, 2000.0, tiers=(("flex", FLEX),))
    sent = plan.plan_for(cfg, plan.rows(cfg)[0], "run-1").to_json()
    again = plan.Plan.from_json(sent)
    assert again.service_tier == "priority"
    assert dict(again.prices)["gpt-4o-mini"].at("flex") == FLEX
    schema = json.loads((Path(scrimmage.__file__).parent / "schemas" / "plan.v1.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(sent)


@pytest.mark.design("tier.setting")
def test_a_bad_tier_table_is_rejected_with_its_table_and_key(tmp_path: Path) -> None:
    bad = TIERED.replace("tiers.flex]\ninput = 500.0", "tiers.flex]\nspeed = 5\ninput = 500.0")
    (tmp_path / "scrimmage.toml").write_text(bad)
    with pytest.raises(ConfigError, match=r"tiers flex speed: unknown setting"):
        config.load(tmp_path / "scrimmage.toml")
