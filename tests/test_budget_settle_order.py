"""A finished call's real cost counts before the next call is reserved."""

import asyncio
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from scrimmage import client as client_module
from scrimmage import http_gate, state
from scrimmage.budget import Ledger
from scrimmage.client import ControlClient, ControlError, RunSender, Sender
from scrimmage.control import ControlServer
from scrimmage.errors import ScrimmageRefused
from scrimmage.records import RecordStore
from test_http_gate import URL, make_plan

TOKEN = "d" * 64
LIMIT = 0.06
OUTPUT = 5000  # gpt-4.1 output tokens in each reply: $0.04, far above a hold of 64 tokens
SENT = {"model": "gpt-4.1", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
REPLY = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": OUTPUT}}
EACH = (10 * 2.00 + OUTPUT * 8.00) / 1e6  # gpt-4.1: $2 in and $8 out per million


class Stack:
    """The real runner ledger, control server, client and sender, with the sender thread
    never started: nothing but the gate's own steps delivers a settle, so the timing that
    a fast agent meets by luck is the same on every run."""

    def __init__(self, tmp_path: Path) -> None:
        self.ledger = Ledger(LIMIT)
        self.control = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=self.ledger)
        self.control.start()
        self.control.open_run("run-1", "baseline", {"v": 1})
        self.client = ControlClient(self.control.url, TOKEN)
        self.sender = Sender(self.client, "run-1")
        plan = make_plan(budget=LIMIT, reserve_output_tokens=64)
        run = state.RunState(plan, self.sender, [])
        state.install(run)
        ctx = http_gate.Context(run, self.client, self.sender, list[str]().append)
        http_gate.patch(httpx2, ctx)
        answer = httpx2.MockTransport(lambda _: httpx2.Response(200, json=REPLY))
        self.http = httpx2.Client(transport=answer)
        self.holds: list[float] = []
        self.spent_at_reserve: list[float] = []  # what the ledger had counted at each reserve
        reserve = self.ledger.reserve

        def watch(usd: float, *rest: object) -> object:
            self.holds.append(usd)
            self.spent_at_reserve.append(self.ledger.spent)
            return reserve(usd, *rest)  # type: ignore[arg-type]

        self.ledger.reserve = watch  # type: ignore[method-assign]

    def calls(self, count: int) -> int:
        """Make calls one at a time until one is refused; how many were made."""
        for made in range(count):
            try:
                self.http.post(URL, json=SENT)
            except ScrimmageRefused:
                return made
        return count

    def stop(self) -> None:
        self.control.stop()
        state.install(None)


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    stack = Stack(tmp_path)
    yield stack
    stack.stop()


def test_one_call_at_a_time_passes_the_limit_by_at_most_one_calls_excess(stack: Stack) -> None:
    """The docs' bound: only calls in flight at once can pass max_cost_usd, each by what it
    cost above its hold. Here one call is in flight at a time."""
    made = stack.calls(6)
    excess = EACH - min(stack.holds)
    # The sender never ran, so the ledger has seen no settle yet: what the calls cost is
    # what the mock server billed them, EACH a call.
    assert made * EACH <= LIMIT + excess, f"{made} calls cost ${made * EACH:.4f} of ${LIMIT}"


def test_a_call_right_after_a_finished_call_sees_what_it_cost(stack: Stack) -> None:
    assert stack.calls(2) == 2
    assert stack.spent_at_reserve == [0.0, pytest.approx(EACH)]
    assert len(stack.sender.settles) == 1  # only the second call's own settle is queued


def test_an_async_agent_one_call_at_a_time_cannot_pass_the_limit_either(stack: Stack) -> None:
    async def run() -> int:
        answer = httpx2.MockTransport(lambda _: httpx2.Response(200, json=REPLY))
        async with httpx2.AsyncClient(transport=answer) as http:
            for made in range(6):
                try:
                    await http.post(URL, json=SENT)
                except ScrimmageRefused:
                    return made
        return 6

    made = asyncio.run(run())
    assert made * EACH <= LIMIT + EACH - min(stack.holds)
    assert stack.spent_at_reserve[:2] == [0.0, pytest.approx(EACH)]


