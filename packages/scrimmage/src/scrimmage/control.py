"""The control server: the runner's side of the SDK's requests.

It serves each run's plan while the run is open and stores its record lines. It listens on
the loopback address unless a service test gives it another, which then needs TLS. Every
request must be signed, and every reply is.
"""

import base64
import dataclasses
import hashlib
import io
import json
import math
import re
import secrets
import socket as socket_module
import ssl
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from typing import Any, cast

from scrimmage import protocol, versions
from scrimmage.budget import Ledger, Shortfall
from scrimmage.plan import items, number, obj
from scrimmage.records import RecordStore

MAX_CONNECTIONS = 64
MAX_BODY = protocol.MAX_BODY  # a batch of 500 lines is far smaller
MAX_BLOB_BODY = 14 * 1024 * 1024  # one 10 MB side-folder item, base64 encoded
BLOB_PATH = "/v1/blobs"
SHA256 = re.compile(r"[0-9a-f]{64}")
READ_BUFFER = 8192  # bytes the handler reads ahead; the same on every Python
READ_TIMEOUT_S = 10.0  # a connection that sends nothing frees its slot
REQUEST_DEADLINE_S = 15.0  # from the connection being served to the last byte of its reply
PLAN_PATH = re.compile(r"/v1/plans/([A-Za-z0-9_-]{1,64})")
TLS_ALERT = re.compile(r"^(tlsv1|sslv3) alert ")


@dataclass
class Run:
    plan: dict[str, object]
    row: str
    open: bool = True
    fetched: bool = False
    refused: bool = False
    broken: bool = False  # a library its gate cannot work with
    dropped: int = 0
    # Lines each process said it made, from its last batch, keyed like proc_key.
    made: dict[str, int] = field(default_factory=dict[str, int])
    refusals: list[str] = field(default_factory=list[str])  # the reason of each refusal
    setup_refused: bool = False  # the first refusal was the test's setup, not the agent
    # When the run opened, just before its program started: the runner's run_timeout counts
    # from about here. Monotonic, so it is read only as a difference on this process's clock.
    opened: float = field(default_factory=time.monotonic)
    # A service run is one request: its plan is claimed once, by the process that answers
    # it, and only that process's lines are taken from then on.
    service: bool = False
    claimed_by: str | None = None
    claimer: dict[str, object] | None = None  # {host, pid} of the claiming process
    # What the claimer's last batch said of the plan its state used; None when it said none.
    claimed_plan_digest: str | None = None
    # The claiming process's control load from the run's claim to its close: its last batch.
    control_busy: int | None = None
    control_p99_ms: float | None = None
    ended: threading.Event = field(default_factory=threading.Event)  # the claimer's last batch


