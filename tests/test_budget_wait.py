"""A call that only holds keep out waits its turn; spent money alone refuses at once.

The ledger never blocks a request: a waiter has a ticket and asks again every step.
"""

import asyncio
import dataclasses
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from conftest import EXAMPLE_FAULT, Project, Result
from fakes.openai_server import FakeOpenAI
from scrimmage_core import http_gate, state
from scrimmage_core.budget import STALE_S, Held, Ledger, Shortfall
from scrimmage_core.client import ControlClient, ControlError
from scrimmage_core.config import Agent
from scrimmage_core.control import ControlServer
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.records import RecordStore
from test_http_gate import BODY, URL, Budget, client, gated, install, make_plan

Fake = Callable[..., FakeOpenAI]
TOKEN = "f" * 64
LABEL = "model:openai"
__all__ = ["gated"]  # the fixture, used by name


class Clock:
    """The gate's clock: time passes only when the gate pauses, so a wait is exact under
    any load. An action set for a time runs once the wait has reached it."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        actions: list[tuple[float, Callable[[], object]]] | None = None,
    ) -> None:
        current = state.current()
        assert current is not None, "install the run first"
        self.start = self.now = current.began
        self.slept: list[float] = []
        self.actions = sorted(actions or [], key=lambda action: action[0])
        monkeypatch.setattr(http_gate, "time", self)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 1e-6)  # as a real clock does, it always moves on
        while self.actions and self.waited >= self.actions[0][0] - 1e-9:
            self.actions.pop(0)[1]()

    @property
    def waited(self) -> float:
        return self.now - self.start


def until(event: Callable[[], bool], ceiling: float = 60.0) -> None:
    """Wait for an event; the ceiling only ends a test whose event never comes."""
    end = time.monotonic() + ceiling
    while not event():
        assert time.monotonic() < end, "the event never came"
        time.sleep(0.01)


def granted(answer: int | Shortfall) -> int:
    assert isinstance(answer, int), answer
    return answer


def short(answer: int | Shortfall) -> Shortfall:
    assert isinstance(answer, Shortfall), answer
    return answer


# The ledger


@pytest.mark.design("wait.ledger_tickets")
def test_a_held_call_gets_a_ticket_and_is_granted_when_it_asks_after_a_settle() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.06, "run-a", LABEL))
    refused = short(ledger.reserve(0.06, "run-b", LABEL))
    assert refused.why == "held" and refused.ticket is not None
    assert refused.holds == (Held("run-a", LABEL, 0.06),)
    ledger.settle(held, 0.01)
    granted(ledger.reserve(0.06, "run-b", LABEL, refused.ticket))
    assert ledger.spent == pytest.approx(0.01) and ledger.reserved == pytest.approx(0.06)


@pytest.mark.design("wait.spent_refuses_at_once")
def test_money_spent_that_leaves_no_room_refuses_at_once() -> None:
    ledger = Ledger(0.10)
    ledger.settle(granted(ledger.reserve(0.08)), 0.08)
    refused = short(ledger.reserve(0.03))
    assert (refused.why, refused.ticket, refused.spent) == ("spent", None, pytest.approx(0.08))


@pytest.mark.design("wait.no_drift")
def test_holds_that_all_settle_leave_nothing_held() -> None:
    ledger = Ledger(1.0)
    for number in [granted(ledger.reserve(usd)) for usd in (0.1, 0.2, 0.3)]:
        ledger.settle(number, 0.0)
    assert ledger.reserved == 0.0  # exactly, so nothing waits on a rounding error


@pytest.mark.design("wait.fifo")
def test_tickets_are_served_in_order_so_a_large_call_is_not_starved() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.06, "run-a"))
    big = short(ledger.reserve(0.05, "run-b")).ticket
    small = short(ledger.reserve(0.03, "run-c"))  # it fits alone, but the big call came first
    assert small.why == "held"
    ledger.settle(held, 0.0)
    granted(ledger.reserve(0.03, "run-c", ticket=small.ticket))  # now both fit
    granted(ledger.reserve(0.05, "run-b", ticket=big))


@pytest.mark.design("wait.no_starvation")
def test_a_large_waiter_gets_its_turn_among_steady_small_calls() -> None:
    """The review's probe, in fixed turns, so no scheduler decides it: four callers keep
    taking $0.03 and giving it back, and one call needs $0.05. Each turn every small
    caller settles what it holds and asks again, then the large call asks."""
    ledger = Ledger(0.10)
    small: list[int | Shortfall | None] = [None] * 4
    big: int | Shortfall | None = None
    for _ in range(20):
        for n in range(4):
            mine = small[n]
            if isinstance(mine, int):
                ledger.settle(mine, 0.00001)
            ticket = mine.ticket if isinstance(mine, Shortfall) else None
            small[n] = ledger.reserve(0.03, "run-small", ticket=ticket)
        ticket = big.ticket if isinstance(big, Shortfall) else None
        big = ledger.reserve(0.05, "run-big", ticket=ticket)
        if isinstance(big, int):
            return
    pytest.fail("the large call never got in: the small calls took every room it freed")


@pytest.mark.design("wait.stale_ticket")
def test_a_ticket_not_asked_for_again_is_dropped() -> None:
    now = [0.0]
    ledger = Ledger(0.10, clock=lambda: now[0])
    held = granted(ledger.reserve(0.06))
    short(ledger.reserve(0.06, "run-gone"))  # its caller dies and never asks again
    ledger.settle(held, 0.0)
    assert short(ledger.reserve(0.05, "run-c")).why == "held"  # still behind the dead one
    now[0] = STALE_S + 0.1
    granted(ledger.reserve(0.05, "run-c"))


def test_a_released_ticket_lets_the_next_call_through() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.06))
    first = short(ledger.reserve(0.06, "run-b")).ticket
    ledger.settle(held, 0.0)
    ledger.release(first or 0)
    granted(ledger.reserve(0.06, "run-c"))


@pytest.mark.design("wait.close_keeps_holds")
def test_a_closed_runs_holds_stay_and_are_listed_until_a_late_settle() -> None:
    ledger = Ledger(0.10)
    hold = granted(ledger.reserve(0.06, "run-a", LABEL))
    ticket = short(ledger.reserve(0.06, "run-a")).ticket
    ledger.close_run("run-a")
    assert ledger.reserved == pytest.approx(0.06)  # the call may have been sent
    assert ledger.leaked() == [Held("run-a", LABEL, 0.06, ended=True)]
    assert ticket is not None and not ledger._line  # its ticket went
    ledger.settle(hold, 0.02)  # money spent is spent, even after the close
    assert ledger.leaked() == [] and ledger.spent == pytest.approx(0.02)


# The control server


@pytest.fixture
def server(tmp_path: Path) -> Iterator[ControlServer]:
    made = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=Ledger(0.10))
    made.start()
    made.open_run("run-1", "baseline", {"v": 1})
    yield made
    made.stop()


@pytest.mark.design("wait.protocol")
def test_the_server_answers_at_once_and_names_the_holds(server: ControlServer) -> None:
    talk = ControlClient(server.url, TOKEN)
    held = granted(talk.reserve("run-1", 0.06, LABEL))
    started = time.monotonic()
    refused = short(talk.reserve("run-1", 0.06, LABEL))
    for _ in range(99):
        short(talk.reserve("run-1", 0.06, LABEL, refused.ticket))
    # No request is held open. The first build held each up to 2 s waiting for a settle,
    # so these 100 asks would take 200 s; short requests take far less than 30 s.
    assert time.monotonic() - started < 30
    assert refused.holds == (Held("run-1", LABEL, 0.06),) and refused.ticket is not None
    server.ledger.settle(held, 0.02)
    granted(talk.reserve("run-1", 0.06, LABEL, refused.ticket))


@pytest.mark.parametrize("payload", [{"usd": 0.01, "ticket": "x"}, {"usd": float("nan")}])
def test_a_bad_reserve_is_a_bad_request(server: ControlServer, payload: dict[str, object]) -> None:
    talk = ControlClient(server.url, TOKEN)
    assert talk.request("POST", "/v1/budget/reserve", {"run": "run-1", **payload})[0] == 400
    assert talk.request("POST", "/v1/budget/release", {"ticket": "x"})[0] == 400


def test_a_budget_reply_without_its_numbers_is_a_control_error(server: ControlServer) -> None:
    talk = ControlClient(server.url, TOKEN)
    talk.request = lambda *args: (200, {"ok": False})  # type: ignore[method-assign, assignment]
    with pytest.raises(ControlError, match="budget reply is not valid"):
        talk.reserve("run-1", 0.01)
    talk.request = lambda *args: (_ for _ in ()).throw(ControlError("down"))  # type: ignore[method-assign, assignment]
    talk.release(1)  # best effort: a ticket nobody asks for is dropped anyway


WAITER = """
import sys, time
from scrimmage_core.client import ControlClient
talk, ticket = ControlClient(sys.argv[1], sys.argv[2]), None
while True:
    answer = talk.reserve("run-1", 0.06, "model:openai", ticket)
    ticket = answer.ticket
    time.sleep(0.1)
