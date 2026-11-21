"""What a process knows and what a run knows, for a service that serves many runs at once.

Every gate reads its run at each call, from the slot the entry gate sets. These tests swap
two runs in that slot between calls and check that each call lands in its own run's record,
and that what a process learns with no run, such as an MCP tools/list at startup, is seen by
every later run.
"""

import asyncio
import contextvars
import importlib
import pkgutil
import sys
import types
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any, cast

import httpx2
import pytest

import scrimmage
from scrimmage import (
    asgi_gate,
    http_gate,
    langchain_gate,
    mcp_gate,
    process_gate,
    protocol,
    state,
    tool_gate,
)
from scrimmage.client import ControlClient, Senders
from scrimmage.config import Mock
from scrimmage.errors import ScrimmageRefused
from scrimmage.executor import Executor
from scrimmage.plan import Plan
from service_harness import drive
from test_http_gate import Budget
from test_langgraph import Shape, base_tool, module
from test_mcp import library as mcp_library

PLAIN = state.Plain()  # one plain request, still being served

API = "https://api.test/items"


class Sink:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.blobs: dict[str, bytes] = {}

    def put(self, line: dict[str, Any], *, keep: bool = False) -> None:
        del keep
        self.lines.append(line)

    def put_blob(self, sha: str, data: bytes) -> bool:
        self.blobs[sha] = data
        return True

    def put_settle(self, reservation: int, usd: float) -> None:
        del reservation, usd

    def calls(self) -> list[str]:
        return [str(x["call"]) for x in self.lines if "call" in x]


def a_plan(name: str, mocks: tuple[Mock, ...] = (), real: tuple[str, ...] = ()) -> Plan:
    return Plan(name, "t", "baseline", mocks, (), (), real, 60.0)


@pytest.fixture
def service() -> Iterator[state.ProcessState]:
    """A service process with no run of its own, as each uvicorn worker is."""
    process = state.ProcessState(service=True)
    stops: list[str] = []
    process.stop = stops.append
    state.install_process(process)
    yield process
    state.install_process(None)


def run_in(process: state.ProcessState, name: str, mocks: tuple[Mock, ...] = ()) -> state.RunState:
    sink = Sink()
    return state.RunState(a_plan(name, mocks), sink, [], blobs=sink, process=process)


def sink_of(run: state.RunState) -> Sink:
    return cast(Sink, run.sink)


def joined(run: state.RunState) -> contextvars.Context:
    """A context whose slot holds this run, as the entry gate makes for a run request."""
    context = contextvars.copy_context()
    context.run(state.SLOT.set, state.Joined(run.plan.run, run))
    return context


def plain() -> contextvars.Context:
    context = contextvars.copy_context()
    context.run(state.SLOT.set, PLAIN)
    return context


class Reply:
    def __init__(self, tools: list[dict[str, object]]) -> None:
        self.tools = tools

    def model_dump(self, **_: object) -> dict[str, object]:
        return {"tools": self.tools}


class Request:
    def __init__(self, method: str, params: dict[str, object] | None = None) -> None:
        self.method = method
        self.params = params or {}

    def model_dump(self, **_: object) -> dict[str, object]:
        return {"method": self.method, "params": self.params}


class Dispatcher:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.sent: list[object] = []

    async def send_raw_request(self, request: object, *_: object) -> object:
        self.sent.append(request)
        return self.reply


def mcp_session(module_: types.ModuleType, reply: object) -> Any:
    session = module_.ClientSession()
    session._dispatcher = Dispatcher(reply)
    return session


@pytest.mark.design("service.tools_listed_at_startup")
def test_a_tool_listed_with_no_run_is_seen_by_every_later_run(
    service: state.ProcessState,
) -> None:
    lib = mcp_library()
    mcp_gate.patch(lib, mcp_gate.Gate(service, None, service.stop))
    session = mcp_session(lib, Reply([{"name": "lookup_order"}]))
    # At startup: no run, as a service lists its MCP tools in its lifespan.
    reply = asyncio.run(session.send_request(Request("tools/list"), None))
    assert isinstance(reply, Reply)
    later = [run_in(service, "run-a"), run_in(service, "run-b")]
    assert all("lookup_order" in run.seen for run in later)
    assert all(sink_of(run).lines == [] for run in later)  # nothing was recorded for a run