def queue_settle(stack: Stack, usd: float = 0.002, hold: float = 0.001) -> int:
    held = stack.ledger.reserve(hold, "run-1")
    assert isinstance(held, int)
    stack.sender.put_settle(held, usd)
    return held


def test_a_reserve_carries_its_settles_in_one_request(stack: Stack) -> None:
    queue_settle(stack)
    paths: list[str] = []
    request = stack.client.request

    def count(method: str, path: str, *rest: Any, **more: Any) -> tuple[int, dict[str, object]]:
        paths.append(path)
        return request(method, path, *rest, **more)

    stack.client.request = count  # type: ignore[method-assign]
    stack.client.reserve("run-1", 0.001)
    assert paths == ["/v1/budget/reserve"]
    assert not stack.sender.settles and stack.ledger.spent == pytest.approx(0.002)


def test_a_reserve_with_nothing_queued_carries_nothing(stack: Stack) -> None:
    bodies: list[object] = []
    request = stack.client.request

    def keep(method: str, path: str, payload: Any = None, **more: Any) -> Any:
        bodies.append(payload)
        return request(method, path, payload, **more)

    stack.client.request = keep  # type: ignore[method-assign]
    stack.client.reserve("run-1", 0.001)
    assert cast(dict[str, object], bodies[0])["settles"] == []


def test_settles_that_did_not_get_through_are_queued_again_in_order(stack: Stack) -> None:
    first, second = queue_settle(stack, 0.002), queue_settle(stack, 0.003)
    stack.client.senders["run-1"] = stack.sender
    good = stack.sender.client
    stack.sender.client = ControlClient("http://127.0.0.1:9", TOKEN)
    stack.sender.client.senders["run-1"] = stack.sender
    with pytest.raises(ControlError):
        stack.sender.client.reserve("run-1", 0.001)
    assert [line["reservation"] for line in stack.sender.settles] == [first, second]
    assert stack.ledger.spent == 0.0
    good.reserve("run-1", 0.001)
    assert not stack.sender.settles and stack.ledger.spent == pytest.approx(0.005)


def test_settles_are_queued_again_when_the_request_is_interrupted(stack: Stack) -> None:
    held = queue_settle(stack)

    def interrupted(*_: object, **__: object) -> tuple[int, dict[str, object]]:
        raise KeyboardInterrupt

    request = stack.client.request
    stack.client.request = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        stack.client.reserve("run-1", 0.001)
    assert [line["reservation"] for line in stack.sender.settles] == [held]
    stack.client.request = request  # type: ignore[method-assign]


def test_a_settle_carried_twice_counts_once(stack: Stack) -> None:
    """A reply lost after the ledger applied the settles: the same ones ride again."""
    held = queue_settle(stack)
    sent = list(stack.sender.settles)
    stack.client.reserve("run-1", 0.001)
    stack.sender.give_back_settles(sent)  # as if that reply had never come
    stack.client.reserve("run-1", 0.001)
    assert stack.ledger.spent == pytest.approx(0.002) and held not in stack.ledger._held  # type: ignore[reportPrivateUsage]


def test_a_settle_is_settled_even_when_its_run_has_closed(stack: Stack) -> None:
    held = queue_settle(stack)
    stack.control.close_run("run-1")
    with pytest.raises(ControlError):
        stack.client.reserve("run-1", 0.001)  # run_closed
    assert held not in stack.ledger._held and stack.ledger.spent == pytest.approx(0.002)  # type: ignore[reportPrivateUsage]


def test_a_reserve_with_an_invalid_settle_applies_none_of_them(stack: Stack) -> None:
    held = queue_settle(stack)
    status, reply = stack.client.request(
        "POST",
        "/v1/budget/reserve",
        {
            "run": "run-1",
            "usd": 0.001,
            "settles": [{"reservation": held, "usd": 0.002}, {"reservation": 1, "usd": -1.0}],
        },
    )
    assert (status, reply) == (400, {"error": "bad_request"}) and stack.ledger.spent == 0.0


def test_no_more_than_the_cap_ride_on_one_reserve(stack: Stack) -> None:
    for _ in range(client_module.SETTLES_PER_RESERVE + 5):
        queue_settle(stack, hold=1e-6)
    stack.client.reserve("run-1", 0.001)
    assert len(stack.sender.settles) == 5