"""


@pytest.mark.design("wait.killed_waiter_gets_nothing")
def test_a_waiter_killed_mid_wait_is_never_granted_a_hold(server: ControlServer) -> None:
    """The review's leak.py: before, the server granted the dead waiter after a settle."""
    hold = granted(server.ledger.reserve(0.06, "run-0", LABEL))
    child = subprocess.Popen([sys.executable, "-c", WAITER, server.url, TOKEN])
    try:
        until(lambda: bool(server.ledger._line))  # the waiter is waiting, with its ticket
    finally:
        child.kill()
        child.wait()
    server.close_run("run-1")
    server.ledger.settle(hold, 0.0)
    time.sleep(0.3)
    assert server.ledger.reserved == 0.0 and server.ledger.leaked() == []


@pytest.mark.design("wait.many_waiters")
def test_a_hundred_waiters_get_no_control_errors_and_a_settle_stays_fast(
    tmp_path: Path,
) -> None:
    """The review's slots.py: before, waiters filled the 64 slots and connections reset."""
    ledger = Ledger(1.0)
    made = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=ledger)
    made.start()
    made.open_run("run-1", "baseline", {"v": 1})
    hold = granted(ledger.reserve(0.9, "run-0"))
    errors: list[str] = []
    asks = [0] * 100
    grants: list[int] = []
    stop = threading.Event()

    def waiter(n: int) -> None:
        talk, ticket = ControlClient(made.url, TOKEN), None
        while not stop.is_set():
            try:
                answer = talk.reserve("run-1", 0.5, LABEL, ticket)
            except ControlError as exc:
                errors.append(str(exc))
                return
            asks[n] += 1
            if isinstance(answer, int):
                grants.append(n)
                talk.request("POST", "/v1/budget/settle", {"reservation": answer, "usd": 0.0})
                ticket = None
            else:
                ticket = answer.ticket
            time.sleep(http_gate.WAIT_STEP_S)

    threads = [threading.Thread(target=waiter, args=(n,), daemon=True) for n in range(100)]
    try:
        for thread in threads:
            thread.start()
        until(lambda: min(asks) >= 2 or bool(errors))  # all 100 are waiting, asking again
        payload = {"reservation": hold, "usd": 0.0}
        status = ControlClient(made.url, TOKEN).request("POST", "/v1/budget/settle", payload)[0]
        until(lambda: len(grants) >= 10 or bool(errors))  # the freed room goes round them
    finally:
        stop.set()
        for thread in threads:
            thread.join(30)
        made.stop()
    # Before, waiters filled the server's threads, and 16 of 100 got a connection reset.
    assert errors == [] and status == 200


