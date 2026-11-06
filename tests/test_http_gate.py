"""The httpx2 gate in one process, with lines kept in a list and a mock transport."""

import asyncio
import dataclasses
import gzip
import itertools
import json
import threading
import time
import tomllib
import types
import zlib
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx2
import openai
import pytest

from faultline_sdk import http_gate, provider_openai, state
from faultline_sdk.budget import Ledger
from faultline_sdk.client import ControlClient
from faultline_sdk.config import Fault, ModelHost
from faultline_sdk.decide import Counters
from faultline_sdk.errors import FaultlineRefused
from faultline_sdk.plan import Plan
from faultline_sdk.runner import real_error

URL = "https://api.openai.com/v1/chat/completions"
BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
REPLY = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class Sink:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []
        self.blobs: dict[str, bytes] = {}
        self.settles: list[tuple[int, float]] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        self.lines.append(line)

    def put_blob(self, sha: str, data: bytes) -> bool:
        self.blobs[sha] = data
        return True

    def put_settle(self, reservation: int, usd: float) -> None:
        self.settles.append((reservation, usd))


class Budget:
    """Stands in for the runner: records reservations, optionally slowly."""

    def __init__(self, limit: float | None = None, delay: float = 0.0) -> None:
        self.ledger = Ledger(limit)
        self.delay = delay

    def reserve(self, run: str, usd: float) -> int | None:
        time.sleep(self.delay)
        return self.ledger.reserve(usd)


def make_plan(faults: tuple[Fault, ...] = (), budget: float | None = None, **kw: Any) -> Plan:
    return Plan(
        "run-1", "t", "row", (), faults, (), (), 60.0, budget_usd=budget,
        model_hosts=(ModelHost("gateway.acme.internal", "openai"),), **kw,
    )  # fmt: skip


@pytest.fixture
def gated() -> Iterator[None]:
    sync_send = httpx2.Client._send_single_request
    async_send = httpx2.AsyncClient._send_single_request
    yield
    httpx2.Client._send_single_request = sync_send  # type: ignore[method-assign]
    httpx2.AsyncClient._send_single_request = async_send  # type: ignore[method-assign]
    state.install(None)


def stopper(stops: list[str]) -> Any:
    return stops.append


def install(plan: Plan, client: Any = None) -> tuple[Sink, http_gate.Context]:
    sink = Sink()
    run = state.RunState(plan, sink, [])
    state.install(run)
    ctx = http_gate.Context(run, client or Budget(), sink, stopper([]))
    http_gate.patch(httpx2, ctx)
    return sink, ctx


def answer(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=REPLY)


def client() -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(answer))


def test_without_a_run_the_gate_passes_the_request_through(gated: None) -> None:
    sink, _ = install(make_plan())
    state.install(None)
    assert client().post(URL, json=BODY).status_code == 200
    assert sink.lines == []


@pytest.mark.design("gate.other_hosts_gated")
def test_a_write_to_a_host_that_is_not_a_model_host_is_refused(gated: None) -> None:
    sink, _ = install(make_plan())
    with pytest.raises(FaultlineRefused, match="POST /refunds"):
        client().post("https://orders.acme.internal/refunds", json={})
    assert sink.lines[0]["call"] == "http:orders.acme.internal"
    assert sink.lines[0]["reason"] == "web_write"


@pytest.mark.design("gate.model_hosts_setting")
def test_a_team_gateway_under_model_hosts_is_a_model_call(gated: None) -> None:
    sink, _ = install(make_plan())
    client().post("https://gateway.acme.internal/v1/chat/completions", json=BODY)
    assert sink.lines[0]["call"] == "model:openai"


def test_a_read_on_a_model_host_runs_and_is_recorded(gated: None) -> None:
    sink, _ = install(make_plan())
    assert client().get("https://api.openai.com/v1/files").status_code == 200
    line = sink.lines[0]
    assert (line["call"], line["action"], line["method"]) == ("http:api.openai.com", "real", "GET")


@pytest.mark.design("gate.reply_unchanged")
def test_the_agent_gets_the_reply_exactly_as_it_was_sent(gated: None) -> None:
    sink, _ = install(make_plan())
    response = client().post(URL, json=BODY)
    assert response.json() == REPLY
    line = sink.lines[0]
    assert line["tokens"] == {"input": 10, "output": 5} and line["status"] == 200
    assert line["cost_estimated"] is False


@pytest.mark.design("gate.handled_once")
def test_a_request_is_decided_once(gated: None) -> None:
    sink, ctx = install(make_plan())
    http_gate.patch(httpx2, ctx)  # a second patch does not wrap twice
    client().post(URL, json=BODY)
    assert len(sink.lines) == 1


