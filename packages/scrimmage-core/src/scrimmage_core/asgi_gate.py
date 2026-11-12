# pyright: standard
"""The entry gate of a service: where uvicorn loads the app, and at Starlette's own call.

A request without x-scrimmage-run runs exactly as it would without Scrimmage: the slot is
set to plain, and the app gets the server's own scope, receive and send. A request with it
is a test's run: its MAC is checked here, its plan claimed from the runner once, and the app
called with that run in the slot and every x-scrimmage-* header removed. No request with
the header ever reaches the app without its plan.

The SDK never imports uvicorn or Starlette: the hook patches each when the app imports it.
"""

import asyncio
import contextlib
import functools
import importlib.metadata
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from scrimmage_core import hooks, labels, protocol, state, threadctx
from scrimmage_core.client import (
    Busy,
    ClaimRefused,
    ControlClient,
    ControlError,
    RunSender,
    Senders,
)
from scrimmage_core.executor import Executor
from scrimmage_core.mocks import allowed
from scrimmage_core.plan import Plan
from scrimmage_core.redact import redact
from scrimmage_core.state import SLOT, Joined, Plain, ProcessState, RunState, is_plain

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

RUN = b"x-scrimmage-run"
PROBE = b"x-scrimmage-probe"
PREFIX = b"x-scrimmage-"
HEADER = re.compile(r"((?:run|setup)-[0-9a-f]{12})\.([0-9a-f]{16})")
CLAIM_WAIT_S = 0.25  # how long a claim waits on the loop for a free control slot
CLAIM_STEP_S = 0.01
GATED = "__scrimmage_gate__"
LIBRARIES = ("uvicorn", "starlette", "fastapi", "anyio", "httpx", "httpx2", "mcp")
ACTIVE = (
    "active in service mode (pid {pid}); requests with x-scrimmage-run follow their test's plan"
)


class Unavailable(Exception):
    """A run request is answered 503 with this code, before the app sees it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Gate:
    """What the entry gate needs in this process."""

    process: ProcessState
    client: ControlClient
    token: str
    prefixes: list[str]
    tools: tuple[str, ...]  # the import paths SCRIMMAGE_TOOLS names, as the gates swap them
    renames: tuple[tuple[str, str], ...]
    senders: Senders
    executor: Executor
    proc: dict[str, object] = field(default_factory=state.this_process)
    nonces: protocol.NonceStore = field(default_factory=protocol.NonceStore)
    gates: list[str] = field(default_factory=list[str])  # the entry gates installed, by name


GATE: Gate | None = None  # set by bootstrap.start_service


def active(name: str) -> None:
    """An entry gate is in this process: say so once, so the supervisor says nothing."""
    gate = GATE
    if gate is None or name in gate.gates:
        return
    gate.gates.append(name)
    if len(gate.gates) == 1:
        # This process serves the app, so the team's own children, started from here, never
        # learn where the token is. A supervisor that loads no app keeps it for its workers.
        os.environ.pop("SCRIMMAGE_TOKEN_FILE", None)
        print(f"scrimmage: {ACTIVE.format(pid=os.getpid())}", file=sys.stderr, flush=True)


async def entry(app: App, scope: Scope, receive: Receive, send: Send) -> None:
    gate = GATE
    if gate is None:
        return await app(scope, receive, send)
    listening(gate)
    kind = scope.get("type")
    if kind not in ("http", "websocket"):
        return await app(scope, receive, send)  # lifespan: untouched, the slot as it is
    headers = list(scope.get("headers") or [])
    runs = [value for name, value in headers if bytes(name).lower() == RUN]
    probe = any(bytes(name).lower() == PROBE for name, _ in headers)
    if kind == "websocket" and runs:
        await close_websocket(receive, send)
        return None
    if kind == "websocket" or (not runs and not probe):
        # Every request sets the slot, so a value leaked between tasks is overwritten.
        return await served_plain(app, scope, receive, send)
    copied = dict(scope)
    copied["headers"] = [(n, v) for n, v in headers if not bytes(n).lower().startswith(PREFIX)]
    if probe:
        return await answer_probe(gate, app, scope, copied, receive, send)
    try:
        name = checked_name(gate, runs)
        if gate.process.broken is not None:
            raise Unavailable("structure")
        run, sender = await claimed(gate, name)
    except Unavailable as unavailable:
        return await refuse(send, unavailable.code)
    await joined(gate, app, copied, receive, send, run, sender)
    return None


async def served_plain(app: App, scope: Scope, receive: Receive, send: Send) -> None:
    """A request with no run, under its own plain marker. Its threads and tasks share it, so
    work it waits for, background tasks included, stays plain; once the call returns, work it
    left running reads as outside any request."""
    plain = Plain()
    token = SLOT.set(plain)
    try:
        return await app(scope, receive, send)
    finally:
        plain.done = True
        SLOT.reset(token)


def listening(gate: Gate) -> None:
    """From the first call, of any scope, the process is listening; each loop is watched."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if not gate.process.listening:
        why = threadctx.check_loop(loop)
        if why is not None:
            gate.process.structure("scrimmage", why, gate.process.stop)
        gate.process.listening = True
    threadctx.watch_loop(loop)


