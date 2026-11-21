"""The budget: reservations before each call, real costs after it, one ledger per test."""

import json
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest

import scrimmage
from conftest import ACME, EXAMPLE_FAULT, Project, TimeLimit
from fakes.openai_server import FIXTURES, FakeOpenAI
from scrimmage import http_gate, state
from scrimmage import provider_openai as table
from scrimmage.budget import Ledger, Shortfall
from scrimmage.client import ControlClient, ControlError
from scrimmage.control import ControlServer
from scrimmage.records import RecordStore
from test_http_gate import URL, Budget, Sink, gated, make_plan

__all__ = ["gated"]  # the fixture, used by name

Fake = Callable[..., FakeOpenAI]
TOKEN = "e" * 64
TOOL_FAULT = EXAMPLE_FAULT
RAG = 'function = "acme.rag:answer"\nruns = 1'


def rag(project: Project, extra: str) -> None:
    project.edit('function = "acme.agent:answer"', RAG + extra)
    project.edit(TOOL_FAULT, "")


@pytest.mark.design("budget.reservations_race")
def test_four_calls_at_79_of_80_cents_reserve_one_and_refuse_three() -> None:
    ledger = Ledger(0.80)
    first = ledger.reserve(0.79)
    assert isinstance(first, int)
    ledger.settle(first, 0.79)
    results: list[int | Shortfall] = []
    start = threading.Barrier(4)

    def call() -> None:
        start.wait()
        results.append(ledger.reserve(0.01))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(r, int) for r in results) == 1


@pytest.mark.design("budget.real_cost_replaces_reservation")
def test_the_real_cost_replaces_the_reservation() -> None:
    ledger = Ledger(1.00)
    held = ledger.reserve(0.60)
    waiting = ledger.reserve(0.60)
    assert isinstance(held, int) and isinstance(waiting, Shortfall)
    ledger.settle(held, 0.10)
    assert (round(ledger.spent, 2), ledger.reserved) == (0.10, 0.0)
    assert isinstance(ledger.reserve(0.60, ticket=waiting.ticket), int)


def test_no_limit_means_every_reservation_passes() -> None:
    assert isinstance(Ledger(None).reserve(1_000_000.0), int)


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlServer]:
    server = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=Ledger(0.80))
    server.start()
    server.open_run("run-1", "baseline", {"v": 1})
    yield server
    server.stop()


@pytest.mark.design("budget.ledger_over_protocol")
def test_the_ledger_works_through_the_signed_protocol(control: ControlServer) -> None:
    client = ControlClient(control.url, TOKEN)
    held = client.reserve("run-1", 0.79)
    assert held is not None
    payload = {"run": "run-1", "reservation": held, "usd": 0.79}
    assert client.request("POST", "/v1/budget/settle", payload)[0] == 200
    outcomes = [client.reserve("run-1", 0.01) for _ in range(2)]
    assert isinstance(outcomes[0], int) and isinstance(outcomes[1], Shortfall)
    assert control.ledger.spent == pytest.approx(0.79)


@pytest.mark.design("budget.settle_after_close")
def test_money_spent_counts_even_when_the_run_has_just_closed(control: ControlServer) -> None:
    client = ControlClient(control.url, TOKEN)
    held = client.reserve("run-1", 0.5)
    control.close_run("run-1")
    payload = {"run": "run-1", "reservation": held, "usd": 0.25}
    assert client.request("POST", "/v1/budget/settle", payload)[0] == 200
    assert control.ledger.spent == pytest.approx(0.25)
    assert control.ledger.reserved == pytest.approx(0.0)


def test_a_reservation_for_a_closed_run_is_an_error(control: ControlServer) -> None:
    client = ControlClient(control.url, TOKEN)
    control.close_run("run-1")
    with pytest.raises(ControlError, match="could not be checked"):
        client.reserve("run-1", 0.01)