# The gate


@pytest.mark.design("wait.gate_waits")
def test_a_sync_call_waits_for_another_runs_hold_and_goes_through(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = Budget(limit=0.004)
    sink, _ = install(make_plan(budget=0.004), budget)
    other = granted(budget.ledger.reserve(0.003, "run-other", LABEL))
    clock = Clock(monkeypatch, [(0.3, lambda: budget.ledger.settle(other, 0.0001))])
    assert client().post(URL, json=BODY).status_code == 200  # gpt-4o-mini holds about $0.0025
    assert sink.lines[0].get("action") == "real" and "reason" not in sink.lines[0]
    assert clock.waited >= 0.3  # it waited for the settle


@pytest.mark.design("wait.other_run_past_the_short_bound")
def test_a_hold_in_another_run_is_waited_out_past_the_short_bound(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow model in another run must not stop the test at 30 s, here made 0.3 s."""
    monkeypatch.setattr(http_gate, "BUDGET_WAIT_S", 0.3)
    budget = Budget(limit=0.004)
    install(make_plan(budget=0.004, time_limit_s=60.0), budget)
    other = granted(budget.ledger.reserve(0.003, "run-other", LABEL))
    clock = Clock(monkeypatch, [(1.0, lambda: budget.ledger.settle(other, 0.0001))])
    assert client().post(URL, json=BODY).status_code == 200
    assert clock.waited >= 1.0  # past the short bound of 0.3 s


@pytest.mark.design("wait.bounds")
def test_the_bound_is_short_for_own_holds_and_long_for_other_runs() -> None:
    plan = make_plan(time_limit_s=120.0)
    assert http_gate.budget_wait(plan) == 30.0
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0)) == 5.0
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0), elapsed=4.0) == 3.0
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0), elapsed=11.0) == 0.0
    assert http_gate.budget_wait(plan, elapsed=20.0, own=False) == 90.0  # 10 s left over
    assert http_gate.budget_wait(make_plan(time_limit_s=10.0), own=False) == 9.0
    assert http_gate.budget_wait(plan, elapsed=200.0, own=False) == 0.0


def held_forever(
    limit: float, time_limit: float = 1.0, run: str = "run-1"
) -> tuple[Budget, list[object]]:
    budget = Budget(limit=limit)
    budget.ledger.reserve(0.003, run, LABEL)  # a hold that never settles
    sink, _ = install(make_plan(budget=limit, time_limit_s=time_limit), budget)
    return budget, sink.lines  # type: ignore[return-value]


@pytest.mark.design("wait.gate_refuses_held")
def test_a_call_held_out_by_its_own_run_is_refused_naming_the_hold(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget, lines = held_forever(0.004)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused) as caught:
        client().post(URL, json=BODY)
    assert clock.waited == pytest.approx(0.5, abs=0.001)  # half of a one-second time limit
    message = str(caught.value)
    for part in (
        "$0.0030 is held by calls in flight, of a budget of $0.004 "
        "(model:openai in this run $0.0030)",
        "$0.0000 is spent",
        "budget of $0.004",
        "a stream left open, or not read to its end, keeps its hold",
    ):
        assert part in message, message
    line = lines[0]
    assert isinstance(line, dict) and line["reason"] == "budget_held"
    numbers = line["budget"]
    assert isinstance(numbers, dict) and numbers["held"] == pytest.approx(0.003)
    assert numbers["holds"] == [{"run": "run-1", "label": LABEL, "usd": 0.003, "ended": False}]
    assert numbers["limit"] == 0.004 and numbers["waited_s"] == 0.5
    assert budget.ledger.reserve(0.0, "run-x") is not None and not budget.ledger._line


def test_a_refusal_after_other_runs_names_them_and_gives_the_parallel_advice(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_gate, "RUN_MARGIN_S", 0.5)
    _, _ = held_forever(0.004, time_limit=1.0, run="run-other")
    Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused) as caught:
        client().post(URL, json=BODY)
    message = str(caught.value)
    assert "model:openai in run-other $0.0030" in message
    assert "lower [agent] parallel" in message and "reserve_output_tokens (4096)" in message


@pytest.mark.design("wait.in_steps")
def test_a_wait_asks_again_every_step_with_one_ticket(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_gate, "WAIT_STEP_S", 0.05)
    budget, _ = held_forever(0.004, time_limit=1.0)
    Clock(monkeypatch)
    asked: list[int | None] = []
    real = budget.reserve

    def watch(run: str, usd: float, label: str = "", ticket: int | None = None) -> object:
        asked.append(ticket)
        return real(run, usd, label, ticket)

    budget.reserve = watch  # type: ignore[method-assign, assignment]
    with pytest.raises(ScrimmageRefused, match=r"waited [01]s"):
        client().post(URL, json=BODY)
    assert len(asked) >= 5 and asked[0] is None and len(set(asked[1:])) == 1


@pytest.mark.design("wait.gate_refuses_spent")
def test_a_call_spent_money_cannot_cover_is_refused_at_once(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = Budget(limit=0.004)
    budget.ledger.settle(granted(budget.ledger.reserve(0.003)), 0.003)
    sink, _ = install(make_plan(budget=0.004, time_limit_s=60.0), budget)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused, match=r"\$0.0030 is spent"):
        client().post(URL, json=BODY)
    assert clock.slept == []  # no pause: waiting cannot help
    assert sink.lines[0]["reason"] == "budget"


@pytest.mark.design("wait.loop_runs")
def test_an_async_call_waits_without_blocking_the_loop(gated: None) -> None:
    import httpx2

    budget = Budget(limit=0.004)
    other = granted(budget.ledger.reserve(0.003, "run-other"))
    install(make_plan(budget=0.004), budget)
    ticks: list[float] = []

    async def tick() -> None:
        for _ in range(8):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)
        # The settle comes from a task on the loop, so the call can get through only if
        # the loop kept running while it waited. A blocked loop ends at the bound, refused.
        budget.ledger.settle(other, 0.0)

    async def main() -> int:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
        async with httpx2.AsyncClient(transport=transport) as session:
            ticker = asyncio.create_task(tick())
            reply = await session.post(URL, json=BODY)
            await ticker
            return reply.status_code

    assert asyncio.run(main()) == 200
    assert len(ticks) == 8


@pytest.mark.design("wait.cancel_ends_it")
def test_a_task_cancelled_while_it_waits_stops_and_gives_up_its_ticket(gated: None) -> None:
    import httpx2

    budget, lines = held_forever(0.004, time_limit=60.0, run="run-other")
    asks: list[int | None] = []
    real = budget.reserve

    def watch(run: str, usd: float, label: str = "", ticket: int | None = None) -> object:
        asks.append(ticket)
        return real(run, usd, label, ticket)

    budget.reserve = watch  # type: ignore[method-assign, assignment]
    at_cancel: list[int] = []

    async def main() -> None:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
        async with httpx2.AsyncClient(transport=transport) as session:
            task = asyncio.create_task(session.post(URL, json=BODY))
            for _ in range(6000):  # until the gate has asked once and is waiting
                if asks:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            at_cancel.append(len(asks))
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(main())  # it waits for the gate's thread, so the wait has ended
    assert asks and len(asks) - at_cancel[0] <= 1  # an ask in flight at most, then it stopped
    assert budget.ledger.reserved == pytest.approx(0.003)  # only the other hold is left
    assert not budget.ledger._line  # type: ignore[attr-defined]  # its ticket is gone
    line = lines[0]
    assert isinstance(line, dict) and "CancelledError" in str(line["error"])


# End to end


def agent(project: Project, function: str, settings: str) -> None:
    project.edit('function = "acme.agent:answer"', f'function = "{function}"\n{settings}')
    project.edit(EXAMPLE_FAULT, "")


def run_on_fake(project: Project, fixtures: list[str]) -> Result:
    fake = FakeOpenAI(fixtures)
    fake.start()
    try:
        return project.test(fake)
    finally:
        fake.stop()


@pytest.mark.design("wait.parallel_runs_end_to_end")
def test_parallel_runs_whose_holds_overlap_all_answer(project: Project) -> None:
    # gpt-4o-mini holds 4096 x $0.60 a million, about $0.0025, so two fit in $0.006.
    agent(
        project, "acme.rag:answer_streamed_slowly", "runs = 4\nparallel = 4\nmax_cost_usd = 0.006"
    )
    result = run_on_fake(project, ["chat_stream_usage"])
    assert result.finished, result.out
    assert [s["outcome"] for s in result.summaries()] == ["answered"] * 4
    assert "could not be reserved" not in result.out


DEADLOCK = '''"""The agent that holds a stream open, with a short wait for its own holds."""

from scrimmage_core import http_gate

from acme.rag import answer_holding_a_stream as answer

# The bound under test, made short in this run's own process. The run's time limit stays
# at 120 s, so the wait always ends first, however slowly the run starts.
http_gate.BUDGET_WAIT_S = 1.0
__all__ = ["answer"]
'''


@pytest.mark.design("wait.deadlock_end_to_end")
def test_a_stream_held_open_across_a_second_call_ends_in_a_clear_stop(project: Project) -> None:
    (project.root / "acme" / "deadlock.py").write_text(DEADLOCK)
    agent(project, "acme.deadlock:answer", "runs = 1\nmax_cost_usd = 0.004")
    result = run_on_fake(project, ["chat_stream_usage"])
    assert result.code == 2, result.out
    assert [s["outcome"] for s in result.summaries()] == ["not reserved"]
    assert "test mistake: a model call could not be reserved" in result.out
    assert "was held by calls in flight (model:openai in run-" in result.out
    assert "after a wait of 1s for calls in flight" in result.out, result.out
    assert "was reached" not in result.out


@pytest.mark.design("wait.leak_reported")
def test_a_run_killed_with_a_hold_leaves_it_held_and_the_report_says_so(
    project: Project,
) -> None:
    """The agent kills itself once its stream holds money, so no timeout is raced."""
    agent(project, "acme.rag:answer_killed_holding", "runs = 1")
    result = run_on_fake(project, ["chat_stream_usage"])
    assert re.search(r"warning: run-\w+ ended with model:openai still holding \$0\.00", result.out)


@pytest.mark.design("wait.async_holds_no_thread")
def test_many_async_calls_to_a_host_name_all_answer_under_a_tight_budget(
    project: Project,
) -> None:
    """The verifier's repro: 16 calls by gather to localhost, with room for 2 at a time.
    Before, waiting calls held asyncio's threads, which also look up host names."""
    agent(project, "acme.rag:answer_many_async", "runs = 1\nmax_cost_usd = 0.006")
    project.edit(
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]',
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}, '
        '{host = "localhost", provider = "openai"}]',
    )
    result = run_on_fake(project, ["chat_stream_usage"])
    assert [s["outcome"] for s in result.summaries()] == ["answered"], result.out
    assert result.summaries()[0]["answer"] == " ".join(["Hello"] * 16)
    model = [x for x in result.records()[0] if x.get("kind") == "model"]
    assert len(model) == 16 and not any(x.get("error") for x in model)


