"""A gateway that says what a call cost: OpenRouter's usage.cost, the LiteLLM proxy's header."""

import json
from typing import Any, cast

import httpx2
import pytest

from scrimmage import http_gate, state
from scrimmage import provider_openai as table
from scrimmage.budget import Ledger
from scrimmage.errors import ScrimmageRefused
from test_http_gate import BODY, URL, Budget, Sink, gated, install, make_plan

__all__ = ["gated"]  # the fixture, used by name

COST_HEADER = "x-litellm-response-cost"
# gpt-4o-mini at $0.15 in and $0.60 out per million: 1000 in and 10 out cost this by the table.
BY_TABLE = (1000 * 0.15 + 10 * 0.60) / 1e6


def call(
    reply: dict[str, Any], headers: dict[str, str] | None = None, model: str = "gpt-4o-mini"
) -> tuple[dict[str, object], httpx2.Request]:
    sink, _ = install(make_plan(budget=1000.0), Budget(limit=1000.0))
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=reply, headers=headers)

    body = json.dumps({**BODY, "model": model, "max_tokens": 10}).encode()
    httpx2.Client(transport=httpx2.MockTransport(answer)).post(URL, content=body)
    assert seen[0].content == body
    return sink.lines[0], seen[0]


USAGE = {"prompt_tokens": 1000, "completion_tokens": 10}


@pytest.mark.design("gateway.cost")
def test_an_openrouter_reply_settles_at_its_usage_cost(gated: None) -> None:
    line, _ = call({"choices": [], "usage": {**USAGE, "cost": 0.0123}})
    assert line["cost_usd"] == pytest.approx(0.0123)
    assert line["cost_estimated"] is False


@pytest.mark.design("gateway.cost")
def test_a_litellm_header_settles_at_its_cost(gated: None) -> None:
    line, _ = call({"choices": [], "usage": USAGE}, {COST_HEADER: "0.0456"})
    assert line["cost_usd"] == pytest.approx(0.0456)


@pytest.mark.design("gateway.cost")
def test_the_request_is_not_changed_to_ask_for_a_cost(gated: None) -> None:
    _, plain = call({"choices": [], "usage": USAGE})
    assert plain.headers.get(COST_HEADER) is None
    assert b"cost" not in plain.content and b"include" not in plain.content


@pytest.mark.design("gateway.cost")
@pytest.mark.parametrize("bad", ["x", "-1", "nan", "inf", ""])
def test_a_cost_that_is_not_a_sensible_number_is_ignored(gated: None, bad: str) -> None:
    line, _ = call({"choices": [], "usage": USAGE}, {COST_HEADER: bad})
    assert line["cost_usd"] == pytest.approx(BY_TABLE)


@pytest.mark.design("gateway.cost")
def test_a_reply_with_no_cost_settles_at_the_table(gated: None) -> None:
    line, _ = call({"choices": [], "usage": USAGE})
    assert line["cost_usd"] == pytest.approx(BY_TABLE)


@pytest.mark.design("gateway.cost")
def test_a_cost_is_read_from_a_streamed_reply_and_a_responses_reply() -> None:
    used = {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.5}
    stream = f"data: {json.dumps({'usage': used})}\n\ndata: [DONE]\n\n".encode()
    assert table.usage(stream).cost == 0.5
    responses = {"response": {"usage": {"input_tokens": 5, "output_tokens": 5, "cost": 0.25}}}
    assert table.usage(json.dumps(responses).encode()).cost == 0.25


@pytest.mark.design("gateway.cost")
def test_a_model_with_no_price_is_still_refused_before_the_call(gated: None) -> None:
    sink, _ = install(make_plan(budget=1000.0), Budget(limit=1000.0))
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json={"usage": {**USAGE, "cost": 0.01}})

    body = json.dumps({**BODY, "model": "mystery-model"}).encode()
    with pytest.raises(ScrimmageRefused):
        httpx2.Client(transport=httpx2.MockTransport(answer)).post(URL, content=body)
    assert sent == []
    assert cast(str, sink.lines[0]["reason"]) == "no_price"


class Settling(Sink):
    """Hands each settle straight to the ledger, as the runner does."""

    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

    def put_settle(self, reservation: int, usd: float) -> None:
        super().put_settle(reservation, usd)
        self.ledger.settle(reservation, usd)


@pytest.mark.design("gateway.cost_never_lowers")
@pytest.mark.parametrize(
    ("cost", "header"),
    [(0, None), (1e-9, None), (None, "0.0"), (None, "1e-9")],
    ids=["usage.cost 0", "usage.cost 1e-9", "header 0.0", "header 1e-9"],
)
def test_a_gateway_that_states_a_tiny_cost_cannot_turn_the_budget_off(
    gated: None, cost: float | None, header: str | None
) -> None:
    """50 gpt-4.1 calls of 100k in and 10k out cost $0.28 each by the table, against a $0.10
    budget. The real ledger, settled as the runner settles, refuses once spent passes it."""
    budget = Budget(limit=0.10)
    sink = Settling(budget.ledger)
    run = state.RunState(make_plan(budget=0.10), sink, [])
    state.install(run)
    http_gate.patch(httpx2, http_gate.Context(run, budget, sink, list[str]().append))
    usage: dict[str, Any] = {"prompt_tokens": 100_000, "completion_tokens": 10_000}
    if cost is not None:
        usage["cost"] = cost
    headers = {COST_HEADER: header} if header is not None else None
    sent = json.dumps({"model": "gpt-4.1", "messages": [], "max_tokens": 10}).encode()
    send = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"usage": usage}, headers=headers)
        )
    ).post
    refused = 0
    for _ in range(50):
        try:
            send(URL, content=sent)
        except ScrimmageRefused:
            refused += 1
    table_cost = (100_000 * 2.00 + 10_000 * 8.00) / 1e6
    assert budget.ledger.spent == pytest.approx(table_cost)  # the first call, at the table's cost
    assert refused == 49
