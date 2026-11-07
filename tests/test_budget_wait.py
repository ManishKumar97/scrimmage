"""A call that only holds keep out waits for them; spent money alone refuses at once."""

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from conftest import EXAMPLE_FAULT, Project
from derail_sdk import control
from derail_sdk.budget import Ledger, Shortfall
from derail_sdk.client import ControlClient
from derail_sdk.control import ControlServer
from derail_sdk.errors import DerailRefused
from derail_sdk.records import RecordStore
from fakes.openai_server import FakeOpenAI
from test_http_gate import BODY, URL, Budget, client, gated, install, make_plan

Fake = Callable[..., FakeOpenAI]
TOKEN = "f" * 64
__all__ = ["gated"]  # the fixture, used by name


def later(seconds: float, action: Callable[[], object]) -> threading.Thread:
    thread = threading.Thread(target=lambda: (time.sleep(seconds), action()), daemon=True)
    thread.start()
    return thread


@pytest.mark.design("wait.ledger_waits_for_a_hold")
def test_a_reservation_waits_for_a_hold_to_settle_then_takes_its_room() -> None:
    ledger = Ledger(0.10)
    held = ledger.reserve(0.06)
    assert isinstance(held, int)
    later(0.2, lambda: ledger.settle(held, 0.01))
    started = time.monotonic()
    second = ledger.reserve(0.06, wait_s=10)
    assert isinstance(second, int)
    assert 0.15 < time.monotonic() - started < 5  # woken by the settle, not the bound
    assert ledger.spent == pytest.approx(0.01) and ledger.reserved == pytest.approx(0.06)


@pytest.mark.design("wait.spent_refuses_at_once")
def test_money_spent_that_leaves_no_room_refuses_at_once() -> None:
    ledger = Ledger(0.10)
    first = ledger.reserve(0.08)
    assert isinstance(first, int)
    ledger.settle(first, 0.08)
    started = time.monotonic()
    refused = ledger.reserve(0.03, wait_s=10)
    assert time.monotonic() - started < 0.5
    assert refused == Shortfall("spent", 0.03, pytest.approx(0.08), 0.0, 0.10)  # type: ignore[arg-type]


@pytest.mark.design("wait.bounded")
def test_a_hold_that_never_settles_ends_the_wait_at_its_bound() -> None:
    ledger = Ledger(0.10)
    assert isinstance(ledger.reserve(0.06), int)
    started = time.monotonic()
    refused = ledger.reserve(0.06, wait_s=0.3)
    waited = time.monotonic() - started
    assert isinstance(refused, Shortfall) and refused.why == "held"
    assert refused.held == pytest.approx(0.06) and refused.spent == 0.0
    assert 0.25 < waited < 3


def test_with_no_wait_a_held_budget_refuses_at_once() -> None:
    ledger = Ledger(0.10)
    ledger.reserve(0.06)
    refused = ledger.reserve(0.06)
    assert isinstance(refused, Shortfall) and refused.why == "held"


@pytest.mark.design("wait.no_drift")
def test_holds_that_all_settle_leave_nothing_held() -> None:
    ledger = Ledger(1.0)
    numbers = [ledger.reserve(usd) for usd in (0.1, 0.2, 0.3)]
    for number in numbers:
        assert isinstance(number, int)
        ledger.settle(number, 0.0)
    assert ledger.reserved == 0.0  # exactly, so nothing waits on a rounding error


@pytest.mark.design("wait.trial_numbers")
def test_four_calls_at_once_on_gpt_4_1_all_get_their_turn() -> None:
    """The trial: each call holds 4096 tokens at $8 a million, the budget is $0.10."""
    ledger = Ledger(0.10)
    hold = 4096 * 8.0 / 1e6 + 0.0004
    got: list[object] = []
    start = threading.Barrier(4)

    def call() -> None:
        start.wait()
        number = ledger.reserve(hold, wait_s=10)
        got.append(number)
        time.sleep(0.2)  # the call is in flight
        if isinstance(number, int):
            ledger.settle(number, 0.0001)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(isinstance(number, int) for number in got)
    assert ledger.spent == pytest.approx(0.0004)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ControlServer]:
    monkeypatch.setattr(control, "MAX_WAIT_S", 0.3)
    made = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=Ledger(0.10))
    made.start()
    made.open_run("run-1", "baseline", {"v": 1})
    yield made
    made.stop()