@pytest.mark.design("service.no_gate_keeps_a_run")
def test_no_gate_keeps_a_run_state_between_calls(service: state.ProcessState) -> None:
    """Each gate kind, called under run A then run B: every line lands in its own run."""
    mocks = (
        Mock("tool:lookup", "found", None),
        Mock("http:api.test", None, None, method="GET", json={"ok": True}),
        Mock("mcp:lookup_order", "shipped", None),
    )
    a, b = run_in(service, "run-a", mocks), run_in(service, "run-b", mocks)

    def lookup(order: str) -> str:
        raise AssertionError("a mocked tool never runs")

    tool = tool_gate.gate(lookup, "lookup")
    http_gate.patch(httpx2, http_gate.Context(service, Budget(), None, service.stop))
    lib = mcp_library()
    mcp_gate.patch(lib, mcp_gate.Gate(service, None, service.stop))
    session = mcp_session(lib, Reply([{"name": "lookup_order"}]))
    asyncio.run(session.send_request(Request("tools/list"), None))  # at startup, with no run
    hook = process_gate.hook_for(lambda: None)
    blind = (sys.executable, [sys.executable, "-c", "pass"], None, {"PATH": "/usr/bin"})
    langchain = langchain_gate.gate(Shape._run, Exception)

    class Named(Shape):
        func = None

    for run in (a, b, a, b):
        context = joined(run)
        assert context.run(tool, "1") == "found"
        client = httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(500)))
        assert context.run(client.get, API).json() == {"ok": True}
        call = Request("tools/call", {"name": "lookup_order", "arguments": {}})
        context.run(asyncio.run, session.send_request(call, None))
    for run in (a, b):  # the gates that refuse: a run's first refusal poisons it, so last
        context = joined(run)
        with pytest.raises(ScrimmageRefused):
            context.run(hook, "subprocess.Popen", blind)
        with pytest.raises(ScrimmageRefused):
            context.run(langchain, Named(name="write"), {})
    for run in (a, b):
        calls = sink_of(run).calls()
        assert calls.count("tool:lookup") == 2, calls
        assert calls.count("http:api.test") == 2, calls
        assert calls.count("mcp:lookup_order") == 2, calls
        assert calls.count("process:python") == 1, calls
        assert calls.count("tool:write") == 1, calls
        assert {x["run"] for x in sink_of(run).lines} == {run.plan.run}


@pytest.mark.design("service.notice_with_no_run")
def test_a_notice_with_no_run_goes_to_the_startup_report(
    service: state.ProcessState, capsys: pytest.CaptureFixture[str]
) -> None:
    library = types.ModuleType("httpx2")
    # Subclasses, so the gate this patches never reaches the real classes other tests use.
    library.__dict__.update(
        {
            name: type(name, (getattr(httpx2, name),), {})
            for name in ("Client", "AsyncClient", "SyncByteStream", "AsyncByteStream")
        },
        __version__="2.99.0",
    )
    http_gate.patch(library, http_gate.Context(service, Budget(), None, service.stop))
    kinds = [entry["notice"] for entry in service.report]
    assert kinds == ["structure", "newer_version"], service.report
    err = capsys.readouterr().err
    assert "lacks what Scrimmage builds faults and mocks from" in err
    assert "is newer than the versions" in err


def scrimmage_modules() -> Iterator[types.ModuleType]:
    for info in pkgutil.walk_packages(scrimmage.__path__, "scrimmage."):
        if info.name.startswith("scrimmage._boot") or info.name.endswith("__main__"):
            continue
        yield importlib.import_module(info.name)