def short_of(holds: tuple[Held, ...], spent: float = 0.0) -> Shortfall:
    held = sum(h.usd for h in holds)
    return Shortfall("held", 0.0025, spent, held, 0.006, 1, holds)


@pytest.mark.design("wait.own_run_classified")
def test_a_call_waits_on_itself_when_its_own_holds_alone_keep_it_out() -> None:
    own, other = Held("run-1", LABEL, 0.004), Held("run-2", LABEL, 0.001)
    assert http_gate.waits_on_itself(short_of((own, other)), "run-1")  # other run or not
    assert not http_gate.waits_on_itself(short_of((Held("run-1", LABEL, 0.001), other)), "run-1")
    assert not http_gate.waits_on_itself(short_of((Held("run-2", LABEL, 0.004),)), "run-1")
    assert http_gate.waits_on_itself(short_of((own,), spent=0.0001), "run-1")


@pytest.mark.design("wait.own_run_with_another_run_holding")
def test_an_own_run_deadlock_ends_at_the_short_bound_though_another_run_holds_money(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_gate, "BUDGET_WAIT_S", 0.3)
    budget = Budget(limit=0.006)
    budget.ledger.reserve(0.004, "run-1", LABEL)  # this run's open stream
    budget.ledger.reserve(0.001, "run-other", LABEL)  # another run's call, also in flight
    sink, _ = install(make_plan(budget=0.006, time_limit_s=60.0), budget)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused, match="may be waiting on itself"):
        client().post(URL, json=BODY)
    assert clock.waited == pytest.approx(0.3, abs=0.001)  # the short bound, not most of a minute
    numbers = sink.lines[0]["budget"]
    assert isinstance(numbers, dict) and numbers["own"] is True and numbers["waited_s"] == 0.3


