"""Cached input tokens: read from the reply, priced at the cached rate, held at the full one."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx2
import jsonschema
import pytest

import derail_sdk
from conftest import EXAMPLE_FAULT, Project
from derail_sdk import config, plan
from derail_sdk import provider_openai as table
from derail_sdk.errors import ConfigError
from fakes.openai_server import FIXTURES, FakeOpenAI
from test_http_gate import BODY, URL, Budget, gated, install, make_plan

Fake = Callable[..., FakeOpenAI]
__all__ = ["gated"]  # the fixture, used by name

# Dollars per million tokens chosen so the costs below are whole by hand.
TEAM = table.Rates(input=1000.0, output=2000.0, cached_input=250.0)


@pytest.mark.design("cached.read_from_usage")
@pytest.mark.parametrize(
    ("fixture", "tokens_in", "cached"),
    [
        ("chat_answer_cached.json", 2048, 1536),  # usage.prompt_tokens_details
        ("chat_stream_usage_cached.sse", 2048, 2048),  # a Chat stream's last chunk
        ("response_stream_cached.sse", 2048, 1024),  # usage.input_tokens_details, streamed
        ("chat_answer.json", 19, 0),  # a cached count of 0
        ("response_text.json", 36, 0),
        ("chat_stream_usage.sse", 9, 0),  # no details block at all
    ],
)
def test_the_cached_count_is_read_from_each_kind_of_reply(
    fixture: str, tokens_in: int, cached: int
) -> None:
    used = table.usage((FIXTURES / fixture).read_bytes())
    assert (used.input, used.cached, used.known) == (tokens_in, cached, True)


@pytest.mark.design("cached.responses_block")
def test_a_responses_reply_names_its_block_input_tokens_details() -> None:
    body = {"usage": {"input_tokens": 100, "output_tokens": 5}}
    body["usage"]["input_tokens_details"] = {"cached_tokens": 64}  # type: ignore[index]
    assert table.usage(json.dumps(body).encode()).cached == 64


@pytest.mark.design("cached.bad_count_is_zero")
@pytest.mark.parametrize("value", [101, -1, 1.5, "64", True, None, 2_000_000_000])
def test_a_cached_count_that_cannot_be_trusted_is_read_as_none(value: object) -> None:
    usage = {"prompt_tokens": 100, "completion_tokens": 5}
    usage["prompt_tokens_details"] = {"cached_tokens": value}  # type: ignore[assignment]
    used = table.usage(json.dumps({"usage": usage}).encode())
    assert (used.known, used.input, used.cached) == (True, 100, 0)


def test_a_details_block_that_is_not_a_table_is_no_count() -> None:
    usage = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": [1]}
    assert table.usage(json.dumps({"usage": usage}).encode()).cached == 0


@pytest.mark.design("cached.cost_by_hand")
@pytest.mark.parametrize(
    ("rates", "dollars"),
    [
        # 512 full at $1000 + 1536 cached at $250 + 10 out at $2000, per million tokens.
        (TEAM, 0.512 + 0.384 + 0.02),
        # No cached rate: every input token at the full rate.
        (table.Rates(1000.0, 2000.0), 2.048 + 0.02),
        # A cached rate of 0 is a price, not a missing one.
        (table.Rates(1000.0, 2000.0, 0.0), 0.512 + 0.02),
    ],
    ids=["cached rate", "no cached rate", "free cache"],
)
def test_cached_tokens_cost_the_cached_rate(rates: table.Rates, dollars: float) -> None:
    assert table.cost(2048, 10, rates, cached=1536) == pytest.approx(dollars)


@pytest.mark.design("cached.builtin_rates")
def test_the_built_in_table_has_openais_cached_rates() -> None:
    # OpenAI's pricing page, standard tier, checked on 7 November 2026.
    assert table.PRICES["gpt-4o"] == table.Rates(2.50, 10.00, 1.25)
    assert table.PRICES["gpt-4o-mini"] == table.Rates(0.15, 0.60, 0.075)
    assert table.PRICES["gpt-4.1"] == table.Rates(2.00, 8.00, 0.50)
    assert table.PRICES["gpt-4.1-mini"] == table.Rates(0.40, 1.60, 0.10)
    assert table.PRICES["gpt-4.1-nano"] == table.Rates(0.10, 0.40, 0.025)
    assert table.PRICES["text-embedding-3-small"].cached_input is None


@pytest.mark.design("cached.reservation_full_rate")
def test_the_reservation_holds_every_input_token_at_the_full_rate(gated: None) -> None:
    budget = Budget(limit=1000.0)
    sink, _ = install(make_plan(budget=1000.0, prices=(("gpt-4o-mini", TEAM),)), budget)
    body = {**BODY, "max_tokens": 10}
    sent = json.dumps(body).encode()
    reply = {"choices": [], "usage": {"prompt_tokens": 2048, "completion_tokens": 10}}
    reply["usage"]["prompt_tokens_details"] = {"cached_tokens": 1536}  # type: ignore[index]
    held: list[float] = []
    real = budget.ledger.reserve

    def watch(usd: float, *rest: float) -> object:
        held.append(usd)
        return real(usd, *rest)

    budget.ledger.reserve = watch  # type: ignore[method-assign, assignment]
    answer = httpx2.MockTransport(lambda request: httpx2.Response(200, json=reply))
    httpx2.Client(transport=answer).post(URL, content=sent)
    # The hold: every byte of the body as a quarter token, at $1000, plus 10 out at $2000.
    assert held == [pytest.approx((len(sent) // 4 * 1000 + 10 * 2000) / 1e6)]
    assert sink.lines[0]["cost_usd"] == pytest.approx(0.512 + 0.384 + 0.02)
    assert sink.lines[0]["tokens"] == {"input": 2048, "output": 10, "cached": 1536}


def agent_on(project: Project, function: str, extra: str = "") -> None:
    project.edit('function = "acme.agent:answer"', f'function = "{function}"\nruns = 1{extra}')
    project.edit(EXAMPLE_FAULT, "")


@pytest.mark.design("cached.end_to_end")
@pytest.mark.parametrize(
    ("price", "dollars"),
    [
        ("{input = 1000, cached_input = 250, output = 2000}", 0.916),
        ("{input = 1000, output = 2000}", 2.068),  # the team gave no cached rate
    ],
    ids=["team cached rate", "no cached rate"],
)
def test_a_run_records_the_cached_count_and_the_cost_column_agrees(
    project: Project, fake_openai: Fake, price: str, dollars: float
) -> None:
    agent_on(project, "acme.rag:answer_if_asked", "\nmax_cost_usd = 1000")
    project.edit("[[test]]\n", f"[agent.prices]\ngpt-4o-mini = {price}\n\n[[test]]\n")
    result = project.test(fake_openai(["chat_answer_cached"]))
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["tokens"] == {"input": 2048, "output": 10, "cached": 1536}
    assert line["cost_usd"] == pytest.approx(dollars)
    assert result.summaries()[0]["cost_usd"] == pytest.approx(dollars)
    assert f"model calls cost ${dollars:.4f} of a budget of $1000" in result.out
    row = next(line for line in result.out.splitlines() if line.startswith("order-status"))
    assert f"{dollars:.5f}" in row  # the cost column


@pytest.mark.design("cached.end_to_end_builtin")
def test_a_stream_priced_from_the_built_in_table_uses_its_cached_rate(
    project: Project, fake_openai: Fake
) -> None:
    agent_on(project, "acme.rag:answer_streamed")
    result = project.test(fake_openai(["chat_stream_usage_cached"]))
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["tokens"] == {"input": 2048, "output": 1, "cached": 2048}
    # gpt-4o-mini: 2048 cached at $0.075 and 1 out at $0.60, per million tokens.
    assert line["cost_usd"] == pytest.approx((2048 * 0.075 + 0.60) / 1e6)
    schema = json.loads(
        (Path(derail_sdk.__file__).parent / "schemas" / "record.v1.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(line)


def priced(tmp_path: Path, entry: str) -> config.Config:
    text = (Path(__file__).parent / "example" / "derail.toml").read_text()
    path = tmp_path / "derail.toml"
    path.write_text(text.replace("[[test]]\n", f"[agent.prices]\nacme = {entry}\n\n[[test]]\n", 1))
    return config.load(path)


@pytest.mark.design("cached.setting")
def test_a_team_price_takes_an_optional_cached_rate(tmp_path: Path) -> None:
    with_rate = priced(tmp_path, "{input = 0.6, cached_input = 0.3, output = 0.7}")
    assert with_rate.agent.prices == (("acme", table.Rates(0.6, 0.7, 0.3)),)
    without = priced(tmp_path, "{input = 0.6, output = 0.7}")
    assert without.agent.prices == (("acme", table.Rates(0.6, 0.7, None)),)


@pytest.mark.design("cached.setting_checked")
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("-1", "cached_input: expected dollars per million tokens"),
        ('"x"', "cached_input: expected dollars per million tokens"),
        ("true", "cached_input: expected dollars per million tokens"),
        ("nan", "cached_input: expected dollars per million tokens"),
        ("0.7", "cached_input: 0.7 is above input, 0.6"),
    ],
)
def test_a_bad_cached_rate_is_rejected_with_its_table_and_key(
    tmp_path: Path, value: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message) as caught:
        priced(tmp_path, f"{{input = 0.6, cached_input = {value}, output = 0.7}}")
    assert "[agent].prices acme" in str(caught.value)


@pytest.mark.design("cached.plan_round_trip")
def test_the_cached_rate_survives_the_plan_and_fits_its_schema(tmp_path: Path) -> None:
    cfg = priced(tmp_path, "{input = 0.6, cached_input = 0.3, output = 0.7}")
    row = plan.rows(cfg)[0]
    sent = plan.plan_for(cfg, row, "run-1").to_json()
    budget = sent["budget"]
    assert isinstance(budget, dict)
    assert budget["prices"] == {"acme": {"input": 0.6, "output": 0.7, "cached_input": 0.3}}
    assert plan.Plan.from_json(sent).prices == (("acme", table.Rates(0.6, 0.7, 0.3)),)
    schema = json.loads((Path(derail_sdk.__file__).parent / "schemas" / "plan.v1.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(sent)
    plain = plan.plan_for(priced(tmp_path, "{input = 0.6, output = 0.7}"), row, "run-1")
    assert plain.to_json()["budget"]["prices"] == {"acme": {"input": 0.6, "output": 0.7}}  # type: ignore[index]