@pytest.mark.design("wait.protocol")
def test_the_server_waits_at_most_one_slice_and_says_why(server: ControlServer) -> None:
    talk = ControlClient(server.url, TOKEN)
    held = talk.reserve("run-1", 0.06)
    assert isinstance(held, int)
    started = time.monotonic()
    refused = talk.reserve("run-1", 0.06, wait_s=60)
    assert time.monotonic() - started < 3  # one slice, not the minute asked for
    assert refused == Shortfall("held", 0.06, 0.0, 0.06, 0.10)
    later(0.1, lambda: server.ledger.settle(held, 0.02))
    assert isinstance(talk.reserve("run-1", 0.06, wait_s=60), int)


@pytest.mark.parametrize("wait", [-1, "x", True])
def test_a_bad_wait_is_a_bad_request(server: ControlServer, wait: object) -> None:
    talk = ControlClient(server.url, TOKEN)
    payload = {"run": "run-1", "usd": 0.01, "wait_s": wait}
    assert talk.request("POST", "/v1/budget/reserve", payload)[0] == 400


@pytest.mark.design("wait.gate_waits")
def test_a_sync_call_waits_for_another_runs_hold_and_goes_through(gated: None) -> None:
    budget = Budget(limit=0.004)
    sink, _ = install(make_plan(budget=0.004), budget)
    other = budget.ledger.reserve(0.003)  # another run's call, in flight
    assert isinstance(other, int)
    later(0.3, lambda: budget.ledger.settle(other, 0.0001))
    assert client().post(URL, json=BODY).status_code == 200  # gpt-4o-mini holds about $0.0025
    assert sink.lines[0].get("action") == "real" and "reason" not in sink.lines[0]


def held_forever(limit: float, time_limit: float = 1.0) -> tuple[Budget, list[object]]:
    budget = Budget(limit=limit)
    budget.ledger.reserve(0.003)  # a hold that never settles
    sink, _ = install(make_plan(budget=limit, time_limit_s=time_limit), budget)
    return budget, sink.lines  # type: ignore[return-value]


@pytest.mark.design("wait.in_steps")
def test_a_long_wait_is_made_of_short_requests(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from derail_sdk import http_gate

    monkeypatch.setattr(http_gate, "WAIT_STEP_S", 0.1)
    budget, _ = held_forever(0.004, time_limit=1.0)
    asked: list[float] = []
    real = budget.reserve

    def watch(run: str, usd: float, wait_s: float = 0.0) -> object:
        asked.append(wait_s)
        return real(run, usd, wait_s)

    budget.reserve = watch  # type: ignore[method-assign, assignment]
    with pytest.raises(DerailRefused, match=r"waited [01]s"):
        client().post(URL, json=BODY)
    assert len(asked) >= 4 and max(asked) <= 0.1  # each step short, so a cancel is seen


def test_a_budget_reply_without_its_numbers_is_a_control_error(server: ControlServer) -> None:
    from derail_sdk.client import ControlError

    talk = ControlClient(server.url, TOKEN)
    talk.request = lambda *args: (200, {"ok": False})  # type: ignore[method-assign, assignment]
    with pytest.raises(ControlError, match="budget reply is not valid"):
        talk.reserve("run-1", 0.01)


@pytest.mark.design("wait.gate_refuses_held")
def test_a_call_still_held_out_at_the_bound_is_refused_with_the_numbers(gated: None) -> None:
    _, lines = held_forever(0.004)
    started = time.monotonic()
    with pytest.raises(DerailRefused) as caught:
        client().post(URL, json=BODY)
    assert 0.4 < time.monotonic() - started < 3  # half of a one-second time limit
    message = str(caught.value)
    for part in ("$0.0030 is held by calls in flight", "$0.0000 is spent", "budget of $0.004"):
        assert part in message, message
    assert "max_tokens" in message and "reserve_output_tokens (4096)" in message
    line = lines[0]
    assert isinstance(line, dict) and line["reason"] == "budget_held"
    numbers = line["budget"]
    assert isinstance(numbers, dict) and numbers["held"] == pytest.approx(0.003)
    assert numbers["limit"] == 0.004 and numbers["waited_s"] == pytest.approx(0.5, abs=0.3)


@pytest.mark.design("wait.bound_is_capped")
def test_the_wait_is_never_more_than_thirty_seconds() -> None:
    from derail_sdk import http_gate

    assert http_gate.budget_wait(make_plan(time_limit_s=120.0)) == 30.0
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0)) == 5.0


@pytest.mark.design("wait.gate_refuses_spent")
def test_a_call_spent_money_cannot_cover_is_refused_at_once(gated: None) -> None:
    budget = Budget(limit=0.004)
    first = budget.ledger.reserve(0.003)
    assert isinstance(first, int)
    budget.ledger.settle(first, 0.003)
    sink, _ = install(make_plan(budget=0.004, time_limit_s=60.0), budget)
    started = time.monotonic()
    with pytest.raises(DerailRefused, match=r"\$0.0030 is spent"):
        client().post(URL, json=BODY)
    assert time.monotonic() - started < 1
    assert sink.lines[0]["reason"] == "budget"