@pytest.mark.design("gate.async_same_path")
def test_the_async_gate_follows_the_same_path(gated: None) -> None:
    faults = (Fault("model", "status 429", "first 1"),)
    sink, _ = install(make_plan(faults))

    async def go() -> list[int]:
        transport = httpx2.MockTransport(answer)
        async with httpx2.AsyncClient(transport=transport) as c:
            first = await c.post(URL, json=BODY)
            second = await c.post(URL, json=BODY)
            return [first.status_code, second.status_code]

    assert asyncio.run(go()) == [429, 200]
    assert [x["action"] for x in sink.lines] == ["fault", "real"]


@pytest.mark.design("gate.async_no_block")
def test_an_async_call_does_not_block_the_loop_while_it_reserves(gated: None) -> None:
    budget = Budget(limit=100.0, delay=0.3)
    install(make_plan(budget=100.0), budget)
    ticks: list[float] = []

    async def ticker() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def go() -> None:
        tick = asyncio.create_task(ticker())
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as c:
            await c.post(URL, json=BODY)
        tick.cancel()

    asyncio.run(go())
    gaps = [b - a for a, b in itertools.pairwise(ticks)]
    assert len(ticks) > 10 and max(gaps) < 0.15  # the loop kept running through the 0.3 s wait


def test_a_timeout_fault_on_the_async_path_is_httpx2s_own(gated: None) -> None:
    install(make_plan((Fault("model", "timeout", "always"),)))

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as c:
            await c.post(URL, json=BODY)

    with pytest.raises(httpx2.ReadTimeout):
        asyncio.run(go())


@pytest.mark.design("budget.cancelled_reservation_given_back")
def test_a_task_cancelled_while_it_reserves_gives_the_reservation_back(gated: None) -> None:
    budget = Budget(limit=100.0, delay=0.2)
    sink, _ = install(make_plan(budget=100.0), budget)

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as c:
            task = asyncio.create_task(c.post(URL, json=BODY))
            await asyncio.sleep(0.05)  # it is now waiting for the reservation
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.3)  # the thread gets its reservation with nobody to use it

    asyncio.run(go())
    assert sink.settles == [(1, 0.0)]
    assert [str(x["error"]).split(":")[0] for x in sink.lines] == ["CancelledError"]
    assert real_error(sink.lines[0]) is None  # cancelling is not the provider failing


@pytest.mark.design("budget.cancelled_reservation_given_back")
def test_a_task_cancelled_just_after_it_reserved_gives_the_reservation_back(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def done_then_cancelled(work: Any) -> None:
        work()  # the thread finished, and the cancel lands before the task wakes
        raise asyncio.CancelledError

    budget = Budget(limit=100.0)
    sink, _ = install(make_plan(budget=100.0), budget)
    monkeypatch.setattr(asyncio, "to_thread", done_then_cancelled)

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as c:
            with pytest.raises(asyncio.CancelledError):
                await c.post(URL, json=BODY)

    asyncio.run(go())
    assert sink.settles == [(1, 0.0)]
    assert [str(x["error"]).split(":")[0] for x in sink.lines] == ["CancelledError"]


@pytest.mark.design("budget.cancelled_in_the_queue_is_recorded")
def test_work_that_starts_after_the_cancel_does_nothing(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    late: list[Any] = []

    async def cancelled_first(work: Any) -> None:
        late.append(work)  # a thread picked it up, and the cancel got to the lock first
        raise asyncio.CancelledError

    budget = Budget(limit=100.0)
    sink, _ = install(make_plan(budget=100.0), budget)
    monkeypatch.setattr(asyncio, "to_thread", cancelled_first)

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as c:
            with pytest.raises(asyncio.CancelledError):
                await c.post(URL, json=BODY)

    asyncio.run(go())
    assert late[0]() is None
    assert len(sink.lines) == 1 and sink.settles == []
    assert budget.ledger.reserved == 0.0


@pytest.mark.design("budget.cancelled_in_the_queue_is_recorded")
def test_tasks_cancelled_before_a_thread_took_them_keep_their_lines(gated: None) -> None:
    release = threading.Event()

    class Held(Budget):
        def reserve(self, run: str, usd: float) -> int | None:
            release.wait(5)
            return super().reserve(run, usd)

    sink, _ = install(make_plan(budget=100.0), Held(limit=100.0))

    async def go() -> None:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(1))
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as c:
            tasks = [asyncio.create_task(c.post(URL, json=BODY)) for _ in range(5)]
            await asyncio.sleep(0.05)  # one holds the only thread, four wait for it
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release.set()
            await asyncio.sleep(0.1)

    asyncio.run(go())
    assert [str(x["error"]).split(":")[0] for x in sink.lines] == ["CancelledError"] * 5
    assert {x["cost_usd"] for x in sink.lines} == {0.0}
    assert sink.settles == [(1, 0.0)]  # only the one that reached a thread reserved


@pytest.mark.design("gate.outside_asyncio_refused")
def test_an_async_call_outside_asyncio_fails_closed(gated: None) -> None:
    sink, _ = install(make_plan())
    c = httpx2.AsyncClient(transport=httpx2.MockTransport(answer))
    attempt = c._send_single_request(c.build_request("POST", URL, json=BODY))  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(FaultlineRefused, match="outside asyncio"):
        attempt.send(None)  # driven by hand, as a loop other than asyncio would
    assert sink.lines[0]["reason"] == "unsupported_loop"


def test_a_negative_retry_header_counts_as_a_first_attempt(gated: None) -> None:
    sink, _ = install(make_plan())
    client().post(URL, json=BODY, headers={"x-stainless-retry-count": "-1"})
    assert sink.lines[0]["retry"] == 0


@pytest.mark.design("budget.unreachable_refuses")
def test_an_unreachable_control_server_refuses_the_call(gated: None) -> None:
    dead = ControlClient("http://127.0.0.1:9", "t" * 64)
    sink, _ = install(make_plan(budget=1.0), dead)
    with pytest.raises(FaultlineRefused, match="could not be checked"):
        client().post(URL, json=BODY)
    assert sink.lines[0]["reason"] == "control_unreachable"


@pytest.mark.design("budget.settles_off_the_agents_thread")
def test_the_real_cost_is_settled_by_the_sender_not_the_agent(gated: None) -> None:
    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)
    client().post(URL, json=BODY)
    assert sink.settles == [(1, pytest.approx(0.0000045))]  # 10 in, 5 out, at gpt-4o-mini prices