def checked_name(gate: Gate, runs: list[bytes]) -> str:
    """The run's name, if its one header carries the MAC only a token holder can make. No
    control request is made for any other header."""
    if len(runs) != 1:
        raise Unavailable("bad-run-header")
    found = HEADER.fullmatch(bytes(runs[0]).decode("latin-1"))
    if found is None:
        raise Unavailable("bad-run-header")
    name, mac = found.groups()
    if not protocol.same(mac, protocol.run_mac(gate.token, name)):
        raise Unavailable("bad-run-header")
    return name


async def claimed(gate: Gate, name: str) -> tuple[RunState, RunSender]:
    """The run's plan, claimed on Scrimmage's own threads with a control slot taken on the
    loop: with none free in about 250 ms the request gets 503, holding no thread."""
    loop = asyncio.get_running_loop()
    began = time.monotonic()
    deadline = loop.time() + CLAIM_WAIT_S
    client = gate.client
    while not client.try_slot(count=False):
        if loop.time() >= deadline:
            client.turned_away()
            raise Unavailable("plan-unavailable")
        await asyncio.sleep(CLAIM_STEP_S)
    work = functools.partial(client.run_held, lambda: client.claim(name, gate.proc))
    try:
        doc = await asyncio.wrap_future(gate.executor.submit(work))
    except ClaimRefused as refused:
        code = {"claimed": "claimed"}.get(refused.code, "no-such-run")
        raise Unavailable(code) from None
    except (ControlError, Busy):
        raise Unavailable("plan-unavailable") from None
    try:
        plan = Plan.from_json(doc)
    except ValueError:
        await abandon(gate, name)
        raise Unavailable("plan-unavailable") from None
    if problem(gate, plan) is not None:
        await abandon(gate, name)
        raise Unavailable("plan-unavailable")
    sender = RunSender(client, name, gate.proc)
    sender.since = began
    run = RunState(plan, sender, gate.prefixes, process=gate.process, blobs=sender)
    run.proc = gate.proc
    sender.on_closed = run.run_closed
    gate.senders.add(sender)
    gate.process.register(run)
    gate.process.claimed(run)
    return run, sender


def problem(gate: Gate, plan: Plan) -> str | None:
    """Why this process cannot follow the plan: an import outside SCRIMMAGE_ALLOW_IMPORTS, or
    tools other than the ones its gates swap."""
    raises = [f.raises for f in plan.faults if f.raises and ":" in f.raises]
    paths = [*plan.tools, *(m.type for m in plan.mocks if m.type), *raises]
    outside = next((path for path in paths if not allowed(path, gate.prefixes)), None)
    if outside is not None:
        return f"{outside} is outside SCRIMMAGE_ALLOW_IMPORTS"
    if tuple(plan.tools) != gate.tools or dict(plan.renames) != dict(gate.renames):
        return "the plan's tools are not the ones this service gates"
    return None


async def abandon(gate: Gate, name: str) -> None:
    """The claimed plan cannot be used: the runner ends the run at once, not at its wait."""
    work = functools.partial(gate.client.abandoned, name)
    with contextlib.suppress(Exception):
        await asyncio.wrap_future(gate.executor.submit(work))


async def joined(
    gate: Gate,
    app: App,
    scope: Scope,
    receive: Receive,
    send: Send,
    run: RunState,
    sender: RunSender,
) -> None:
    """Call the app with the run in the slot. Its record closes record_grace_s after the
    reply's last byte, or after the call ends with no reply, so a late call is its own."""
    scheduled: list[bool] = []

    def close_later() -> None:
        if scheduled:
            return
        scheduled.append(True)
        gate.executor.timers.later(run.plan.record_grace_s, close)

    def close() -> None:
        try:
            threadctx.close_run(run)
            sender.close()
        finally:
            run.run_closed()
            gate.process.unregister(run)
            gate.senders.remove(sender)

    async def watched(message: Message) -> None:
        await send(message)
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            close_later()

    token = SLOT.set(Joined(run.name, run))
    try:
        await app(scope, receive, watched)
    finally:
        run.entry_done = True
        close_later()
        SLOT.reset(token)