@pytest.mark.design("wait.loop_runs")
def test_an_async_call_waits_without_blocking_the_loop(gated: None) -> None:
    import httpx2

    budget = Budget(limit=0.004)
    other = budget.ledger.reserve(0.003)
    assert isinstance(other, int)
    install(make_plan(budget=0.004), budget)
    later(0.5, lambda: budget.ledger.settle(other, 0.0))
    ticks: list[float] = []

    async def tick() -> None:
        for _ in range(8):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    async def main() -> int:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
        async with httpx2.AsyncClient(transport=transport) as session:
            ticker = asyncio.create_task(tick())
            reply = await session.post(URL, json=BODY)
            await ticker
            return reply.status_code

    assert asyncio.run(main()) == 200
    assert len(ticks) == 8 and ticks[-1] - ticks[0] < 1.0  # the loop ran while it waited


@pytest.mark.design("wait.cancel_ends_it")
def test_a_task_cancelled_while_it_waits_stops_waiting_and_holds_nothing(gated: None) -> None:
    import httpx2

    budget, lines = held_forever(0.004, time_limit=60.0)

    async def main() -> None:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
        async with httpx2.AsyncClient(transport=transport) as session:
            task = asyncio.create_task(session.post(URL, json=BODY))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    started = time.monotonic()
    asyncio.run(main())
    assert time.monotonic() - started < 5  # asyncio.run waits for the thread: it stopped
    assert budget.ledger.reserved == pytest.approx(0.003)  # only the other hold is left
    line = lines[0]
    assert isinstance(line, dict) and "CancelledError" in str(line["error"])


def agent(project: Project, function: str, settings: str) -> None:
    project.edit('function = "acme.agent:answer"', f'function = "{function}"\n{settings}')
    project.edit(EXAMPLE_FAULT, "")


@pytest.mark.design("wait.parallel_runs_end_to_end")
def test_parallel_runs_whose_holds_overlap_all_answer(project: Project) -> None:
    # gpt-4o-mini holds 4096 x $0.60 a million, about $0.0025, so two fit in $0.006.
    agent(
        project, "acme.rag:answer_streamed_slowly", "runs = 4\nparallel = 4\nmax_cost_usd = 0.006"
    )
    fake = FakeOpenAI(["chat_stream_usage"])
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.finished, result.out  # every run answered "Hello", which the test does not expect
    assert [s["outcome"] for s in result.summaries()] == ["answered"] * 4
    assert "could not be reserved" not in result.out and "was reached" not in result.out


@pytest.mark.design("wait.deadlock_end_to_end")
def test_a_stream_held_open_across_a_second_call_ends_in_a_clear_stop(project: Project) -> None:
    agent(
        project,
        "acme.rag:answer_holding_a_stream",
        "runs = 1\nrun_timeout = 4\nmax_cost_usd = 0.004",
    )
    fake = FakeOpenAI(["chat_stream_usage"])
    fake.start()
    started = time.monotonic()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert time.monotonic() - started < 15
    assert result.code == 2, result.out
    assert [s["outcome"] for s in result.summaries()] == ["budget held"]
    assert "test mistake: a model call could not be reserved" in result.out
    assert "was held by calls in flight" in result.out and "after a wait of 2s" in result.out
    assert "was reached" not in result.out


@pytest.mark.design("wait.report_says_reached_only_when_spent")
def test_the_report_says_reached_only_when_money_spent_reached_the_limit() -> None:
    from derail_sdk.runner import budget_line

    numbers: dict[str, object] = {
        "usd": 0.0331,
        "spent": 0.0001,
        "held": 0.0993,
        "limit": 0.1,
        "waited_s": 0.0,
    }
    line = budget_line("budget", numbers, 4096)
    assert line.startswith("a model call could not be reserved: it could cost up to $0.0331")
    assert "$0.0001 was spent and $0.0993 was held by calls in flight, of a budget of $0.1" in line
    assert "reserve_output_tokens (4096)" in line and "was reached" not in line
    reached = budget_line("budget", {**numbers, "spent": 0.1003}, 4096)
    assert reached.startswith("the budget of $0.1 was reached: $0.1003 was spent")
    held = budget_line("budget_held", {**numbers, "waited_s": 30.0}, 4096)
    assert "after a wait of 30s for calls in flight" in held