@pytest.mark.design("budget.failed_call_gives_back")
def test_a_call_that_gets_no_reply_gives_its_reservation_back(gated: None) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)
    with pytest.raises(httpx2.ConnectError):
        httpx2.Client(transport=httpx2.MockTransport(down)).post(URL, json=BODY)
    assert str(sink.lines[0]["error"]).startswith("ConnectError")
    assert sink.settles == [(1, 0.0)]


def test_an_async_call_that_gets_no_reply_gives_its_reservation_back(gated: None) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(down)) as c:
            await c.post(URL, json=BODY)

    with pytest.raises(httpx2.ConnectError):
        asyncio.run(go())
    assert sink.settles == [(1, 0.0)]


@pytest.mark.design("budget.output_reserve")
def test_a_reservation_uses_max_tokens_or_the_default(gated: None) -> None:
    budget = Budget(limit=1000.0)
    install(make_plan(budget=1000.0, reserve_output_tokens=1000), budget)
    client().post(URL, json={**BODY, "max_tokens": 1})
    small = budget.ledger.spent + budget.ledger.reserved
    assert small < 0.0001  # one output token, not a thousand
    client().post(URL, json=BODY)
    # 1000 output tokens at $0.60 a million is $0.0006 more than 1 output token would be.
    assert budget.ledger.reserved - small == pytest.approx(0.0006, abs=0.00002)