async def refuse(send: Send, code: str) -> None:
    """The gate's own 503: a JSON body naming the code, and nothing of the app's."""
    body = json.dumps({"error": code}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"x-scrimmage-error", code.encode()),
    ]
    await send({"type": "http.response.start", "status": 503, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def close_websocket(receive: Receive, send: Send) -> None:
    """One run is one HTTP request in this version: a WebSocket with the header is closed."""
    message = await receive()
    if message.get("type") == "websocket.connect":
        await send({"type": "websocket.close", "code": 1008})


async def read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunks.append(bytes(message.get("body", b"")))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def replaying(body: bytes, receive: Receive) -> Receive:
    """A receive that gives the body already read, then whatever comes after."""
    given: list[bool] = []

    async def again() -> Message:
        if not given:
            given.append(True)
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return again


async def answer_probe(
    gate: Gate, app: App, scope: Scope, copied: Scope, receive: Receive, send: Send
) -> None:
    """The runner's signed probe gets the startup report, signed. Anything else, a stranger's
    or a replay, goes to the app as a plain request with Scrimmage's headers removed."""
    body = await read_body(receive)
    fields = {
        bytes(k).decode("latin-1").title(): bytes(v).decode("latin-1") for k, v in scope["headers"]
    }
    target = bytes(scope.get("raw_path") or scope.get("path", "").encode())
    query = bytes(scope.get("query_string") or b"")
    path = target.decode("latin-1") + (f"?{query.decode('latin-1')}" if query else "")
    method = str(scope.get("method", ""))
    error = protocol.check_request(gate.token, gate.nonces, method, path, fields, body, time.time())
    if error is not None:
        return await served_plain(app, copied, replaying(body, receive), send)
    report = json.dumps(startup_report(gate)).encode()
    signature = protocol.sign_reply(gate.token, fields.get(protocol.SIGNATURE, ""), 200, report)
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(report)).encode()),
        (PROBE, b"report"),
        (protocol.SIGNATURE.lower().encode(), signature.encode()),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": report})
    return None


def startup_report(gate: Gate) -> dict[str, object]:
    """Versions, gate names and tools: no token, no plan, nothing of a run."""
    found: dict[str, str] = {}
    for name in LIBRARIES:
        if name in sys.modules:
            with contextlib.suppress(importlib.metadata.PackageNotFoundError):
                found[name] = importlib.metadata.version(name)
    chained = getattr(sys.modules.get("sitecustomize"), "__file__", "") or ""
    renames = dict(gate.renames)
    return {
        "v": 1,
        "sdk": sdk_version(),
        "python": sys.version,
        "executable": redact(sys.executable),
        "pid": os.getpid(),
        "mode": "service",
        "gates": list(gate.gates),
        "libraries": found,
        "chained_sitecustomize": bool(chained) and "scrimmage_core" not in chained,
        "tools": sorted(labels.tool_name(path, renames) for path in gate.tools),
        "notices": [dict(entry) for entry in gate.process.report],
        "server_time": time.time(),
    }


def sdk_version() -> str:
    try:
        return importlib.metadata.version("scrimmage-core")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class Entry:
    """The app uvicorn loaded, behind the entry gate, outermost, so the header is removed
    before uvicorn's own proxy-header or message-logging middleware sees it."""

    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await entry(self.app, scope, receive, send)


def install(process: ProcessState) -> None:
    hooks.on_import("uvicorn.config", lambda module: patch_uvicorn(module, process))
    hooks.on_import("starlette.applications", lambda module: patch_starlette(module, process))


def patch_uvicorn(module: ModuleType, process: ProcessState) -> None:
    config: Any = getattr(module, "Config", None)
    load = getattr(config, "load", None)
    from scrimmage_core.http_gate import params  # the gate's own structural check

    if load is None or params(load) != ["self"]:
        process.structure(
            "uvicorn",
            "uvicorn's Config has no load(self), so its requests cannot be gated, and the service "
            "is stopped",
            process.stop,
        )
        return
    if getattr(load, GATED, False):
        return

    @functools.wraps(load)
    def gated_load(self: Any) -> None:
        protocol_name = getattr(self, "http", None)
        if protocol_name == "zttp" or getattr(self, "http2", False):
            flag = "--http zttp" if protocol_name == "zttp" else "--http2"
            process.structure(
                "uvicorn",
                f"uvicorn {flag} is not tested with Scrimmage in this version, so the service is "
                "stopped; start it with the default --http",
                process.stop,
            )
            return
        load(self)
        if not hasattr(self, "loaded_app"):
            process.structure(
                "uvicorn",
                "uvicorn's Config has no loaded_app after load, so its requests cannot be gated",
                process.stop,
            )
            return
        self.loaded_app = Entry(self.loaded_app)
        active("asgi")

    setattr(gated_load, GATED, True)
    config.load = gated_load


def patch_starlette(module: ModuleType, process: ProcessState) -> None:
    app: Any = getattr(module, "Starlette", None)
    call = vars(app).get("__call__") if isinstance(app, type) else None
    if call is None:
        process.structure(
            "starlette",
            "starlette has no Starlette.__call__, so its requests cannot be gated, and the "
            "service is stopped",
            process.stop,
        )
        return
    if getattr(call, GATED, False):
        return

    @functools.wraps(call)
    async def gated_call(self: Any, scope: Scope, receive: Receive, send: Send) -> None:
        slot = SLOT.get()
        if isinstance(slot, Joined) or is_plain(slot):  # the uvicorn gate has been, once
            return await call(self, scope, receive, send)
        return await entry(functools.partial(call, self), scope, receive, send)

    setattr(gated_call, GATED, True)
    app.__call__ = gated_call
    active("starlette")