class Stats:
    """What the server counts for the report: connections that failed TLS, by OpenSSL's reason,
    connections the timeouts cut, and the claims it answered. A control server that no SDK can
    reach shows here when the runs' own load figures, which arrive in a claimed run's last
    batch, do not."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tls_failed: Counter[str] = Counter()
        self._cut = 0
        self._claims = 0

    def failed_tls(self, error: ssl.SSLError) -> None:
        reason = TLS_ALERT.sub("", str(error.reason or "unknown").lower().replace("_", " "))
        with self._lock:
            self._tls_failed[reason] += 1

    def cut(self) -> None:
        with self._lock:
            self._cut += 1

    def claimed(self) -> None:
        with self._lock:
            self._claims += 1

    @property
    def tls_failed(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._tls_failed.items()))

    @property
    def connections_cut(self) -> int:
        with self._lock:
            return self._cut

    @property
    def claims_received(self) -> int:
        with self._lock:
            return self._claims


class ControlServer:
    def __init__(
        self,
        token: str,
        store: RecordStore,
        on_refused: Callable[[str, str], None] = lambda run, reason: None,
        clock: Callable[[], float] = time.time,
        ledger: Ledger | None = None,
        address: tuple[str, int] = ("127.0.0.1", 0),
        tls: ssl.SSLContext | None = None,
    ) -> None:
        """address: where to listen; a service test binds the one its service was told.
        tls: each accepted connection is wrapped in it, and the url says https."""
        self.token = token
        self.store = store
        self.on_refused = on_refused
        self.ledger = ledger or Ledger(None)
        self.clock = clock
        self.nonces = protocol.NonceStore()
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()
        self.tls = tls is not None
        self.stats = Stats()
        # Who this server is, for a ping to prove: random for each runner and kept in memory. It
        # appears in the startup report and result.json, which is fine: it opens nothing.
        self.runner_id = secrets.token_hex(8)
        # The SDK version of the first claim refused for its version, which the runner names.
        self.refused_sdk: list[str | None] = []
        self.server = _Server(address, _handler(self), tls, self.stats)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        shown = f"[{host}]" if ":" in str(host) else str(host)
        return f"{'https' if self.tls else 'http'}://{shown}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def open_run(
        self, run: str, row: str, plan: dict[str, object], *, service: bool = False
    ) -> None:
        json.dumps(plan)  # a plan that cannot be sent fails here, not in a handler
        self.store.open_run(run)
        with self.lock:
            self.runs[run] = Run(plan, row, service=service)

    def close_run(self, run: str) -> Run:
        """No line or plan request is accepted for the run after this."""
        with self.lock:
            state = self.runs[run]
            state.open = False
            self.ledger.close_run(run)
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
        if method == "POST" and path == "/v1/budget/release":
            return self._release(body)
        if method == "POST" and path == BLOB_PATH:
            return self._blob(body)
        if method == "POST" and path == "/v1/claims":
            return self._claim(body)
        if method == "POST" and path == "/v1/ping":
            return 200, {"ok": True, "runner_id": self.runner_id}  # stores nothing
        if method == "POST" and path == "/v1/abandoned":
            return self._abandoned(body)
        return 404, {"error": "not_found"}

    def _claim(self, body: bytes) -> tuple[int, dict[str, object]]:
        """A service run's plan, to the first process that asks. A second claim, as from a
        replayed header, is refused: a run is joined once."""
        try:
            request = obj(json.loads(body))
            run, proc = str(request["run"]), proc_key(request["proc"])
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        sdk = request.get("sdk")  # a claim that does not say its version is refused too
        if not versions.same(sdk):
            with self.lock:
                if not self.refused_sdk:  # the first is the one the runner names
                    self.refused_sdk.append(sdk if isinstance(sdk, str) else None)
            return 409, {"error": "version"}
        with self.lock:
            state = self.runs.get(run)
            if state is None or not state.open:
                return 404, {"error": "no_such_run"}
            if state.claimed_by is not None:
                return 409, {"error": "claimed"}
            state.claimed_by, state.fetched = proc, True
            state.claimer = claimer_of(request["proc"])
            self.stats.claimed()
            elapsed = round(max(0.0, time.monotonic() - state.opened), 3)
            return 200, {"plan": {**state.plan, "elapsed_s": elapsed}}

    def _abandoned(self, body: bytes) -> tuple[int, dict[str, object]]:
        """The claimer could not use the plan: the run is broken, and ends at once."""
        try:
            run = str(obj(json.loads(body))["run"])
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        with self.lock:
            state = self.runs.get(run)
            if state is None or not state.open:
                return 409, {"error": "run_closed"}
            first = not state.broken
            state.broken = True
        if first:
            threading.Thread(target=self.on_refused, args=(run, "structure"), daemon=True).start()
        return 200, {"ok": True}

    def _open(self, run: str) -> Run | None:
        with self.lock:
            state = self.runs.get(run)
            return state if state is not None and state.open else None

    def _reserve(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            request = obj(json.loads(body))
            run, usd = str(request["run"]), number(request, "usd")
            label = str(request.get("label", ""))
            ticket = None if request.get("ticket") is None else int(str(request["ticket"]))
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        if not usd >= 0:  # NaN too
            return 400, {"error": "bad_request"}
        settles = self._settles(request)
        if settles is None:
            return 400, {"error": "bad_request"}
        # The run's finished calls first: money spent is spent, so they count even if the run
        # has closed, and the reservation below is checked against what they really cost.
        for reservation, spent in settles:
            self.ledger.settle(reservation, spent)
        # Answered at once, never held: a waiter asks again, and one that has gone stops
        # asking. Under the run lock, so a run that has closed is granted nothing.
        with self.lock:
            state = self.runs.get(run)
            if state is None or not state.open:
                return 409, {"error": "run_closed"}
            reservation = self.ledger.reserve(usd, run, label, ticket)
        if isinstance(reservation, Shortfall):
            return 200, {"ok": False, **dataclasses.asdict(reservation)}
        return 200, {"ok": True, "reservation": reservation}

    @staticmethod
    def _settles(request: dict[str, object]) -> list[tuple[int, float]] | None:
        """The settles a reserve carries, or None if one is not valid, so none is applied. A
        settle is not valid if its cost is not a finite number of at least 0. One whose hold
        is gone is valid: it was settled already, and the ledger ignores it."""
        carried = request.get("settles")
        if carried is None:
            return []
        if not isinstance(carried, list):
            return None
        settles: list[tuple[int, float]] = []
        try:
            for item in cast(list[object], carried):
                line = obj(item)
                spent = number(line, "usd")
                if not (math.isfinite(spent) and spent >= 0):
                    return None
                # No check that the hold belongs to this run: reservation numbers are unique
                # and only a holder of the token calls this, who could as well send the same
                # settle to /v1/budget/settle, which does not check either.
                settles.append((int(str(line["reservation"])), spent))
        except (ValueError, KeyError):
            return None
        return settles

    def _release(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            ticket = int(str(obj(json.loads(body))["ticket"]))
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        self.ledger.release(ticket)
        return 200, {"ok": True}

    def _settle(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            request = obj(json.loads(body))
            usd = number(request, "usd")
            reservation = int(str(request["reservation"]))
        except (ValueError, KeyError):
            return 400, {"error": "bad_request"}
        if not (math.isfinite(usd) and usd >= 0):
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
            if state.service:  # a service run's plan is claimed, once, never fetched
                return 409, {"error": "service_run"}
            state.fetched = True
            # The run's age, not a time on this clock, which means nothing in the agent's.
            elapsed = round(max(0.0, time.monotonic() - state.opened), 3)
            return 200, {"plan": {**state.plan, "elapsed_s": elapsed}}

    def _records(self, body: bytes) -> tuple[int, dict[str, object]]:
        try:
            batch = obj(json.loads(body))
            run = str(batch["run"])
            lines = [obj(line) for line in items(batch, "lines")]
            dropped = max(0, int(str(batch.get("dropped", 0))))
            made = None if batch.get("made") is None else int(str(batch["made"]))
            if made is not None and made < 0:
                raise ValueError("made is negative")
            busy, p99 = control_load(batch)
            digest = plan_digest_of(batch)
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "bad_batch"}
        mine = [line for line in lines if line.get("run") == run]
        with self.lock:  # one lock, so no line lands after the run's summary
            state = self.runs.get(run)
            if state is None or not state.open:
                return 409, {"error": "run_closed"}
            claimer = state.claimed_by
            senders = {proc_key(line.get("proc")) for line in mine}
            if made is not None:
                senders.add(proc_key(batch.get("proc")))
            if claimer is not None and senders - {claimer}:
                return 409, {"error": "not_the_claimer"}  # no run spans processes in a service
            if made is not None and claimer is not None and digest is None:
                return 400, {"error": "bad_batch"}  # a claimed run's last batch names its plan
            state.dropped += dropped
            if made is not None:
                state.made[proc_key(batch.get("proc"))] = made
                state.control_busy, state.control_p99_ms = busy, p99
                if claimer is not None:
                    state.claimed_plan_digest = digest
                    state.ended.set()
            accepted = self.store.add(run, mine)
            reasons = [
                str(x.get("reason", "unlisted")) for x in mine if x.get("action") == "refused"
            ]
            first = not state.refused and bool(reasons)
            if first:
                lead = next(x for x in mine if x.get("action") == "refused")
                state.setup_refused = lead.get("by_config") is True
            state.refused = state.refused or first
            state.refusals += reasons
            broken = not state.broken and any(x.get("notice") == "structure" for x in mine)
            state.broken = state.broken or broken
        # Stop the program off this thread, so the reply is not held up.
        if first:
            threading.Thread(target=self.on_refused, args=(run, reasons[0]), daemon=True).start()
        if broken:
            threading.Thread(target=self.on_refused, args=(run, "structure"), daemon=True).start()
        return 200, {"accepted": accepted}


def control_load(batch: dict[str, object]) -> tuple[int | None, float | None]:
    """A last batch's control_busy and control_p99_ms, which only a newer SDK sends."""
    busy = batch.get("control_busy")
    p99 = batch.get("control_p99_ms")
    if busy is not None and (isinstance(busy, bool) or not isinstance(busy, int) or busy < 0):
        raise ValueError("control_busy is not a count")
    if p99 is not None and (isinstance(p99, bool) or not isinstance(p99, int | float) or p99 < 0):
        raise ValueError("control_p99_ms is not a time")
    return busy, None if p99 is None else float(p99)