@pytest.mark.parametrize(
    "payload",
    [{"run": "run-1"}, {"run": "run-1", "usd": "x"}, {"run": "run-1", "usd": -1}],
)
def test_bad_budget_requests_are_refused(
    control: ControlServer, payload: dict[str, object]
) -> None:
    client = ControlClient(control.url, TOKEN)
    assert client.request("POST", "/v1/budget/reserve", payload)[0] == 400
    assert client.request("POST", "/v1/budget/settle", {**payload, "reservation": "x"})[0] == 400
    assert (
        client.request("POST", "/v1/budget/settle", {"run": "r", "reservation": 1, "usd": -1})[0]
        == 400
    )


# Dollars per million tokens, chosen so a few tokens make whole cents.
PRICES = table.Rates(1_000_000.0 * 0.01, 1_000_000.0 * 0.02)  # 1 cent an input token, 2 an output


@pytest.mark.design("budget.costs_to_the_cent")
@pytest.mark.parametrize(
    ("fixture", "tokens_in", "tokens_out", "dollars"),
    [
        # By hand: tokens_in at $0.01 plus tokens_out at $0.02.
        ("chat_tool_call.json", 82, 17, 1.16),
        ("chat_answer.json", 19, 10, 0.39),
        ("embedding.json", 8, 0, 0.08),
        ("response_text.json", 36, 87, 2.10),
        ("chat_stream_usage.sse", 9, 1, 0.11),
        ("response_stream.sse", 37, 11, 0.59),
    ],
)
def test_fixed_replies_cost_what_a_hand_computed_table_says(
    fixture: str, tokens_in: int, tokens_out: int, dollars: float
) -> None:
    needs_output = fixture != "embedding.json"
    used = table.usage((FIXTURES / fixture).read_bytes(), output_required=needs_output)
    assert (used.input, used.output, used.known) == (tokens_in, tokens_out, True)
    assert table.cost(used.input, used.output, PRICES) >= 0
    assert round(table.cost(used.input, used.output, PRICES), 2) == dollars


def test_a_stream_with_no_counts_says_so() -> None:
    assert not table.usage((FIXTURES / "chat_stream.sse").read_bytes()).known


@pytest.mark.design("budget.stream_events_scanned")
def test_a_stream_with_events_and_pings_is_read_from_its_data_lines() -> None:
    body = (
        b": ping\n\nevent: response.completed\n"
        b'data: {"response": {"usage": {"input_tokens": 3, "output_tokens": 4}}}\n\n'
    )
    used = table.usage(body)
    assert (used.input, used.output, used.known) == (3, 4, True)


def test_a_line_separator_in_the_text_does_not_hide_the_counts() -> None:
    separator = "\u2028"
    body = (
        f'data: {{"note": "a{separator}b", "usage": '
        '{"prompt_tokens": 3, "completion_tokens": 4}}\n\n'
    ).encode()
    assert table.usage(body).known


def test_counts_nested_too_deep_to_read_are_not_counts() -> None:
    assert not table.usage(b"[" * 100_000).known


def test_a_reply_that_is_not_json_has_no_counts() -> None:
    assert not table.usage(b"<html>bad gateway</html>").known
    assert table.request_fields(b"not json") is None
    assert table.request_fields(b"[1]") is None


@pytest.mark.design("budget.price_lookup")
def test_prices_come_from_the_team_then_the_table_then_the_family() -> None:
    mini = table.Rates(0.15, 0.60, 0.075)
    assert table.price("gpt-4o-mini", {}) == mini
    assert table.price("gpt-4o-mini-2024-07-18", {}) == mini  # a dated snapshot
    team = table.Rates(9.0, 9.0)
    assert table.price("gpt-4o-mini", {"gpt-4o-mini": team}) == team  # whole, no cached rate
    llama = table.Rates(0.6, 0.6)
    assert table.price("acme-llama-70b", {"acme-llama-70b": llama}) == llama
    assert table.price("nobody-knows-me", {}) is None


