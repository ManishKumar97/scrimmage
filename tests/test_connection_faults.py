"""refused, reset and dns on httpx and httpx2, sync and async, in one process: the error the
library raises, its cause, the line, and that nothing is sent or reserved."""

import asyncio
import errno
import os
import socket
import sys
import types
from collections.abc import Iterator
from typing import Any

import anyio
import httpcore
import httpcore2
import httpx
import httpx2
import openai
import pytest

from scrimmage_core import http_gate, state
from scrimmage_core.budget import Shortfall
from scrimmage_core.config import Fault
from test_http_gate import BODY, URL, Budget, Sink, make_plan

LIBRARIES = {"httpx": httpx, "httpx2": httpx2}
CORE = {"httpx": httpcore, "httpx2": httpcore2}
WEB = "https://inventory.acme.internal/stock/SKU-42"
SYNC_MESSAGE = {
    "refused": f"[Errno {errno.ECONNREFUSED}] {os.strerror(errno.ECONNREFUSED)}",
    "reset": f"[Errno {errno.ECONNRESET}] {os.strerror(errno.ECONNRESET)}",
    "dns": f"[Errno {socket.EAI_NONAME}] Name or service not known",
}
# anyio wraps the OSError before httpcore does on the async path.
ASYNC_MESSAGE = {**SYNC_MESSAGE, "refused": "All connection attempts failed", "reset": ""}
CAUSE: dict[str, tuple[type[OSError], int]] = {
    "refused": (ConnectionRefusedError, errno.ECONNREFUSED),
    "reset": (ConnectionResetError, errno.ECONNRESET),
    "dns": (socket.gaierror, socket.EAI_NONAME),
}
PATHS = [(name, sync) for name in LIBRARIES for sync in (True, False)]
PATH_IDS = [f"{name} {'sync' if sync else 'async'}" for name, sync in PATHS]


class Counting(Budget):
    """A budget that counts what is asked of it."""

    def __init__(self) -> None:
        super().__init__(limit=1.0)
        self.asked = 0

    def reserve(
        self, run: str, usd: float, label: str = "", ticket: int | None = None
    ) -> int | Shortfall:
        self.asked += 1
        return super().reserve(run, usd, label, ticket)


def never_stopped(why: str) -> None:
    """A gate stops the program only for a library it cannot gate, which no test here is."""
    raise AssertionError(why)


@pytest.fixture
def gated() -> Iterator[None]:
    sends = {
        module: (module.Client._send_single_request, module.AsyncClient._send_single_request)
        for module in LIBRARIES.values()
    }
    yield
    for module, (sync_send, async_send) in sends.items():
        module.Client._send_single_request = sync_send  # type: ignore[method-assign]
        module.AsyncClient._send_single_request = async_send  # type: ignore[method-assign]
    state.install(None)


def install(word: str, call: str = "http:inventory.acme.internal") -> tuple[Sink, Counting]:
    """A run with this connection fault on every call, both libraries gated."""
    sink, budget = Sink(), Counting()
    plan = make_plan((Fault(call, word, "always"),), budget=1.0)
    run = state.RunState(plan, sink, [])
    state.install(run)
    ctx = http_gate.Context(run, budget, sink, never_stopped)
    for name, module in LIBRARIES.items():
        http_gate.patch(module, ctx, name)
    return sink, budget


def send(name: str, sync: bool, reached: list[object], url: str = WEB) -> None:
    """One request through that library and path, to a transport that keeps what reaches it."""
    module: Any = LIBRARIES[name]

    def answer(request: Any) -> Any:
        reached.append(request)
        return module.Response(200, json={"choices": []})

    transport = module.MockTransport(answer)
    method = "POST" if url == URL else "GET"
    body = BODY if url == URL else None
    if sync:
        module.Client(transport=transport).request(method, url, json=body)
        return

    async def go() -> None:
        async with module.AsyncClient(transport=transport) as client:
            await client.request(method, url, json=body)

    asyncio.run(go())


def raised(name: str, sync: bool, url: str = WEB) -> BaseException:
    reached: list[object] = []
    with pytest.raises(Exception) as caught:
        send(name, sync, reached, url)
    assert reached == [], "the gate sent the request before it raised"
    return caught.value


@pytest.mark.design("fault.connection_refused")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_refused_raises_the_librarys_connect_error(gated: None, name: str, sync: bool) -> None:
    install("refused")
    error = raised(name, sync)
    assert type(error) is LIBRARIES[name].ConnectError
    assert str(error) == (SYNC_MESSAGE if sync else ASYNC_MESSAGE)["refused"]


@pytest.mark.design("fault.connection_dns")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_dns_raises_the_librarys_connect_error(gated: None, name: str, sync: bool) -> None:
    install("dns")
    error = raised(name, sync)
    assert type(error) is LIBRARIES[name].ConnectError
    assert str(error) == SYNC_MESSAGE["dns"]  # the resolver fails before anyio is involved


