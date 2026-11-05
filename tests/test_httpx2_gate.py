"""The httpx2 gate in one process, with lines kept in a list and a mock transport."""

import asyncio
import gzip
import itertools
import time
import types
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx2
import pytest

from faultline_sdk import httpx2_gate, state
from faultline_sdk.budget import Ledger
from faultline_sdk.client import ControlClient
from faultline_sdk.config import Fault, ModelHost
from faultline_sdk.errors import FaultlineRefused
from faultline_sdk.plan import Plan

URL = "https://api.openai.com/v1/chat/completions"
BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
REPLY = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class Sink:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []
        self.blobs: dict[str, bytes] = {}
        self.posts: list[tuple[str, dict[str, object]]] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        self.lines.append(line)

    def put_blob(self, sha: str, data: bytes) -> bool:
        self.blobs[sha] = data
        return True

    def put_post(self, path: str, payload: dict[str, object]) -> bool:
        self.posts.append((path, payload))
        return True


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


def install(plan: Plan, client: Any = None) -> tuple[Sink, httpx2_gate.Context]:
    sink = Sink()
    run = state.RunState(plan, sink, [])
    state.install(run)
    ctx = httpx2_gate.Context(run, client or Budget(), sink)
    httpx2_gate.patch(httpx2, ctx)
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


@pytest.mark.design("gate.other_hosts_untouched")
def test_a_host_that_is_not_a_model_host_is_left_alone(gated: None) -> None:
    sink, _ = install(make_plan())
    response = client().post("https://orders.acme.internal/refunds", json={})
    assert response.status_code == 200 and sink.lines == []


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
    httpx2_gate.patch(httpx2, ctx)  # a second patch does not wrap twice
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
    path, payload = sink.posts[0]
    assert path == "/v1/budget/settle" and payload["reservation"] == 1


@pytest.mark.design("budget.failed_call_gives_back")
def test_a_call_that_gets_no_reply_gives_its_reservation_back(gated: None) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    budget = Budget(limit=1.0)
    sink, _ = install(make_plan(budget=1.0), budget)
    with pytest.raises(httpx2.ConnectError):
        httpx2.Client(transport=httpx2.MockTransport(down)).post(URL, json=BODY)
    assert str(sink.lines[0]["error"]).startswith("ConnectError")
    assert sink.posts[0][1]["usd"] == 0.0


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
    assert sink.posts[0][1]["usd"] == 0.0


@pytest.mark.design("budget.output_reserve")
def test_a_reservation_uses_max_tokens_or_the_default(gated: None) -> None:
    budget = Budget(limit=1000.0)
    install(make_plan(budget=1000.0, reserve_output_tokens=1000), budget)
    client().post(URL, json={**BODY, "max_tokens": 1})
    small = budget.ledger.reserved
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
    assert httpx2_gate.decoded(raw, encoding) == b"hello"


def test_a_reply_that_will_not_decompress_stays_as_it_is() -> None:
    assert httpx2_gate.decoded(b"not gzip", "gzip") == b"not gzip"


@pytest.mark.design("gate.structural_check")
def test_a_library_without_the_gated_function_loses_faults_and_is_reported() -> None:
    plan = make_plan((Fault("model", "status 429", "always"),))
    sink = Sink()
    ctx = httpx2_gate.Context(state.RunState(plan, sink, []), Budget(), sink)
    broken = types.ModuleType("httpx2")
    httpx2_gate.patch(broken, ctx)
    assert sink.lines[0]["notice"] == "structure" and not ctx.faults


@pytest.mark.design("gate.structural_keeps_refusals")
def test_with_a_failed_check_the_gate_drops_faults_but_keeps_refusing(gated: None) -> None:
    plan = make_plan((Fault("model", "status 429", "always"),))
    sink = Sink()
    run = state.RunState(plan, sink, [])
    state.install(run)
    ctx = httpx2_gate.Context(run, Budget(), sink)
    fake = types.ModuleType("httpx2")
    fake.Client = httpx2.Client  # type: ignore[attr-defined]
    fake.AsyncClient = httpx2.AsyncClient  # type: ignore[attr-defined]
    fake.__version__ = httpx2.__version__  # type: ignore[attr-defined]
    fake.Response = httpx2.Response  # type: ignore[attr-defined]
    # No ReadTimeout or SyncByteStream: the check fails, the function is still there.
    fake.SyncByteStream = httpx2.SyncByteStream  # type: ignore[attr-defined]
    httpx2_gate.patch(fake, ctx)
    assert not ctx.faults
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
    assert httpx2_gate.version(types.ModuleType("x")) is None
