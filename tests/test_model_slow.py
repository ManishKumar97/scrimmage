"""slow on a model call, in one process: the delay comes first, then the call runs as the
baseline would, reserved and billed. The openai library's own timeout and retries meet it as
they would meet a slow server."""

import asyncio
import threading
from collections.abc import Iterator

import httpx2
import openai
import pytest

from model_gate import GUARD, URL, Gate, Provider, ask, guard
from scrimmage import state
from scrimmage.config import Fault
from scrimmage.errors import ScrimmageStopped


@pytest.fixture(autouse=True)
def gated() -> Iterator[None]:
    yield
    state.install(None)


class Watched(threading.Event):
    """A run's stop event that notes, in the shared events, when a slowed call waits on it."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.looked = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        self.events.append("wait")
        self.looked.set()
        return super().wait(timeout)

    def is_set(self) -> bool:
        self.looked.set()
        return super().is_set()


def slow(seconds: str, when: str = "always") -> Fault:
    return Fault("model", f"slow {seconds}", when, model=GUARD)


def slowed(*faults: Fault) -> tuple[Gate, Provider, Watched]:
    gate = Gate(*faults)
    watched = Watched(gate.events)
    gate.run.stopped = watched
    return gate, Provider(gate.events), watched


def model_lines(gate: Gate) -> list[dict[str, object]]:
    return [x for x in gate.sink.lines if x.get("kind") == "model"]


def test_slow_sends_the_real_call_after_the_delay() -> None:
    gate, provider, _ = slowed(slow("0.3s"))
    assert ask(guard(provider)) == "unsafe\nS14"  # the real reply, late
    line = gate.line
    assert (line["action"], line["under"]) == ("fault", "real")
    assert line["fault"] == {"do": "slow 0.3s", "when": "always", "n": 1}
    assert line["slowed_ms"] >= 290 and "cut_by" not in line  # type: ignore[operator]
    assert len(provider.sent) == 1 and line["cost_usd"]


def test_slow_reserves_only_after_the_delay() -> None:
    gate, provider, _ = slowed(slow("0.2s"))
    ask(guard(provider))
    assert gate.events == ["wait", "reserve", "sent"]


def test_slow_past_the_clients_timeout_raises_its_timeout_and_sends_and_reserves_nothing() -> None:
    gate, provider, _ = slowed(slow("30s"))
    with pytest.raises(openai.APITimeoutError):
        ask(guard(provider, timeout=0.3))
    assert provider.sent == [] and "reserve" not in gate.events
    line = gate.line
    assert (line["action"], line["cut_by"], line["error"]) == (
        "fault",
        "client timeout",
        "ReadTimeout: The read operation timed out",
    )
    assert 250 <= line["slowed_ms"] < 20000  # type: ignore[operator]
    assert gate.sink.settles == []  # nothing was held, so nothing is settled


def test_the_openai_library_retries_a_slowed_timeout_and_each_attempt_is_slowed() -> None:
    gate, provider, _ = slowed(slow("30s"))
    client = guard(provider, timeout=0.3).with_options(max_retries=2)
    with pytest.raises(openai.APITimeoutError):
        ask(client)
    lines = model_lines(gate)
    assert [(x["action"], x["cut_by"], x["retry"]) for x in lines] == [
        ("fault", "client timeout", 0),
        ("fault", "client timeout", 1),
        ("fault", "client timeout", 2),
    ]
    assert provider.sent == []


def test_first_1_slows_only_the_first_attempt() -> None:
    gate, provider, _ = slowed(slow("30s", "first 1"))
    client = guard(provider, timeout=0.3).with_options(max_retries=2)
    assert ask(client) == "unsafe\nS14"  # the library's retry went through at normal speed
    lines = model_lines(gate)
    assert [(x["action"], x.get("cut_by"), x["retry"]) for x in lines] == [
        ("fault", "client timeout", 0),
        ("real", None, 1),
    ]
    assert len(provider.sent) == 1


def test_requests_1_slows_every_attempt_of_the_first_request() -> None:
    gate, provider, _ = slowed(slow("30s", "requests 1"))
    client = guard(provider, timeout=0.3).with_options(max_retries=2)
    with pytest.raises(openai.APITimeoutError):
        ask(client)
    assert ask(client) == "unsafe\nS14"  # the next request is not slowed
    lines = model_lines(gate)
    assert [(x["action"], x["retry"]) for x in lines] == [
        ("fault", 0),
        ("fault", 1),
        ("fault", 2),
        ("real", 0),
    ]
    assert [x["fault"]["n"] for x in lines[:3]] == [1, 1, 1]  # type: ignore[index]


def test_slow_on_an_async_model_call_leaves_the_loop_running() -> None:
    gate, provider, _ = slowed(slow("0.5s"))

    async def go() -> tuple[str, int]:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.ensure_future(ticker())
        http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(provider))
        async with openai.AsyncOpenAI(
            base_url=URL, api_key="k", max_retries=0, http_client=http
        ) as client:
            reply = await client.chat.completions.create(
                model=GUARD, messages=[{"role": "user", "content": "x"}]
            )
        task.cancel()
        return reply.choices[0].message.content or "", ticks

    text, ticks = asyncio.run(go())
    assert text == "unsafe\nS14"
    assert ticks >= 5  # a time.sleep on the loop would give the ticker one tick
    assert gate.events[-2:] == ["reserve", "sent"]


def test_slow_ends_when_the_run_is_stopped() -> None:
    gate, provider, watched = slowed(slow("30s"))
    caught: list[BaseException] = []

    def call() -> None:
        try:
            ask(guard(provider))
        except BaseException as exc:
            caught.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    assert watched.looked.wait(10)
    gate.run.stop_open()
    thread.join(10)
    assert not thread.is_alive()
    assert provider.sent == [] and "reserve" not in gate.events
    # The library wraps what the gate raised; the cause is the stop.
    assert isinstance(caught[0].__cause__ or caught[0], ScrimmageStopped)
    assert gate.line["cut_by"] == "run stopped"