@pytest.mark.design("fault.connection_reset")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_reset_raises_the_librarys_read_error(gated: None, name: str, sync: bool) -> None:
    install("reset")
    error = raised(name, sync)
    assert type(error) is LIBRARIES[name].ReadError
    assert str(error) == (SYNC_MESSAGE if sync else ASYNC_MESSAGE)["reset"]


@pytest.mark.design("fault.connection_cause")
@pytest.mark.parametrize("word", sorted(CAUSE))
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_the_cause_is_the_os_error_a_real_failure_has(
    gated: None, word: str, name: str, sync: bool
) -> None:
    """httpx raises from httpcore's error, which raises from None over the OSError; on the
    async path anyio's link sits between them for a refused connection and a reset."""
    install(word)
    error = raised(name, sync)
    kind, number = CAUSE[word]
    middle = error.__cause__
    core = CORE[name]
    assert type(middle) is getattr(core, type(error).__name__), f"{word}: cause {middle!r}"
    assert middle is not None and error.__context__ is middle and error.__suppress_context__
    assert middle.__cause__ is None and middle.__suppress_context__  # raise ... from None
    below = middle.__context__
    if not sync and word != "dns":
        anyio_link = OSError if word == "refused" else anyio.BrokenResourceError
        assert type(below) is anyio_link and below.__suppress_context__, f"{word}: {below!r}"
        below = below.__cause__
    assert type(below) is kind, f"{word}: the OSError is {below!r}"
    assert isinstance(below, OSError) and below.errno == number


@pytest.mark.design("fault.connection_costs_nothing")
@pytest.mark.parametrize("word", sorted(CAUSE))
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_connection_fault_on_a_model_call_reserves_and_costs_nothing(
    gated: None, word: str, name: str, sync: bool
) -> None:
    sink, budget = install(word, "model")
    raised(name, sync, URL)
    assert budget.asked == 0 and sink.settles == []
    (line,) = [x for x in sink.lines if x.get("kind") == "model"]
    assert line["action"] == "fault" and line.get("cost_usd", 0) == 0


@pytest.mark.design("fault.connection_openai_retries")
@pytest.mark.parametrize("word", sorted(CAUSE))
def test_the_openai_library_retries_a_connection_fault_twice_then_raises_api_connection_error(
    gated: None, word: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The library's backoff between attempts, cut so the test does not wait for it.
    monkeypatch.setattr(openai.OpenAI, "_calculate_retry_timeout", lambda *_: 0.0)
    sink, budget = install(word, "model")
    reached: list[object] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        reached.append(request)
        return httpx2.Response(200, json={"choices": []})

    http_client = httpx2.Client(transport=httpx2.MockTransport(answer))
    api = openai.OpenAI(api_key="test", http_client=http_client)
    with pytest.raises(openai.APIConnectionError) as caught:
        api.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    assert isinstance(caught.value.__cause__, httpx2.TransportError)
    faults = [(x["action"], x.get("retry")) for x in sink.lines if x.get("kind") == "model"]
    assert faults == [("fault", 0), ("fault", 1), ("fault", 2)]
    assert reached == [] and budget.asked == 0


@pytest.mark.design("fault.connection_line")
@pytest.mark.parametrize(
    ("word", "error"),
    [("refused", "ConnectError"), ("dns", "ConnectError"), ("reset", "ReadError")],
)
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_the_line_names_the_error_class(
    gated: None, word: str, error: str, name: str, sync: bool
) -> None:
    sink, _ = install(word)
    raised(name, sync)
    (line,) = [x for x in sink.lines if x.get("kind") == "http"]
    assert line["action"] == "fault" and line["call"] == "http:inventory.acme.internal"
    assert str(line["error"]).split(":")[0] == error


@pytest.mark.design("gate.structural_keeps_refusals")
def test_a_library_without_the_connection_errors_drops_faults(gated: None) -> None:
    """The gate builds these from the library, so one that lacks them takes no fault."""
    sink = Sink()
    run = state.RunState(make_plan((Fault("model", "refused", "always"),)), sink, [])
    state.install(run)
    ctx = http_gate.Context(run, Budget(), sink, never_stopped)
    parts = {n: getattr(httpx2, n) for n in dir(httpx2) if n not in ("ConnectError", "ReadError")}
    library = types.ModuleType("httpx2")
    library.__dict__.update(parts)
    http_gate.patch(library, ctx)
    assert not ctx.faults and "those do not" in str(sink.lines[0]["message"])


@pytest.mark.design("fault.connection_cause")
def test_a_client_that_never_loaded_httpcore_gets_no_middle_link(
    gated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport of the team's own never loads httpcore, so no real failure has its link."""
    monkeypatch.delitem(sys.modules, "httpcore2")
    install("refused")
    error = raised("httpx2", True)
    assert type(error.__cause__) is ConnectionRefusedError and error.__suppress_context__
    assert "httpcore2" not in sys.modules  # the gate did not import it