class Chunks(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    """A reply that arrives in pieces, as a real server's does."""

    def __iter__(self) -> Iterator[bytes]:
        yield b'{"usage": {"prompt_tokens": 3,'
        yield b' "completion_tokens": 4}}'

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"usage": {"prompt_tokens": 3,'
        yield b' "completion_tokens": 4}}'


def streaming(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, stream=Chunks())


@pytest.mark.design("gate.reply_watched_as_it_passes")
def test_a_reply_in_pieces_is_passed_on_unchanged_and_read_as_it_goes_by(gated: None) -> None:
    sink, _ = install(make_plan())
    response = httpx2.Client(transport=httpx2.MockTransport(streaming)).post(URL, json=BODY)
    assert response.json()["usage"]["completion_tokens"] == 4
    assert sink.lines[0]["tokens"] == {"input": 3, "output": 4}


def test_a_reply_in_pieces_on_the_async_path(gated: None) -> None:
    sink, _ = install(make_plan())

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(streaming)) as c:
            await c.post(URL, json=BODY)

    asyncio.run(go())
    assert sink.lines[0]["tokens"] == {"input": 3, "output": 4}


@pytest.mark.design("gate.reply_cut_short")
def test_a_reply_the_agent_closes_early_still_records_the_call(gated: None) -> None:
    sink, _ = install(make_plan())
    with httpx2.Client(transport=httpx2.MockTransport(streaming)).stream("POST", URL, json=BODY):
        pass
    assert sink.lines[0]["reply_incomplete"] is True


def test_an_async_reply_closed_early_is_recorded_too(gated: None) -> None:
    sink, _ = install(make_plan())

    async def go() -> None:
        transport = httpx2.MockTransport(streaming)
        async with httpx2.AsyncClient(transport=transport) as c, c.stream("POST", URL, json=BODY):
            pass

    asyncio.run(go())
    assert sink.lines[0]["reply_incomplete"] is True


class Dropped(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    """A connection that drops after the 200."""

    def __iter__(self) -> Iterator[bytes]:
        yield b'{"partial":'
        raise httpx2.ReadError("connection lost")

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"partial":'
        raise httpx2.ReadError("connection lost")


def dropping(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, stream=Dropped())


@pytest.mark.design("gate.stream_failure_recorded")
def test_a_connection_dropped_midway_is_recorded_and_still_reaches_the_agent(gated: None) -> None:
    sink, _ = install(make_plan())
    with pytest.raises(httpx2.ReadError, match="connection lost"):
        httpx2.Client(transport=httpx2.MockTransport(dropping)).post(URL, json=BODY)
    line = sink.lines[0]
    assert line["error"] == "ReadError: connection lost" and line["reply_incomplete"] is True
    assert real_error(line) == "model:openai failed with ReadError"


def test_a_connection_dropped_midway_on_the_async_path(gated: None) -> None:
    sink, _ = install(make_plan())

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(dropping)) as c:
            await c.post(URL, json=BODY)

    with pytest.raises(httpx2.ReadError):
        asyncio.run(go())
    assert sink.lines[0]["error"] == "ReadError: connection lost"


@pytest.mark.design("record.side_folder_cap")
def test_a_side_folder_item_over_10_mb_is_cut_and_marked() -> None:
    sink = Sink()
    entry = state.blob(b"x" * (state.BLOB_CAP + 5), sink)
    assert entry["cut"] is True and entry["bytes"] == state.BLOB_CAP + 5
    assert len(next(iter(sink.blobs.values()))) == state.BLOB_CAP


def test_secrets_in_a_model_request_never_reach_the_side_folder() -> None:
    sink = Sink()
    state.blob(b'{"key": "sk-proj-' + b"a" * 30 + b'"}', sink)
    assert b"sk-proj" not in next(iter(sink.blobs.values()))


@pytest.mark.design("record.redacts_gate_fields")
def test_a_secret_in_the_path_or_the_model_name_never_reaches_a_line(gated: None) -> None:
    secret = "sk-proj-" + "a" * 30
    sink, _ = install(make_plan())
    client().post(
        f"https://api.openai.com/{secret}/chat/completions", json={**BODY, "model": secret}
    )
    assert secret not in json.dumps(sink.lines)
    assert "[secret removed]" in str(sink.lines[0]["path"])


def test_an_item_with_no_room_is_marked_unsaved() -> None:
    class Full(Sink):
        def put_blob(self, sha: str, data: bytes) -> bool:
            return False

    assert state.blob(b"x", Full())["unsaved"] is True


@pytest.mark.parametrize(
    ("raw", "encoding"),
    [(gzip.compress(b"hello"), "gzip"), (zlib.compress(b"hello"), "deflate"), (b"hello", "")],
)
def test_a_compressed_reply_is_read_for_its_counts(raw: bytes, encoding: str) -> None:
    assert http_gate.decoded(raw, encoding) == b"hello"


def test_a_reply_that_will_not_decompress_stays_as_it_is() -> None:
    assert http_gate.decoded(b"not gzip", "gzip") == b"not gzip"


def bare_library(**parts: object) -> types.ModuleType:
    module = types.ModuleType("httpx2")
    module.__dict__.update(parts)
    return module


def gate_for(plan: Plan) -> tuple[Sink, http_gate.Context, list[str]]:
    sink, stops = Sink(), []
    run = state.RunState(plan, sink, [])
    state.install(run)
    return sink, http_gate.Context(run, Budget(), sink, stopper(stops)), stops


class NoSend:
    pass


class WrongSend:
    def _send_single_request(self, request: object, extra: object) -> None: ...


# Present in the cases about the send function, so only that function can fail the check.
STREAMS = {"SyncByteStream": httpx2.SyncByteStream, "AsyncByteStream": httpx2.AsyncByteStream}


@pytest.mark.design("gate.structural_check")
@pytest.mark.parametrize(
    "parts",
    [
        {},
        {**STREAMS, "Client": NoSend, "AsyncClient": httpx2.AsyncClient},
        {**STREAMS, "Client": httpx2.Client, "AsyncClient": NoSend},
        {**STREAMS, "Client": WrongSend, "AsyncClient": httpx2.AsyncClient},
        {**STREAMS, "Client": httpx2.Client, "AsyncClient": WrongSend},
        {"Client": httpx2.Client, "AsyncClient": httpx2.AsyncClient},
        {
            "Client": httpx2.Client,
            "AsyncClient": httpx2.AsyncClient,
            "SyncByteStream": httpx2.SyncByteStream,
        },
    ],
    ids=[
        "nothing",
        "sync gone",
        "async gone",
        "sync changed",
        "async changed",
        "no stream types",
        "no async stream type",
    ],
)
def test_a_library_the_gate_cannot_replace_stops_the_program(
    gated: None, parts: dict[str, object]
) -> None:
    sink, ctx, stops = gate_for(make_plan())
    http_gate.patch(bare_library(**parts), ctx)
    assert sink.lines[0]["notice"] == "structure"
    assert len(stops) == 1 and "the program is stopped" in stops[0]


@pytest.mark.design("gate.structural_keeps_refusals")
def test_a_library_missing_only_what_faults_need_drops_faults_but_keeps_refusing(
    gated: None,
) -> None:
    plan = make_plan((Fault("model", "status 429", "always"),))
    sink, ctx, stops = gate_for(plan)
    library = bare_library(
        Client=httpx2.Client,
        AsyncClient=httpx2.AsyncClient,
        SyncByteStream=httpx2.SyncByteStream,
        AsyncByteStream=httpx2.AsyncByteStream,
    )
    http_gate.patch(library, ctx)
    assert not ctx.faults and stops == [] and "those do not" in str(sink.lines[0]["message"])
    assert client().post(URL, json=BODY).status_code == 200  # no fault: the call runs
    with pytest.raises(FaultlineRefused):  # and a write is still refused
        client().post("https://api.openai.com/v1/fine_tuning/jobs", json={})


@pytest.mark.design("gate.newer_version_warns")
def test_a_newer_version_passes_with_a_warning(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx2, "__version__", "2.99.0")
    sink, _ = install(make_plan())
    assert sink.lines[0]["notice"] == "newer_version"
    assert client().post(URL, json=BODY).status_code == 200


def test_a_version_it_cannot_read_is_not_a_warning() -> None:
    assert http_gate.version(types.ModuleType("x")) is None


def test_a_function_with_no_signature_cannot_be_gated() -> None:
    assert http_gate.params(print) is None or http_gate.params(1) is None


REDIRECT = "/v2/chat/completions"


def redirecting(target: str) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx2.Response(307, headers={"location": target})
        return httpx2.Response(200, json=REPLY)

    return httpx2.MockTransport(handler)


@pytest.mark.design("gate.redirect_budgeted")
def test_every_hop_of_a_redirect_is_budgeted(gated: None) -> None:
    budget = Budget(limit=100.0)
    sink, _ = install(make_plan(budget=100.0), budget)
    c = httpx2.Client(transport=redirecting(REDIRECT), follow_redirects=True)
    assert c.post(URL, json=BODY).status_code == 200
    assert [x["call"] for x in sink.lines] == ["model:openai", "model:openai"]
    assert len(sink.settles) == 2


@pytest.mark.design("gate.redirect_refused")
def test_a_redirect_to_a_write_on_a_model_host_is_refused(gated: None) -> None:
    sink, _ = install(make_plan())
    c = httpx2.Client(transport=redirecting("/v1/fine_tuning/jobs"), follow_redirects=True)
    with pytest.raises(FaultlineRefused, match="/v1/fine_tuning/jobs"):
        c.post(URL, json=BODY)
    assert sink.lines[-1]["reason"] == "web_write"


def to_elsewhere(request: httpx2.Request) -> httpx2.Response:
    if request.url.host == "api.openai.com":
        return httpx2.Response(
            307, headers={"location": "http://localhost:9000/v1/chat/completions"}
        )
    return httpx2.Response(200, json=REPLY)


@pytest.mark.design("gate.redirect_off_host_refused")
def test_a_redirect_off_the_model_hosts_is_refused_and_named(gated: None) -> None:
    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)
    c = httpx2.Client(transport=httpx2.MockTransport(to_elsewhere), follow_redirects=True)
    with pytest.raises(FaultlineRefused, match="localhost, which is not a model host"):
        c.post(URL, json=BODY)
    line = sink.lines[0]
    assert (line["action"], line["reason"]) == ("refused", "redirect_off_host")
    assert "localhost" in str(line["mistake"])
    assert budget.ledger.reserved > 0 and len(sink.settles) == 1
    assert sink.settles[0][1] == line["cost_usd"] and line["cost_estimated"] is True
    assert isinstance(line["cost_usd"], float) and line["cost_usd"] > 0


