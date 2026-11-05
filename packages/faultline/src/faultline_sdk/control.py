"""The control server: the runner's side of the SDK's requests.

It serves each run's plan while the run is open and stores its record lines. It
listens on the local machine only. Every request must be signed, and every reply is.
"""

import base64
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from typing import Any

from faultline_sdk import protocol
from faultline_sdk.budget import Ledger
from faultline_sdk.plan import items, number, obj
from faultline_sdk.records import RecordStore

MAX_CONNECTIONS = 64
MAX_BODY = 4 * 1024 * 1024  # a batch of 500 lines is far smaller
MAX_BLOB_BODY = 14 * 1024 * 1024  # one 10 MB side-folder item, base64 encoded
BLOB_PATH = "/v1/blobs"
SHA256 = re.compile(r"[0-9a-f]{64}")
READ_TIMEOUT_S = 10.0  # a connection that sends nothing frees its slot
PLAN_PATH = re.compile(r"/v1/plans/([A-Za-z0-9_-]{1,64})")


@dataclass
class Run:
    plan: dict[str, object]
    row: str
    open: bool = True
    fetched: bool = False
    refused: bool = False
    dropped: int = 0
    # Lines each process said it made, from its last batch, keyed like proc_key.
    made: dict[str, int] = field(default_factory=dict[str, int])
    refusals: list[str] = field(default_factory=list[str])  # the reason of each refusal


class ControlServer:
    def __init__(
        self,
        token: str,
        store: RecordStore,
        on_refused: Callable[[str, str], None] = lambda run, reason: None,
        clock: Callable[[], float] = time.time,
        ledger: Ledger | None = None,
    ) -> None:
        self.token = token
        self.store = store
        self.on_refused = on_refused
        self.ledger = ledger or Ledger(None)
        self.clock = clock
        self.nonces = protocol.NonceStore()
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()
        self.server = _Server(("127.0.0.1", 0), _handler(self))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def open_run(self, run: str, row: str, plan: dict[str, object]) -> None:
        json.dumps(plan)  # a plan that cannot be sent fails here, not in a handler
        self.store.open_run(run)
        with self.lock:
            self.runs[run] = Run(plan, row)

    def close_run(self, run: str) -> Run:
        """No line or plan request is accepted for the run after this."""
        with self.lock:
            state = self.runs[run]
            state.open = False
            return state

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, object]]:
        error = protocol.check_request(
            self.token, self.nonces, method, path, headers, body, self.clock()
        )
        if error is not None:
            return 401, {"error": error}
        plan_path = PLAN_PATH.fullmatch(path)
        if method == "GET" and plan_path:
            return self._plan(plan_path.group(1))
        if method == "POST" and path == "/v1/records":
            return self._records(body)
        if method == "POST" and path == "/v1/budget/reserve":
            return self._reserve(body)
        if method == "POST" and path == "/v1/budget/settle":
            return self._settle(body)
        if method == "POST" and path == BLOB_PATH:
            return self._blob(body)
        return 404, {"error": "not_found"}

    def _open(self, run: str) -> Run | None:
        with self.lock:
            state = self.runs.get(run)
            return state if state is not None and state.open else None

    def _reserve(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            request = obj(json.loads(body))
            run, usd = str(request["run"]), number(request, "usd")
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        if usd < 0 or self._open(run) is None:
            return 409 if usd >= 0 else 400, {"error": "run_closed" if usd >= 0 else "bad_request"}
        reservation = self.ledger.reserve(usd)
        if reservation is None:
            return 200, {"ok": False}
        return 200, {"ok": True, "reservation": reservation}

    def _settle(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            request = obj(json.loads(body))
            usd = number(request, "usd")
            reservation = int(str(request["reservation"]))
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        if usd < 0:
            return 400, {"error": "bad_request"}
        # Money spent is spent: a settle is kept even if its run has just closed.
        self.ledger.settle(reservation, usd)
        return 200, {"ok": True}

    def _blob(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            request = obj(json.loads(body))
            run, sha = str(request["run"]), str(request["sha256"])
            data = base64.b64decode(str(request["data"]), validate=True)
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        if not SHA256.fullmatch(sha) or hashlib.sha256(data).hexdigest() != sha:
            return 400, {"error": "bad_hash"}
        if self._open(run) is None:
            return 409, {"error": "run_closed"}
        self.store.add_blob(sha, data)
        return 200, {"ok": True}

    def _plan(self, run: str) -> tuple[int, dict[str, object]]:
        with self.lock:
            state = self.runs.get(run)
            if state is None or not state.open:
                return 404, {"error": "no_such_run"}
            state.fetched = True
            return 200, {"plan": state.plan}

    def _records(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            batch = obj(json.loads(body))
            run = str(batch["run"])
            lines = [obj(line) for line in items(batch, "lines")]
            dropped = max(0, int(str(batch.get("dropped", 0))))
            made = None if batch.get("made") is None else int(str(batch["made"]))
            if made is not None and made < 0:
                raise ValueError("made is negative")
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "bad_batch"}
        mine = [line for line in lines if line.get("run") == run]
        with self.lock:  # one lock, so no line lands after the run's summary
            state = self.runs.get(run)
            if state is None or not state.open:
                return 409, {"error": "run_closed"}
            state.dropped += dropped
            if made is not None:
                state.made[proc_key(batch.get("proc"))] = made
            accepted = self.store.add(run, mine)
            reasons = [
                str(x.get("reason", "unlisted")) for x in mine if x.get("action") == "refused"
            ]
            first = not state.refused and bool(reasons)
            state.refused = state.refused or first
            state.refusals += reasons
        if first:  # stop the program off this thread, so the reply is not held up
            threading.Thread(target=self.on_refused, args=(run, reasons[0]), daemon=True).start()
        return 200, {"accepted": accepted}


def proc_key(proc: object) -> str:
    """One process, as record lines name it: host, pid and start time."""
    return json.dumps(proc, sort_keys=True)


class _Server(ThreadingHTTPServer):
    """One thread per connection, at most 64 at once."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: socket | tuple[bytes, socket], client_address: Any) -> None:
        self.slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(
        self, request: socket | tuple[bytes, socket], client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def _handler(control: ControlServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = READ_TIMEOUT_S

        def _serve(self, method: str) -> None:
            text = self.headers.get("Content-Length") or "0"
            if not text.isdigit():
                self.send_error(400)
                return
            length = int(text)
            if length > (MAX_BLOB_BODY if self.path == BLOB_PATH else MAX_BODY):
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else b""
            headers = {k.title(): v for k, v in self.headers.items()}
            status, payload = control.handle(method, self.path, headers, body)
            payload["server_time"] = control.clock()
            reply = json.dumps(payload).encode()
            signature = protocol.sign_reply(
                control.token, headers.get(protocol.SIGNATURE, ""), status, reply
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.send_header(protocol.SIGNATURE, signature)
            self.end_headers()
            self.wfile.write(reply)

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

        def log_message(self, format: str, *args: object) -> None:
            del format, args  # the runner prints its own table

    return Handler