@pytest.mark.design("budget.only_dated_snapshots")
@pytest.mark.parametrize(
    "model", ["gpt-4o-audio-preview", "gpt-4o-realtime-preview", "gpt-4o-mini-tts", "gpt-4o-2024"]
)
def test_a_variant_is_not_priced_like_its_family(model: str) -> None:
    assert table.price(model, {}) is None


def test_a_request_can_set_its_own_output_limit() -> None:
    assert table.max_output({"max_tokens": 50}) == 50
    assert table.max_output({"max_completion_tokens": 60, "max_tokens": 50}) == 60
    assert table.max_output({"max_output_tokens": 70}) == 70
    assert table.max_output({}) is None
    assert table.max_output({"max_tokens": True}) is None


@pytest.mark.design("budget.hold_per_choice")
@pytest.mark.parametrize(
    ("url", "fields", "output"),
    [
        (URL, {"max_tokens": 100, "n": 8}, 800),  # Chat bills the output of every choice
        (URL, {"n": 2}, 2 * 4096),  # with no limit, reserve_output_tokens per choice
        (URL, {"max_tokens": 100, "n": 128}, 12_800),  # the most the API takes
        (URL, {"max_tokens": 100, "n": 129}, 100),  # refused by the API, so never billed
        (URL, {"max_tokens": 100, "n": 0}, 100),
        (URL, {"max_tokens": 100, "n": 8.0}, 100),  # not an int: refused by the API
        (URL, {"max_tokens": 100, "n": True}, 100),
        (URL, {"max_tokens": 100, "n": None}, 100),
        # Responses has no n: max_output_tokens bounds the whole response.
        ("https://api.openai.com/v1/responses", {"max_output_tokens": 100, "n": 8}, 100),
    ],
)
def test_a_chat_hold_counts_the_output_of_every_choice(
    gated: None, url: str, fields: dict[str, object], output: int
) -> None:
    budget = Budget(limit=1000.0)
    run = state.RunState(make_plan(budget=1000.0), Sink(), [])
    state.install(run)
    http_gate.patch(httpx2, http_gate.Context(run, budget, Sink(), list[str]().append))
    held: list[float] = []
    reserve = budget.ledger.reserve

    def watch(usd: float, *rest: Any) -> int | Shortfall:
        held.append(usd)
        return reserve(usd, *rest)

    budget.ledger.reserve = watch  # type: ignore[method-assign]
    sent = json.dumps({"model": "gpt-4.1", "input": "hi", **fields}).encode()
    reply = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    answer = httpx2.MockTransport(lambda request: httpx2.Response(200, json=reply))
    httpx2.Client(transport=answer).post(url, content=sent)
    # gpt-4.1: $2 in and $8 out per million, the input estimated at 4 bytes a token.
    assert held == [pytest.approx((len(sent) // 4 * 2.00 + output * 8.00) / 1e6)]


@pytest.mark.design("budget.costs_reach_the_record")
@pytest.mark.slow
def test_a_runs_cost_is_summed_from_its_real_replies(project: Project, fake_openai: Fake) -> None:
    rag(project, "\nmax_cost_usd = 1000")
    project.edit(
        "[[test]]\n",
        "[agent.prices]\ngpt-4o-mini = {input = 10000, output = 20000}\n"
        "text-embedding-3-small = {input = 10000, output = 0}\n\n[[test]]\n",
    )
    result = project.test(fake_openai(ACME, embedding="embedding"))
    assert result.code == 0, result.out
    # By hand: the embedding, 8 tokens, $0.08; two tool replies, 82 in and 17 out, $1.16
    # each; the answer, 19 in and 10 out, $0.39.
    assert result.summaries()[0]["cost_usd"] == pytest.approx(2.79)
    assert "model calls cost $2.7900 of a budget of $1000" in result.out


@pytest.mark.design("budget.stops_the_test")
@pytest.mark.slow
def test_a_budget_that_cannot_cover_a_call_stops_the_test(
    project: Project, fake_openai: Fake
) -> None:
    rag(project, "\nmax_cost_usd = 0.00001")
    fake = FakeOpenAI(ACME, embedding="embedding")
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2, result.out
    assert fake.paths == ["/v1/embeddings"]  # the chat call, which cannot fit, never went
    assert {s["outcome"] for s in result.summaries()} == {"not reserved"}  # spent is below
    assert "a model call could not be reserved: it could cost up to" in result.out
    assert (
        "of a budget of $1e-05, so the test stopped" in result.out
        and "was reached" not in result.out
    )


@pytest.mark.design("budget.stop_with_parallel_runs")
@pytest.mark.parametrize(
    ("function", "extra", "fixtures", "message"),
    [
        ("acme.rag:answer", "max_cost_usd = 0.00001", {"embedding": "embedding"}, "reserved"),
        ("acme.rag:answer_priceless", "", {}, "[agent.prices]"),
        ("acme.rag:answer_odd_tool", "", {}, "a tool of type teleport"),
        ("acme.rag:answer_unseen", "", {}, "lookup_order"),
    ],
    ids=["budget", "no price", "unknown tool type", "unseen tool"],
)
@pytest.mark.slow
def test_a_stop_at_default_parallel_exits_2_not_3(
    project: Project, function: str, extra: str, fixtures: dict[str, Any], message: str
) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "{function}"\nruns = 8\nparallel = 4\n{extra}',
    )
    project.edit(TOOL_FAULT, "")
    fake = FakeOpenAI(ACME, **fixtures)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2, result.out
    assert message in result.out
    assert "infrastructure error" not in {s["outcome"] for s in result.summaries()}


@pytest.mark.design("budget.no_price")
@pytest.mark.slow
def test_a_model_with_no_price_fails_its_first_call_naming_the_setting(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"', 'function = "acme.rag:answer_priceless"\nruns = 1'
    )
    project.edit(TOOL_FAULT, "")
    fake = FakeOpenAI([])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2
    assert fake.paths == []
    assert "[agent.prices]" in result.out and "acme-llama-70b" in result.out


@pytest.mark.design("budget.none_counts_tokens")
@pytest.mark.slow
def test_with_the_budget_off_there_is_no_price_rule_and_tokens_are_counted(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.rag:answer_priceless"\nruns = 1\nmax_cost_usd = "none"',
    )
    project.edit(TOOL_FAULT, "")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.finished, result.out
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["tokens"] == {"input": 19, "output": 10, "cached": 0}
    assert "of no budget" in result.out


@pytest.mark.design("budget.estimates_without_counts")
@pytest.mark.slow
def test_a_stream_with_no_token_counts_is_estimated_and_says_so(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"', 'function = "acme.rag:answer_streamed"\nruns = 1'
    )
    project.edit(TOOL_FAULT, "")
    result = project.test(fake_openai(["chat_stream"]))
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["cost_estimated"] is True and line["stream"] is True
    assert result.summaries()[0]["answer"] == "Hello"


@pytest.mark.design("budget.stream_counts")
@pytest.mark.slow
def test_a_stream_with_token_counts_is_read_as_it_goes_by(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"', 'function = "acme.rag:answer_streamed"\nruns = 1'
    )
    project.edit(TOOL_FAULT, "")
    result = project.test(fake_openai(["chat_stream_usage"]))
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["cost_estimated"] is False and line["tokens"] == {
        "input": 9,
        "output": 1,
        "cached": 0,
    }


@pytest.mark.design("check.no_model_calls")
@pytest.mark.slow
def test_with_a_budget_and_no_model_call_in_the_baseline_the_test_fails(project: Project) -> None:
    project.edit('function = "acme.agent:answer"', 'function = "acme.slow:echo"\nruns = 1')
    project.edit(TOOL_FAULT, "")
    result = project.test()
    assert result.code == 2
    assert "order-status: no baseline run made a model call" in result.out
    assert "answered with no model call" in result.out


@pytest.mark.design("runner.siblings_stopped_not_errors")
@pytest.mark.parametrize(
    "function",
    [
        "acme.rag:answer_one_stops_the_rest",
        "acme.rag:answer_one_odd_tool_stops_the_rest",
        "acme.rag:answer_one_unseen_stops_the_rest",
    ],
    ids=["no price", "unknown tool type", "unseen tool"],
)
@pytest.mark.slow
def test_runs_stopped_because_another_run_stopped_the_test_are_not_errors(
    project: Project, function: str
) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "{function}"\nruns = 3\nparallel = 3',
    )
    project.edit(TOOL_FAULT, "")
    fake = FakeOpenAI([])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    outcomes = sorted(str(s["outcome"]) for s in result.summaries())
    assert outcomes == ["refused call", "stopped", "stopped"], result.out
    assert result.code == 2, result.out
    schema = json.loads(
        (Path(scrimmage.__file__).parent / "schemas" / "record.v1.json").read_text()
    )
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for lines in result.records():
        for line in lines:
            validator.validate(line)  # "stopped" is part of the record format


@pytest.mark.design("runner.finished_run_keeps_its_outcome")
@pytest.mark.slow
def test_a_run_that_finishes_by_itself_during_a_stop_keeps_its_answer(project: Project) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.rag:answer_one_stops_one_finishes"\nruns = 2\nparallel = 2',
    )
    project.edit(TOOL_FAULT, "")
    fake = FakeOpenAI([])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert sorted(str(s["outcome"]) for s in result.summaries()) == ["answered", "refused call"]