@pytest.mark.design("wait.ended_run_counts_as_spent")
def test_a_hold_left_by_an_ended_run_refuses_at_once_and_is_named(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = Budget(limit=0.004)
    budget.ledger.reserve(0.003, "run-dead", LABEL)
    budget.ledger.close_run("run-dead")  # killed during its call: the hold never settles
    sink, _ = install(make_plan(budget=0.004, time_limit_s=60.0), budget)
    clock = Clock(monkeypatch)
    with pytest.raises(ScrimmageRefused) as caught:
        client().post(URL, json=BODY)
    assert clock.slept == []  # waiting cannot help
    message = str(caught.value)
    assert "left by run-dead, which ended while still holding" in message
    assert "waiting cannot help" in message and "waiting on itself" not in message
    assert "lower [agent] parallel" not in message
    line = sink.lines[0]
    assert line["reason"] == "budget"
    numbers = line["budget"]
    assert isinstance(numbers, dict) and numbers["holds"][0]["ended"] is True  # type: ignore[index]


def test_the_ledger_tells_a_left_hold_from_one_in_flight() -> None:
    ledger = Ledger(0.10)
    granted(ledger.reserve(0.06, "run-dead", LABEL))
    ledger.close_run("run-dead")
    refused = short(ledger.reserve(0.06, "run-b"))
    assert refused.why == "left" and refused.ticket is None
    assert refused.holds == (Held("run-dead", LABEL, 0.06, ended=True),)
    granted(ledger.reserve(0.03, "run-b"))  # room that is free is still free


@pytest.mark.design("wait.many_fit_at_once")
def test_fifty_waiters_that_all_fit_are_granted_together(server: ControlServer) -> None:
    """The verifier's drain.py: before, only the head was granted per ask, so 50 took 3.8 s."""
    server.ledger.limit_usd = 1.0
    hold = granted(server.ledger.reserve(0.99, "run-0", "big"))
    asks = [0] * 50
    grants: list[int] = []
    held_after: list[int] = []  # a waiter that asked after the settle and still waited
    settled = threading.Event()

    def waiter(n: int) -> None:
        talk, ticket = ControlClient(server.url, TOKEN), None
        for _ in range(400):
            after = settled.is_set()
            answer = talk.reserve("run-1", 0.01, LABEL, ticket)
            asks[n] += 1
            if isinstance(answer, int):
                grants.append(n)
                return
            if after:
                held_after.append(n)
            ticket = answer.ticket
            time.sleep(http_gate.WAIT_STEP_S)

    threads = [threading.Thread(target=waiter, args=(n,), daemon=True) for n in range(50)]
    for thread in threads:
        thread.start()
    until(lambda: min(asks) >= 1)  # all 50 wait behind the big hold
    server.ledger.settle(hold, 0.0)
    settled.set()
    for thread in threads:
        thread.join(60)
    # Each is granted at its first ask after the settle. Before, only the head of the line
    # was granted per ask, so the last of 50 waited through dozens of asks.
    assert len(grants) == 50 and held_after == []


def test_waiters_ahead_that_would_fit_are_served_together_in_order() -> None:
    ledger = Ledger(0.10)
    held = granted(ledger.reserve(0.10, "run-0"))
    tickets = [short(ledger.reserve(0.03, f"run-{n}")).ticket for n in (1, 2, 3, 4)]
    ledger.settle(held, 0.0)
    granted(ledger.reserve(0.03, "run-3", ticket=tickets[2]))  # 1 and 2 still fit before it
    assert short(ledger.reserve(0.03, "run-4", ticket=tickets[3])).why == "held"  # no room left


@pytest.mark.design("wait.report_says_reached_only_when_spent")
def test_the_report_says_reached_only_when_money_spent_reached_the_limit() -> None:
    from scrimmage_core.runner import budget_line, reached

    agent_ = config_agent(parallel=4)
    numbers: dict[str, object] = {
        "usd": 0.0331,
        "spent": 0.0001,
        "held": 0.0993,
        "limit": 0.1,
        "waited_s": 0.0,
        "own": False,
        "holds": [{"run": "run-7", "label": LABEL, "usd": 0.0331, "ended": False}],
    }
    line = budget_line("budget", numbers, agent_)
    assert line.startswith("a model call could not be reserved: it could cost up to $0.0331")
    assert "$0.0001 was spent and $0.0993 was held by calls in flight" in line
    assert "(model:openai in run-7 $0.0331), of a budget of $0.1" in line
    assert "reserve_output_tokens (4096)" in line and "was reached" not in line
    full = {**numbers, "spent": 0.1003}
    assert budget_line("budget", full, agent_).startswith("the budget of $0.1 was reached: $0.1003")
    held = budget_line("budget_held", {**numbers, "waited_s": 30.0}, agent_)
    assert "after a wait of 30s for calls in flight" in held
    assert reached(full) and not reached(numbers)


@pytest.mark.design("wait.report_advice_fits")
@pytest.mark.parametrize("parallel", [1, 2])
def test_the_reports_advice_fits_the_case_and_the_parallel_setting(parallel: int) -> None:
    from scrimmage_core.runner import budget_line

    base: dict[str, object] = {"usd": 0.03, "spent": 0.0, "held": 0.08, "limit": 0.1}
    hold = {"run": "run-7", "label": LABEL, "usd": 0.08, "ended": False}
    others = budget_line("budget_held", {**base, "holds": [hold]}, config_agent(parallel))
    assert ("lower [agent] parallel" in others) is (parallel > 1)
    own = budget_line("budget_held", {**base, "own": True}, config_agent(parallel))
    assert "close a stream before the next call" in own and "parallel" not in own
    dead = budget_line(
        "budget", {**base, "holds": [{**hold, "ended": True}]}, config_agent(parallel)
    )
    assert "left by run-7, which ended while still holding" in dead
    assert "parallel" not in dead and "run killed during a call" in dead


def config_agent(parallel: int) -> Agent:
    from scrimmage_core import config

    loaded = config.load(Path(__file__).parent / "example" / "scrimmage.toml").agent
    return dataclasses.replace(loaded, parallel=parallel)


@pytest.mark.design("wait.numbers_from_the_stopping_run")
def test_the_report_takes_its_numbers_from_the_run_that_stopped_the_test() -> None:
    from scrimmage_core.runner import RunResult, budget_refusal, stop_numbers

    def result(reason: str, spent: float) -> RunResult:
        made = RunResult("run", None, "not reserved", None, None, {})  # type: ignore[arg-type]
        made.budget_reason, made.budget = reason, {"spent": spent}
        return made

    results = [result("budget", 0.01), result("budget_held", 0.02)]
    assert stop_numbers(results, "budget_held") == {"spent": 0.02}
    assert stop_numbers(results, "no_price") is None
    lines: list[dict[str, object]] = [
        {"action": "refused", "reason": "unlisted"},
        {"action": "refused", "reason": "budget_held", "budget": {"spent": 0.5}},
    ]
    assert budget_refusal(lines) == lines[1]  # its first budget refusal, not its first refusal