def plan_digest_of(batch: dict[str, object]) -> str | None:
    """A last batch's plan_digest: 64 hex, or absent."""
    digest = batch.get("plan_digest")
    if digest is not None and not (isinstance(digest, str) and SHA256.fullmatch(digest)):
        raise ValueError("plan_digest is not a digest")
    return digest


def claimer_of(proc: object) -> dict[str, object]:
    """The host and pid a record names for the process that claimed a run."""
    doc = cast(dict[str, object], proc) if isinstance(proc, dict) else {}
    host, pid = doc.get("host"), doc.get("pid")
    return {
        "host": host if isinstance(host, str) else None,
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
    }


def proc_key(proc: object) -> str:
    """One process, as record lines name it: host, pid and start time."""
    return json.dumps(proc, sort_keys=True)


class _Server(ThreadingHTTPServer):
    """One thread per connection, at most 64 at once."""

    daemon_threads = True
    # Connections wait here while the 64 threads are busy; the default of 5 resets the rest.
    request_queue_size = 1024

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        tls: ssl.SSLContext | None,
        stats: Stats,
    ) -> None:
        self.tls = tls
        self.stats = stats
        if ":" in address[0]:
            self.address_family = socket_module.AF_INET6
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def get_request(self) -> tuple[socket, Any]:
        """Accept a connection. With TLS it is wrapped here and not handshaken: the accept loop
        never waits on a client, and the handshake runs in the connection's own thread, on its
        first read, under that connection's deadline."""
        sock, address = super().get_request()
        if self.tls is None:
            return sock, address
        try:
            wrapped = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        except BaseException:
            sock.close()
            raise
        return wrapped, address

    def process_request(self, request: socket | tuple[bytes, socket], client_address: Any) -> None:
        self.slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A program killed mid-request is routine, and so is a client that fails TLS: neither
        prints a traceback on the runner's output. A TLS failure is counted by its reason."""
        error = sys.exc_info()[1]
        if isinstance(error, ssl.SSLError):
            self.stats.failed_tls(error)
        elif not isinstance(error, ConnectionError):
            super().handle_error(request, client_address)

    def process_request_thread(
        self, request: socket | tuple[bytes, socket], client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _Reader(io.RawIOBase):
    """The connection's reads, each under the lower of the read timeout and what is left of
    the whole request's deadline: a client that sends a byte at a time is cut at the deadline,
    and so is one that never finishes a handshake. A cut is counted."""

    def __init__(self, connection: socket, deadline: float, stats: Stats) -> None:
        self.connection = connection
        self.deadline = deadline
        self.stats = stats

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        left = self.deadline - time.monotonic()
        try:
            if left <= 0:
                raise TimeoutError("the request passed its deadline")
            self.connection.settimeout(min(READ_TIMEOUT_S, left))
            return self.connection.recv_into(buffer)
        except TimeoutError:
            self.stats.cut()
            raise


def _handler(control: ControlServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = READ_TIMEOUT_S
        deadline = 0.0

        def setup(self) -> None:
            super().setup()
            self.deadline = time.monotonic() + REQUEST_DEADLINE_S
            self.rfile.close()
            # An explicit buffer: the default grew from 8 KiB to 128 KiB in Python 3.14, and reading
            # the headers would then pull that much of the body in before the signature is checked.
            self.rfile = io.BufferedReader(
                _Reader(self.connection, self.deadline, control.stats), READ_BUFFER
            )

        def _serve(self, method: str) -> None:
            text = self.headers.get("Content-Length") or "0"
            if not text.isdigit():
                self.send_error(400)
                return
            length = int(text)
            if length > (MAX_BLOB_BODY if self.path == BLOB_PATH else MAX_BODY):
                self.send_error(413)
                return
            headers = {k.title(): v for k, v in self.headers.items()}
            status: int
            payload: dict[str, object]
            early = (
                protocol.signed_head(control.token, method, self.path, headers, control.clock())
                if length > MAX_BODY
                else None
            )
            if early is not None:
                # A large body is read only for a request that is signed and fresh already: an
                # unsigned client, or one that replays an old header, can make the server read
                # no more than the small bodies below.
                status, payload = 401, {"error": early}
            else:
                body = self.rfile.read(length) if length else b""
                status, payload = control.handle(method, self.path, headers, body)
            reply = json.dumps(payload).encode()
            signature = protocol.sign_reply(
                control.token, headers.get(protocol.SIGNATURE, ""), status, reply
            )
            # The reply is written within what is left of the deadline, as the reads were.
            self.connection.settimeout(max(0.001, self.deadline - time.monotonic()))
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