@pytest.mark.design("check.no_model_calls_per_test")
@pytest.mark.slow
def test_the_no_model_call_check_is_per_test(project: Project, fake_openai: Fake) -> None:
    project.edit(
        'function = "acme.agent:answer"', 'function = "acme.rag:answer_if_asked"\nruns = 1'
    )
    project.edit(TOOL_FAULT, "")
    project.toml.write_text(
        project.toml.read_text()
        + '\n[[test]]\nname = "offline"\nquestion = "no model please"\nexpect = ["x"]\n'
    )
    result = project.test(fake_openai(["chat_answer"]))
    assert result.code == 2
    assert "offline: no baseline run made a model call" in result.out
    assert "order-status: no baseline" not in result.out


@pytest.mark.design("check.no_model_calls_only_when_it_means_something")
@pytest.mark.slow
def test_a_run_refused_before_its_first_model_call_says_nothing_about_the_budget(
    project: Project,
) -> None:
    project.edit(
        'function = "acme.agent:answer"', 'function = "acme.slow:refuse_then_hang"\nruns = 1'
    )
    project.edit(TOOL_FAULT, "")
    result = project.test()
    assert result.code == 2
    assert "the budget is not working" not in result.out


@pytest.mark.design("runner.stops_late_starters")
@pytest.mark.slow
def test_a_program_started_during_a_stop_is_stopped_too(
    project: Project, monkeypatch: pytest.MonkeyPatch, time_limit: TimeLimit
) -> None:
    from scrimmage import config
    from scrimmage.runner import Runner

    project.edit('function = "acme.agent:answer"', 'function = "acme.slow:hang"\nruns = 1')
    project.edit(TOOL_FAULT, "")
    runner = Runner(config.load(project.toml))
    real_popen = subprocess.Popen

    def popen_then_cancel(*args: Any, **kwargs: Any) -> Any:
        proc = real_popen(*args, **kwargs)
        runner.cancelled = True  # the stop lands just after the program started
        return proc

    monkeypatch.setattr("scrimmage.runner.subprocess.Popen", popen_then_cancel)
    results = runner.run_all()
    assert time_limit.reached == [False]  # stopped, not left hanging for run_timeout
    assert [r.outcome for r in results] == ["stopped"]