def test_a_forked_childs_queued_settle_rides_on_its_reserve(stack: Stack) -> None:
    stack.sender.forked("a child")
    held = stack.ledger.reserve(0.001, "run-1")
    assert isinstance(held, int)
    with stack.sender._sending:  # type: ignore[reportPrivateUsage]  # a send is in flight
        stack.sender.put_settle(held, 0.002)
    assert len(stack.sender.settles) == 1  # the eager send found the lock busy
    stack.client.reserve("run-1", 0.001)
    assert not stack.sender.settles and stack.ledger.spent == pytest.approx(0.002)


def sending_slowly(stack: Stack) -> tuple[threading.Thread, threading.Event]:
    """The sender thread, stopped inside the send of one settle until the event is set."""
    in_flight, let_go = threading.Event(), threading.Event()
    request = stack.client.request

    def slow(method: str, path: str, payload: Any = None, **more: Any) -> Any:
        if path == "/v1/budget/settle":
            in_flight.set()
            assert let_go.wait(10)
        return request(method, path, payload, **more)

    stack.client.request = slow  # type: ignore[method-assign]
    sending = threading.Thread(target=stack.sender._send_posts)  # type: ignore[reportPrivateUsage]
    sending.start()
    assert in_flight.wait(10)
    return sending, let_go


def test_a_reserve_waits_for_a_settle_in_flight_however_slow_the_runner_is(stack: Stack) -> None:
    """The sender has a settle in flight for 200 ms when the next call is reserved: the
    reserve is checked after it, not against its hold."""
    held = queue_settle(stack)
    sending, let_go = sending_slowly(stack)
    threading.Timer(0.2, let_go.set).start()
    stack.client.reserve("run-1", 0.001)
    sending.join(10)
    assert held not in stack.ledger._held  # type: ignore[reportPrivateUsage]
    assert stack.spent_at_reserve[-1] == pytest.approx(0.002)
    assert not stack.sender.settles


def test_a_claim_thread_does_not_wait_for_a_whole_settle_request(stack: Stack) -> None:
    """A claim thread must not wait for a request: past SETTLE_WAIT_S its reserve goes on,
    still taking the queued settles, and the sender delivers the one it was sending."""
    held = queue_settle(stack)
    other = queue_settle(stack, 0.003)
    sending, let_go = sending_slowly(stack)

    taken: list[dict[str, object]] = []

    def claim() -> None:
        client_module.never_wait()
        stack.sender.take_settles(taken)

    thread = threading.Thread(target=claim)
    began = time.monotonic()
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - began < 5
    assert [line["reservation"] for line in taken] == [other]  # the queued settle is taken
    assert not stack.sender.settles
    let_go.set()
    sending.join(10)
    assert held not in stack.ledger._held  # type: ignore[reportPrivateUsage]


class Raced(deque[dict[str, object]]):
    """A queue another thread has emptied between the check and the pop."""

    def popleft(self) -> dict[str, object]:
        raise IndexError


def test_a_queue_emptied_by_a_reserve_gives_the_slot_back_and_still_sends_the_posts(
    stack: Stack,
) -> None:
    stack.sender.settles = Raced([{"run": "run-1", "reservation": 1, "usd": 0.0}])
    stack.sender.put_blob("a" * 64, b"x")
    paths: list[str] = []
    request = stack.client.request

    def keep(method: str, path: str, *rest: Any, **more: Any) -> Any:
        paths.append(path)
        return request(method, path, *rest, **more)

    stack.client.request = keep  # type: ignore[method-assign]
    stack.sender._send_posts()  # type: ignore[reportPrivateUsage]
    assert stack.client.slots._value == client_module.CAP  # type: ignore[reportPrivateUsage]
    assert paths == ["/v1/blobs"]


def failing(client: ControlClient, error: str) -> Any:
    """A request that fails as the real one does, giving back the slot it was handed."""

    def request(*_: object, **__: object) -> tuple[int, dict[str, object]]:
        client.slots.release()
        if error == "raises":
            raise ControlError("down")
        return int(error), {"error": "no"}

    return request