@pytest.mark.design("service.every_contextvar_carried")
def test_every_contextvar_in_scrimmage_is_carried() -> None:
    found = {
        (mod.__name__, name): value
        for mod in scrimmage_modules()
        for name, value in vars(mod).items()
        if isinstance(value, ContextVar)
    }
    names = {(mod, name) for mod, name in found}
    # The ones the design names, so a walk that found nothing could not pass.
    assert {
        ("scrimmage.state", "SLOT"),
        ("scrimmage.state", "PARENT"),
        ("scrimmage.mcp_gate", "FORWARDING"),
        ("scrimmage.http_gate", "MCP_TRANSPORT"),
        ("scrimmage.process_gate", "EXEMPT"),
        ("scrimmage.langchain_gate", "_inside"),
    } <= names
    carried = {id(var) for var in state.CARRIED}
    missing = [f"{mod}.{name}" for (mod, name), var in found.items() if id(var) not in carried]
    assert missing == []


@pytest.mark.design("service.web_replies_per_run")
def test_web_gate_open_replies_and_model_paths_are_per_run(service: state.ProcessState) -> None:
    a, b = (
        state.RunState(a_plan(name, real=("http:models.test",)), Sink(), [], process=service)
        for name in ("run-a", "run-b")
    )
    ctx = http_gate.Context(service, Budget(), Sink(), service.stop)
    http_gate.patch(httpx2, ctx)

    def streaming(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=httpx2.ByteStream(b'{"id": 1}'))

    url = "https://models.test/v1/chat/completions"
    client = httpx2.Client(transport=httpx2.MockTransport(streaming))
    response = joined(a).run(client.send, client.build_request("POST", url, json={}), stream=True)
    joined(b).run(client.post, url, json={})
    assert len(a.open) == 1 and b.open == set()
    # The model path note is made once per run, not once per process.
    assert [x["notice"] for x in sink_of(b).lines if "notice" in x] == ["model_path"]
    response.close()
    assert a.open == set()
    assert [x["notice"] for x in sink_of(a).lines if "notice" in x] == ["model_path"]


# M3: in a service, a gate path that refuses or stops with no run calls the original.


def ready(service: state.ProcessState) -> list[str]:
    """The service is listening, as it is from the entry gate's first call."""
    service.listening = True
    stops: list[str] = []
    service.stop = stops.append
    return stops


@pytest.mark.design("service.no_run_untouched")
def test_langchain_override_refusal_calls_the_original_with_no_run(
    service: state.ProcessState,
) -> None:
    base = base_tool()
    langchain_gate.patch(module(BaseTool=base, ToolException=Exception), service, pytest.fail)

    class Own(base):  # type: ignore[valid-type, misc]
        def invoke(self, tool_input: object) -> str:
            return "the override ran"

    tool = Own(name="own")
    assert plain().run(tool.invoke, {}) == "the override ran"
    assert tool.invoke({}) == "the override ran"  # outside any request too
    run = run_in(service, "run-a")
    with pytest.raises(ScrimmageRefused, match="overrides invoke"):
        joined(run).run(tool.invoke, {})


@pytest.mark.design("service.no_run_untouched")
def test_langchain_checked_entry_calls_the_original_with_no_run(
    service: state.ProcessState,
) -> None:
    base = base_tool()
    langchain_gate.patch(module(BaseTool=base, ToolException=Exception), service, pytest.fail)

    class Plain(base):  # type: ignore[valid-type, misc]
        pass

    tool = Plain(name="plain")
    tool._run = lambda *a, **k: "set on the tool"
    assert plain().run(tool.run, {}) == "set on the tool"
    run = run_in(service, "run-a")
    with pytest.raises(ScrimmageRefused, match="set on the tool itself"):
        joined(run).run(tool.run, {})