@pytest.mark.design("budget.overrun_visible")
@pytest.mark.slow
def test_a_reply_that_costs_more_than_reserved_is_visible(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.rag:answer_short"\nruns = 1\nmax_cost_usd = 0.00001',
    )
    project.edit(TOOL_FAULT, "")
    result = project.test(fake_openai(["acme_get_order"]))  # 82 + 17 tokens, for max_tokens = 1
    assert "spent more than its budget" in result.out


@pytest.mark.design("budget.reached_outcome")
@pytest.mark.slow
def test_a_run_is_budget_reached_only_when_money_spent_reached_the_limit(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.rag:answer_short_twice"\nruns = 1\nmax_cost_usd = 0.00001',
    )
    project.edit(TOOL_FAULT, "")
    result = project.test(fake_openai(["acme_get_order"]))  # the first call overspends
    assert [s["outcome"] for s in result.summaries()] == ["budget reached"], result.out
    assert "test mistake: the budget of $1e-05 was reached: $0.0000 was spent" in result.out


@pytest.mark.design("check.baseline_failed")
@pytest.mark.slow
def test_a_failed_baseline_skips_its_fault_rows(project: Project, fake_openai: Fake) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:crash"\nruns = 2\nmax_cost_usd = "none"',
    )
    result = project.test()
    assert result.code == 2
    assert len(result.records()) == 2  # the baseline's two runs, and no fault row
    assert "baseline failed" in result.out