@pytest.mark.parametrize("error", ["raises", "503", "400"])
def test_a_settle_the_runner_did_not_accept_stays_queued_in_order(stack: Stack, error: str) -> None:
    first, second = queue_settle(stack, 0.002), queue_settle(stack, 0.003)
    request = stack.client.request
    stack.client.request = failing(stack.client, error)  # type: ignore[method-assign]
    stack.sender._send_posts()  # type: ignore[reportPrivateUsage]
    assert [line["reservation"] for line in stack.sender.settles] == [first, second]
    assert stack.client.slots._value == client_module.CAP  # type: ignore[reportPrivateUsage]
    stack.client.request = request  # type: ignore[method-assign]
    stack.sender._send_posts()  # type: ignore[reportPrivateUsage]
    assert not stack.sender.settles and stack.ledger.spent == pytest.approx(0.005)


def test_a_settle_interrupted_in_the_sender_is_queued_again_and_the_slot_returns(
    stack: Stack,
) -> None:
    held = queue_settle(stack)

    def interrupted(*_: object, **__: object) -> tuple[int, dict[str, object]]:
        stack.client.slots.release()  # as the real request does for a held slot
        raise KeyboardInterrupt

    request = stack.client.request
    stack.client.request = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        stack.sender._send_posts()  # type: ignore[reportPrivateUsage]
    assert [line["reservation"] for line in stack.sender.settles] == [held]
    assert stack.client.slots._value == client_module.CAP  # type: ignore[reportPrivateUsage]
    stack.client.request = request  # type: ignore[method-assign]


class Interrupting(deque[dict[str, object]]):
    """A queue whose second pop is an exception landing in the middle of taking."""

    def popleft(self) -> dict[str, object]:
        if self.armed and len(self) < self.armed:
            raise KeyboardInterrupt
        return super().popleft()

    armed = 0


def test_an_exception_while_settles_are_taken_gives_back_those_taken(stack: Stack) -> None:
    first, second = queue_settle(stack, 0.002), queue_settle(stack, 0.003)
    queue = Interrupting(stack.sender.settles)
    queue.armed = 2  # the first pop works; the second raises
    stack.sender.settles = queue
    with pytest.raises(KeyboardInterrupt):
        stack.client.reserve("run-1", 0.001)
    assert [line["reservation"] for line in stack.sender.settles] == [first, second]


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1.0])
def test_a_non_finite_or_negative_settle_is_rejected_by_both_endpoints(
    stack: Stack, bad: float
) -> None:
    held = queue_settle(stack)
    carried = [{"reservation": held, "usd": 0.002}, {"reservation": held, "usd": bad}]
    status, _ = stack.client.request(
        "POST", "/v1/budget/reserve", {"run": "run-1", "usd": 0.001, "settles": carried}
    )
    assert status == 400 and stack.ledger.spent == 0.0
    status, _ = stack.client.request(
        "POST", "/v1/budget/settle", {"run": "run-1", "reservation": held, "usd": bad}
    )
    assert status == 400 and stack.ledger.spent == 0.0


def test_closing_a_run_sender_forgets_it_even_when_the_last_batch_fails(stack: Stack) -> None:
    sender = RunSender(stack.client, "run-9")
    assert stack.client.senders["run-9"] is sender

    def fail(**_: object) -> bool:
        raise OSError("down")

    sender._send_batch = fail  # type: ignore[method-assign, assignment]
    with pytest.raises(OSError):
        sender.close()
    assert "run-9" not in stack.client.senders


def test_a_settle_sent_by_the_sender_and_a_reserve_at_once_is_sent_once(stack: Stack) -> None:
    for _ in range(50):
        queue_settle(stack, hold=1e-6)
    settled: list[int] = []
    settle = stack.ledger.settle

    def count(reservation: int, usd: float) -> None:
        settled.append(reservation)
        settle(reservation, usd)

    stack.ledger.settle = count  # type: ignore[method-assign]
    threads = [threading.Thread(target=stack.sender._send_posts)]  # type: ignore[reportPrivateUsage]
    threads += [
        threading.Thread(target=stack.client.reserve, args=("run-1", 1e-6)) for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(settled) == 50 and len(set(settled)) == 50 and not stack.sender.settles
