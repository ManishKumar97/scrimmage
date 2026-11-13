"""A service process in the test process: the entry gate, a real control server, and an
ASGI driver written by hand, so no HTTP library stands between the test and the gate."""

import asyncio
import secrets
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scrimmage_core import asgi_gate, executor, protocol, state, threadctx
from scrimmage_core.client import ControlClient, Senders
from scrimmage_core.config import Mock
from scrimmage_core.control import ControlServer
from scrimmage_core.plan import Plan
from scrimmage_core.records import RecordStore

TOOLS = ("acme.tools:get_order",)


def eventually(check: Callable[[], object], within: float = 10.0) -> None:
    """Wait for something another thread does, which sends no event of its own."""
    done = threading.Event()
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("it did not happen in time")
        done.wait(0.01)


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes
    messages: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


async def drive(
    app: Any,
    method: str = "POST",
    path: str = "/chat",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"{}",
    scope_type: str = "http",
    scope: dict[str, Any] | None = None,
) -> Reply:
    """One request through an ASGI app, as a server sends it."""
    scope = scope or {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8765),
    }
    sent: list[dict[str, Any]] = []
    given = [False]

    async def receive() -> dict[str, Any]:
        if scope_type == "websocket":
            return {"type": "websocket.connect"}
        if not given[0]:
            given[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(dict(message))

    await app(scope, receive, send)
    empty: dict[str, Any] = {"status": 0, "headers": []}
    start = next((m for m in sent if m["type"] == "http.response.start"), empty)
    found = {bytes(k).decode(): bytes(v).decode() for k, v in start.get("headers", [])}
    data = b"".join(bytes(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
    return Reply(int(start["status"]), found, data, sent)


class Service:
    """The runner's control server, and one service process's gate, here."""

    def __init__(
        self,
        folder: Path,
        *,
        tools: tuple[str, ...] = TOOLS,
        server_tls: ssl.SSLContext | None = None,
        client_tls: ssl.SSLContext | None = None,
    ) -> None:
        """server_tls, client_tls: TLS on the control channel, on the runner's side and the
        service's."""
        self.token = secrets.token_hex(32)
        self.store = RecordStore(folder)
        self.refused: list[tuple[str, str]] = []
        self.server = ControlServer(
            self.token, self.store, lambda r, why: self.refused.append((r, why)), tls=server_tls
        )
        self.server.start()
        self.stopped: list[str] = []
        self.process = state.ProcessState(("get_order",), service=True)
        self.process.stop = self.stopped.append
        self.client = ControlClient(self.server.url, self.token, client_tls)
        self.senders = Senders()
        self.senders.start()
        self.pool = executor.Executor()
        executor.EXECUTOR = self.pool
        self.gate = asgi_gate.Gate(
            self.process, self.client, self.token, ["acme."], tools, (), self.senders, self.pool
        )
        asgi_gate.GATE = self.gate
        state.install_process(self.process)
        threadctx.install(self.process)

    def close(self) -> None:
        asgi_gate.GATE = None
        executor.EXECUTOR = None
        state.install_process(None)
        self.senders.stop()
        self.server.stop()

    def open(
        self,
        name: str | None = None,
        *,
        mocks: tuple[Mock, ...] = (),
        real: tuple[str, ...] = (),
        grace: float = 0.0,
        tools: tuple[str, ...] = TOOLS,
    ) -> str:
        name = name or f"run-{secrets.token_hex(6)}"
        plan = Plan(
            name, "t", "baseline", mocks, (), tools, real, 60.0, service=True, record_grace_s=grace
        )
        self.server.open_run(name, "baseline", plan.to_json(), service=True)
        return name

    def header(self, name: str, token: str | None = None) -> tuple[bytes, bytes]:
        mac = protocol.run_mac(token or self.token, name)
        return (b"x-scrimmage-run", f"{name}.{mac}".encode())

    def closed(self, name: str) -> list[dict[str, Any]]:
        """The run's lines once its last batch has come, as the runner would read them."""
        eventually(lambda: self.server.runs[name].made)
        return self.store.lines(name)


def service(folder: Path) -> Iterator[Service]:
    made = Service(folder)
    try:
        yield made
    finally:
        made.close()
