"""The budget: reservations before each call, real costs after it, one ledger per test."""

import json
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import derail_sdk
from conftest import ACME, Project
from derail_sdk import provider_openai as table
from derail_sdk.budget import Ledger
from derail_sdk.client import ControlClient, ControlError
from derail_sdk.control import ControlServer
from derail_sdk.records import RecordStore
from fakes.openai_server import FIXTURES, FakeOpenAI

Fake = Callable[..., FakeOpenAI]
TOKEN = "e" * 64
TOOL_FAULT = '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'
RAG = 'function = "acme.rag:answer"\nruns = 1'


def rag(project: Project, extra: str) -> None:
    project.edit('function = "acme.agent:answer"', RAG + extra)
    project.edit(TOOL_FAULT, "")


@pytest.mark.design("budget.reservations_race")
def test_four_calls_at_79_of_80_cents_reserve_one_and_refuse_three() -> None:
    ledger = Ledger(0.80)
    ledger.settle(ledger.reserve(0.79) or 0, 0.79)
    results: list[int | None] = []
    start = threading.Barrier(4)

    def call() -> None:
        start.wait()
        results.append(ledger.reserve(0.01))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(r is not None for r in results) == 1


@pytest.mark.design("budget.real_cost_replaces_reservation")
def test_the_real_cost_replaces_the_reservation() -> None:
    ledger = Ledger(1.00)
    held = ledger.reserve(0.60)
    assert held is not None and ledger.reserve(0.60) is None
    ledger.settle(held, 0.10)
    assert (round(ledger.spent, 2), ledger.reserved) == (0.10, 0.0)
    assert ledger.reserve(0.60) is not None


def test_no_limit_means_every_reservation_passes() -> None:
    assert Ledger(None).reserve(1_000_000.0) is not None


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
    assert outcomes[0] is not None and outcomes[1] is None
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
PRICES = (1_000_000.0 * 0.01, 1_000_000.0 * 0.02)  # 1 cent an input token, 2 an output


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
    assert table.price("gpt-4o-mini", {}) == (0.15, 0.60)
    assert table.price("gpt-4o-mini-2024-07-18", {}) == (0.15, 0.60)  # a dated snapshot
    assert table.price("gpt-4o-mini", {"gpt-4o-mini": (9.0, 9.0)}) == (9.0, 9.0)
    assert table.price("acme-llama-70b", {"acme-llama-70b": (0.6, 0.6)}) == (0.6, 0.6)
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


@pytest.mark.design("budget.costs_reach_the_record")
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
    assert {s["outcome"] for s in result.summaries()} == {"budget reached"}
    assert "the budget of $1e-05 was reached" in result.out


@pytest.mark.design("budget.stop_with_parallel_runs")
@pytest.mark.parametrize(
    ("function", "extra", "fixtures", "message"),
    [
        ("acme.rag:answer", "max_cost_usd = 0.00001", {"embedding": "embedding"}, "was reached"),
        ("acme.rag:answer_priceless", "", {}, "[agent.prices]"),
        ("acme.rag:answer_odd_tool", "", {}, "a tool of type teleport"),
        ("acme.rag:answer_unseen", "", {}, "lookup_order"),
    ],
    ids=["budget", "no price", "unknown tool type", "unseen tool"],
)
def test_a_stop_at_default_parallel_exits_2_not_3(
    project: Project, function: str, extra: str, fixtures: dict[str, str], message: str
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
def test_with_the_budget_off_there_is_no_price_rule_and_tokens_are_counted(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.rag:answer_priceless"\nruns = 1\nmax_cost_usd = "none"',
    )
    project.edit(TOOL_FAULT, "")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.code == 0, result.out
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["tokens"] == {"input": 19, "output": 10}
    assert "of no budget" in result.out


@pytest.mark.design("budget.estimates_without_counts")
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
def test_a_stream_with_token_counts_is_read_as_it_goes_by(
    project: Project, fake_openai: Fake
) -> None:
    project.edit(
        'function = "acme.agent:answer"', 'function = "acme.rag:answer_streamed"\nruns = 1'
    )
    project.edit(TOOL_FAULT, "")
    result = project.test(fake_openai(["chat_stream_usage"]))
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert line["cost_estimated"] is False and line["tokens"] == {"input": 9, "output": 1}


@pytest.mark.design("check.no_model_calls")
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
        (Path(derail_sdk.__file__).parent / "schemas" / "record.v1.json").read_text()
    )
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for lines in result.records():
        for line in lines:
            validator.validate(line)  # "stopped" is part of the record format


@pytest.mark.design("runner.finished_run_keeps_its_outcome")
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
def test_a_program_started_during_a_stop_is_stopped_too(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from derail_sdk import config
    from derail_sdk.runner import Runner

    project.edit('function = "acme.agent:answer"', 'function = "acme.slow:hang"\nruns = 1')
    project.edit(TOOL_FAULT, "")
    runner = Runner(config.load(project.toml))
    real_popen = subprocess.Popen

    def popen_then_cancel(*args: Any, **kwargs: Any) -> Any:
        proc = real_popen(*args, **kwargs)
        runner.cancelled = True  # the stop lands just after the program started
        return proc

    monkeypatch.setattr("derail_sdk.runner.subprocess.Popen", popen_then_cancel)
    started = time.monotonic()
    results = runner.run_all()
    assert time.monotonic() - started < 30  # not left hanging for run_timeout
    assert [r.outcome for r in results] == ["stopped"]


@pytest.mark.design("budget.overrun_visible")
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


@pytest.mark.design("check.baseline_failed")
def test_a_failed_baseline_skips_its_fault_rows(project: Project, fake_openai: Fake) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:crash"\nruns = 2\nmax_cost_usd = "none"',
    )
    result = project.test()
    assert result.code == 2
    assert len(result.records()) == 2  # the baseline's two runs, and no fault row
    assert "baseline failed" in result.out