class Closing(httpx2.AsyncByteStream):
    """A reply body whose close hangs until cancelled, or fails."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.closing = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b""

    async def aclose(self) -> None:
        self.closing.set()
        if self.error is not None:
            raise self.error
        await asyncio.Event().wait()


def closing_redirect(stream: Closing) -> httpx2.AsyncClient:
    async def answer(request: httpx2.Request) -> httpx2.Response:
        headers = {"location": "http://localhost:9000/v1/chat/completions"}
        return httpx2.Response(307, headers=headers, stream=stream)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(answer))


@pytest.mark.design("gate.redirect_cancel_while_closing")
def test_a_cancel_while_closing_an_off_host_reply_keeps_its_line_and_settlement(
    gated: None,
) -> None:
    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)

    async def go() -> None:
        stream = Closing()
        task = asyncio.create_task(closing_redirect(stream).post(URL, json=BODY))
        await stream.closing.wait()
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    line = sink.lines[-1]
    assert (line["action"], line["reason"]) == ("refused", "redirect_off_host")
    assert len(sink.settles) == 1 and sink.settles[0][1] == line["cost_usd"]


def test_a_failed_close_of_an_off_host_reply_still_refuses(gated: None) -> None:
    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)

    async def go() -> None:
        await closing_redirect(Closing(OSError("dropped"))).post(URL, json=BODY)

    with pytest.raises(FaultlineRefused, match="not a model host"):
        asyncio.run(go())
    assert sink.lines[-1]["reason"] == "redirect_off_host" and len(sink.settles) == 1


@pytest.mark.design("gate.redirect_bad_location")
def test_a_redirect_to_a_broken_address_keeps_its_line_and_httpx2_error(gated: None) -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(307, headers={"location": "http://exa mple:notaport/x"})

    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)
    c = httpx2.Client(transport=httpx2.MockTransport(broken), follow_redirects=True)
    with pytest.raises(httpx2.RemoteProtocolError):
        c.post(URL, json=BODY)
    assert sink.lines[0]["status"] == 307
    assert len(sink.settles) == 1


@pytest.mark.design("gate.redirect_off_host_refused_async")
def test_a_redirect_off_the_model_hosts_is_refused_on_the_async_path(gated: None) -> None:
    sink, _ = install(make_plan())

    async def go() -> None:
        transport = httpx2.MockTransport(to_elsewhere)
        async with httpx2.AsyncClient(transport=transport, follow_redirects=True) as c:
            await c.post(URL, json=BODY)

    with pytest.raises(FaultlineRefused, match="not a model host"):
        asyncio.run(go())
    assert sink.lines[0]["reason"] == "redirect_off_host"


def test_a_redirect_to_a_named_gateway_is_followed(gated: None) -> None:
    def to_gateway(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.openai.com":
            target = "https://gateway.acme.internal/v1/chat/completions"
            return httpx2.Response(307, headers={"location": target})
        return httpx2.Response(200, json=REPLY)

    sink, _ = install(make_plan())
    c = httpx2.Client(transport=httpx2.MockTransport(to_gateway), follow_redirects=True)
    assert c.post(URL, json=BODY).status_code == 200
    assert len(sink.lines) == 2


@pytest.mark.design("gate.resend_decided_again")
def test_a_request_sent_again_is_decided_again(gated: None) -> None:
    install(make_plan((Fault("model", "status 429", "always"),)))
    c = client()
    request = c.build_request("POST", URL, json=BODY)
    assert [c.send(request).status_code for _ in range(3)] == [429, 429, 429]


@pytest.mark.design("record.no_query_string")
def test_a_query_string_never_reaches_the_record(gated: None) -> None:
    sink, _ = install(make_plan())
    with pytest.raises(FaultlineRefused):
        client().post("https://api.openai.com/v1/fine_tuning/jobs?api_key=hunter2", json={})
    assert "hunter2" not in json.dumps(sink.lines)


@pytest.mark.design("gate.host_and_path")
def test_a_chat_path_on_another_host_is_not_a_model_call(gated: None) -> None:
    sink, _ = install(make_plan())
    with pytest.raises(FaultlineRefused):
        client().post("https://chat.acme.internal/v1/chat/completions", json=BODY)
    assert sink.lines[0]["call"] == "http:chat.acme.internal"


def test_a_get_on_a_chat_path_is_not_a_model_call(gated: None) -> None:
    sink, _ = install(make_plan())
    client().get(URL)
    assert sink.lines[0]["kind"] == "http"


@pytest.mark.design("budget.interrupted_call_costs")
def test_a_call_that_was_sent_but_got_no_reply_is_charged_its_input(gated: None) -> None:
    def hung(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    sink, _ = install(make_plan(budget=1.0), Budget(limit=1.0))
    with pytest.raises(httpx2.ReadTimeout):
        httpx2.Client(transport=httpx2.MockTransport(hung)).post(URL, json=BODY)
    assert sink.settles[0][1] > 0
    assert sink.lines[0]["cost_estimated"] is True


@pytest.mark.design("budget.bad_usage_estimated")
@pytest.mark.parametrize(
    "usage",
    [
        {"total_tokens": 10},
        {"prompt_tokens": "9", "completion_tokens": 1},
        {"prompt_tokens": -5, "completion_tokens": 1},
        {"prompt_tokens": 10**400, "completion_tokens": 1},
        {"prompt_tokens": 9},
        {"prompt_tokens": 9, "completion_tokens": None},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 9, "completion_tokens": True},
    ],
)
def test_counts_that_make_no_sense_are_estimated(gated: None, usage: dict[str, object]) -> None:
    def odd(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=json.dumps({"usage": usage}).encode())

    sink, _ = install(make_plan())
    assert (
        httpx2.Client(transport=httpx2.MockTransport(odd)).post(URL, json=BODY).status_code == 200
    )
    line = sink.lines[0]
    assert line["cost_estimated"] is True
    assert isinstance(line["cost_usd"], float) and line["cost_usd"] >= 0


@pytest.mark.design("gate.cost_code_never_raises")
def test_a_failure_in_the_cost_code_never_reaches_the_agent(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(body: bytes) -> None:
        raise ArithmeticError

    sink, _ = install(make_plan())
    monkeypatch.setattr(provider_openai, "usage", boom)
    assert client().post(URL, json=BODY).status_code == 200
    assert sink.lines[0]["cost_usd"] is None


@pytest.mark.design("budget.no_price_is_null")
def test_with_no_price_the_cost_is_null_not_zero(gated: None) -> None:
    sink, _ = install(make_plan())
    client().post(URL, json={**BODY, "model": "acme-llama-70b"})
    assert sink.lines[0]["cost_usd"] is None and sink.lines[0]["cost_estimated"] is False


@pytest.mark.design("budget.reads_cost_nothing")
def test_a_free_read_on_a_priced_model_records_no_cost(gated: None) -> None:
    sink, _ = install(make_plan())
    url = "https://api.openai.com/v1/responses/input_tokens"
    client().post(url, json={"model": "gpt-4o-mini", "input": "hi"})
    assert (sink.lines[0]["cost_usd"], sink.lines[0]["cost_estimated"]) == (0.0, False)


def test_an_absurd_max_tokens_does_not_reach_the_agent(gated: None) -> None:
    budget = Budget(limit=1000.0)
    install(make_plan(budget=1000.0), budget)
    assert client().post(URL, json={**BODY, "max_tokens": 10**400}).status_code == 200


@pytest.mark.design("record.cut_size_true")
def test_a_huge_reply_records_its_true_size(gated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_gate, "BLOB_CAP", 10)
    monkeypatch.setattr(state, "BLOB_CAP", 10)
    sink, _ = install(make_plan())
    response = httpx2.Client(transport=httpx2.MockTransport(streaming)).post(URL, json=BODY)
    assert len(response.content) == 55
    result = sink.lines[0]["result"]
    assert isinstance(result, dict) and result["bytes"] == 55 and result["cut"] is True


@pytest.mark.design("gate.unreadable_body_refused")
@pytest.mark.parametrize("content", [gzip.compress(b'{"tools": []}'), b""])
def test_a_chat_body_the_gate_cannot_read_is_refused(gated: None, content: bytes) -> None:
    sink, _ = install(make_plan())
    with pytest.raises(FaultlineRefused, match="cannot read"):
        client().post(URL, content=content)
    assert sink.lines[0]["reason"] == "unreadable_body"


def test_a_streamed_chat_body_is_refused(gated: None) -> None:
    install(make_plan())
    with pytest.raises(FaultlineRefused, match="cannot read"):
        client().post(URL, content=iter([b'{"tools": []}']))


@pytest.mark.design("gate.exit_finishes_open_replies")
def test_at_exit_a_reply_never_read_is_recorded_and_settled(gated: None) -> None:
    budget = Budget(limit=1.0)
    sink, ctx = install(make_plan(budget=1.0), budget)
    c = httpx2.Client(transport=httpx2.MockTransport(streaming))
    response = c.send(c.build_request("POST", URL, json=BODY), stream=True)
    assert sink.lines == [] and len(ctx.open) == 1
    ctx.close_open()
    assert sink.lines[0]["reply_incomplete"] is True and len(sink.settles) == 1
    assert ctx.open == set()
    response.close()
    assert len(sink.lines) == 1  # finished once


@pytest.mark.design("gate.injected_reply_elapsed")
def test_an_injected_reply_is_complete_for_the_code_reading_it(gated: None) -> None:
    install(make_plan((Fault("model", "status 429", "always"),)))
    response = client().post(URL, json=BODY)
    assert response.elapsed.total_seconds() == 0


@pytest.mark.design("model.error_body_reaches_agent")
def test_the_library_reads_the_injected_body(gated: None) -> None:
    install(make_plan((Fault("model", "status 429", "always"),)))
    api = openai.OpenAI(api_key="test", max_retries=0, http_client=client())
    with pytest.raises(openai.RateLimitError) as caught:
        api.chat.completions.create(model="gpt-4o-mini", messages=[])
    assert caught.value.code == "rate_limit_exceeded"
    assert (
        isinstance(caught.value.body, dict) and "Rate limit reached" in caught.value.body["message"]
    )


def two_calls() -> tuple[Any, Any]:
    return httpx2.Request("POST", URL, json=BODY), httpx2.Request("POST", URL, json={"a": 1})


@pytest.mark.design("schedule.key_names_the_sender")
def test_identical_requests_from_two_senders_keep_their_own_numbers(gated: None) -> None:
    _, ctx = install(make_plan())
    request, _ = two_calls()
    body = http_gate.body_of(request) or b""
    one, two = (http_gate.describe(ctx, request, who, body) for who in ("sender-a", "sender-b"))
    assert one is not None and two is not None and one.key != two.key


@pytest.mark.design("schedule.key_names_the_body")
def test_two_requests_from_one_sender_keep_their_own_numbers(gated: None) -> None:
    plan = make_plan((Fault("model", "status 429", "requests 1"),))
    _, ctx = install(plan)
    first, second = two_calls()
    first_body, second_body = (http_gate.body_of(r) or b"" for r in (first, second))
    counters = Counters(plan)
    a = http_gate.describe(ctx, first, "sender-a", first_body)
    b = http_gate.describe(ctx, second, "sender-a", second_body)
    assert a is not None and b is not None
    counters.note(a)
    counters.note(b)
    retry_of_a = dataclasses.replace(a, retry=1)
    assert counters.note(retry_of_a) == [1]  # the first request's number, not the second's


@pytest.mark.design("model.read_paths_free")
def test_a_moderation_is_a_labelled_read_that_needs_no_price(gated: None) -> None:
    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)
    url = "https://api.openai.com/v1/moderations"
    assert (
        client().post(url, json={"model": "omni-moderation-latest", "input": "hi"}).status_code
        == 200
    )
    assert sink.lines[0]["call"] == "model:openai/moderations"
    assert budget.ledger.reserved == 0.0


@pytest.mark.design("budget.price_line_is_toml")
def test_the_suggested_price_line_is_valid_toml_for_a_gateway_name(gated: None) -> None:
    install(make_plan(budget=1.0), Budget(limit=1.0))
    model = "acme/llama-3.1"
    with pytest.raises(FaultlineRefused) as refused:
        client().post(URL, json={**BODY, "model": model})
    line = str(refused.value).split("such as ")[1].split(" (dollars")[0]
    assert tomllib.loads(line) == {model: {"input": 0.15, "output": 0.60}}


@pytest.mark.design("gate.refusal_text_redacted")
def test_a_secret_never_reaches_the_agent_in_a_refusal(gated: None) -> None:
    secret = "sk-proj-" + "a" * 30
    install(make_plan(budget=1.0), Budget(limit=1.0))
    with pytest.raises(FaultlineRefused) as refused:
        client().post(URL, json={**BODY, "model": secret})
    assert secret not in str(refused.value)


@pytest.mark.design("record.model_lines_kept")
def test_a_model_line_waits_for_room_rather_than_being_dropped(gated: None) -> None:
    kept: list[bool] = []

    class Keeping(Sink):
        def put(self, line: dict[str, object], *, keep: bool = False) -> None:
            kept.append(keep)

    run = state.RunState(make_plan(), Keeping(), [])
    state.install(run)
    http_gate.patch(httpx2, http_gate.Context(run, Budget(), Sink(), stopper([])))
    client().post(URL, json=BODY)
    assert kept == [True]


@pytest.mark.design("gate.line_fields_scrubbed")
def test_a_secret_in_a_list_on_a_line_is_removed(gated: None) -> None:
    secret = "sk-proj-" + "a" * 30
    sink, _ = install(make_plan())
    spec = {"type": "function", "function": {"name": secret, "parameters": {}}}
    with pytest.raises(FaultlineRefused):
        client().post(URL, json={**BODY, "tools": [spec]})
    assert sink.lines[0]["unseen_tools"] == ["[secret removed]"]