class Settling(Sink):
    """Hands each settle straight to the ledger, as the runner does."""

    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

    def put_settle(self, reservation: int, usd: float) -> None:
        super().put_settle(reservation, usd)
        self.ledger.settle(reservation, usd)


@pytest.mark.design("budget.hold_can_be_passed")
def test_calls_at_once_that_pass_their_holds_pass_the_limit_by_that_much(gated: None) -> None:
    """Three gpt-4.1 calls with no output limit each hold 4096 output tokens, about $0.0328,
    so all three fit in $0.10 at once. Each then writes 8192, about $0.0656, and the test
    spends $0.1967: the limit is passed by what the calls cost above their holds."""
    budget = Budget(limit=0.10)
    sink = Settling(budget.ledger)
    run = state.RunState(make_plan(budget=0.10), sink, [])
    state.install(run)
    http_gate.patch(httpx2, http_gate.Context(run, budget, sink, list[str]().append))
    together = threading.Barrier(3)
    reply = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8192}}

    def answer(request: httpx2.Request) -> httpx2.Response:
        together.wait(timeout=10)  # all three are reserved before any settles
        return httpx2.Response(200, json=reply)

    held: list[float] = []
    reserve = budget.ledger.reserve

    def watch(usd: float, *rest: Any) -> int | Shortfall:
        held.append(usd)
        return reserve(usd, *rest)

    budget.ledger.reserve = watch  # type: ignore[method-assign]
    sent = {"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]}
    post = httpx2.Client(transport=httpx2.MockTransport(answer)).post
    calls = [threading.Thread(target=post, args=(URL,), kwargs={"json": sent}) for _ in range(3)]
    for call in calls:
        call.start()
    for call in calls:
        call.join()
    each = (10 * 2.00 + 8192 * 8.00) / 1e6  # gpt-4.1: $2 in and $8 out per million
    assert [round(h, 4) for h in held] == [0.0328] * 3 and sum(held) <= 0.10
    assert [usd for _, usd in sink.settles] == [pytest.approx(each)] * 3
    assert round(budget.ledger.spent, 4) == 0.1967
    # Passed by at most what the calls cost above their holds, as the docs say.
    above = sum(each - h for h in held)
    assert round(above, 4) == 0.0983 and budget.ledger.spent - 0.10 <= above