def assert_structure_and_untouched(service: state.ProcessState, stops: list[str]) -> None:
    """The process is broken, not stopped: a run request then gets 503 structure, before any
    control request, and a plain request reaches the app untouched."""
    assert stops == [] and service.broken is not None
    assert [entry["notice"] for entry in service.report] == ["structure"]
    token = "t" * 64
    unreachable = ControlClient("http://127.0.0.1:9", token)  # a claim would fail: none is made
    pool = Executor()
    asgi_gate.GATE = asgi_gate.Gate(service, unreachable, token, [], (), (), Senders(), pool)
    reached: list[object] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"plain"})

    name = "run-0123456789ab"
    header = (b"x-scrimmage-run", f"{name}.{protocol.run_mac(token, name)}".encode())
    try:
        refused = asyncio.run(drive(asgi_gate.Entry(app), headers=[header]))
        plain = asyncio.run(drive(asgi_gate.Entry(app)))
    finally:
        asgi_gate.GATE = None
    assert refused.status == 503 and refused.headers["x-scrimmage-error"] == "structure"
    assert (plain.status, plain.body) == (200, b"plain") and len(reached) == 1


@pytest.mark.design("service.broken_after_listening")
def test_a_missing_listed_tool_with_no_run_marks_the_process_broken_and_imports(
    service: state.ProcessState,
) -> None:
    stops = ready(service)
    tools = types.ModuleType("acme_service_tools")
    tools.get_order = lambda order: f"real {order}"  # type: ignore[attr-defined]
    tool_gate.swap(tools, ["missing", "get_order"])
    assert_structure_and_untouched(service, stops)
    assert "has no function missing" in str(service.broken)
    # The import went on, and the other tool is still gated.
    assert tool_gate.is_gate(tools.get_order)  # type: ignore[attr-defined]
    assert plain().run(tools.get_order, "1") == "real 1"  # type: ignore[attr-defined]


@pytest.mark.design("service.broken_after_listening")
def test_a_missing_listed_tool_before_listening_stops_the_service(
    service: state.ProcessState,
) -> None:
    stops: list[str] = []
    service.stop = stops.append
    with pytest.raises(tool_gate.Ungated):
        tool_gate.cannot_gate("missing", "missing is not there")
    assert stops == ["missing is not there"] and service.broken is None


@pytest.mark.design("service.broken_after_listening")
def test_an_ungatable_httpx_after_listening_marks_broken_and_does_not_exit(
    service: state.ProcessState,
) -> None:
    stops = ready(service)
    library = types.ModuleType("httpx2")
    library.Client = httpx2.Client  # type: ignore[attr-defined]
    before = httpx2.Client._send_single_request
    http_gate.patch(library, http_gate.Context(service, Budget(), None, service.stop))
    assert_structure_and_untouched(service, stops)
    assert httpx2.Client._send_single_request is before  # nothing was replaced


@pytest.mark.design("service.broken_after_listening")
def test_an_ungatable_langchain_after_listening_marks_broken_and_does_not_exit(
    service: state.ProcessState,
) -> None:
    stops = ready(service)
    shim = module(BaseTool=type("Empty", (), {}), ToolException=Exception)
    langchain_gate.patch(shim, service, service.stop)
    assert_structure_and_untouched(service, stops)


@pytest.mark.design("service.broken_after_listening")
def test_an_ungatable_mcp_after_listening_marks_broken_and_does_not_exit(
    service: state.ProcessState,
) -> None:
    stops = ready(service)

    async def renamed(self: Any, request: Any, result_type: Any, *args: Any) -> Any:
        return await self._dispatcher.send_raw_request(request)

    lib = mcp_library(send_request=renamed)
    mcp_gate.patch(lib, mcp_gate.Gate(service, None, service.stop))
    assert_structure_and_untouched(service, stops)
    assert lib.ClientSession.send_request is renamed  # type: ignore[attr-defined]
    session = mcp_session(lib, "a plain reply")
    assert asyncio.run(session.send_request(Request("tools/call"), None)) == "a plain reply"
