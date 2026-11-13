"""The SDK's side of the control protocol: signed requests and the record sender.

Record lines go to a queue and a sender thread sends them in batches, at least every
100 ms and at exit, so a gate never waits for the network. A full queue drops a line
and marks the record incomplete, except a refusal or a model call's line, which
waits for room.

A child process has no thread and sends each line as it is made, because it may never
reach its exit steps: multiprocessing ends a forked worker with os._exit.
"""

import base64
import contextlib
import http.client
import json
import queue
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlsplit

from scrimmage_core import protocol, threads
from scrimmage_core.budget import Held, Shortfall
from scrimmage_core.plan import items, number, obj

QUEUE_SIZE = 10_000
BATCH_LINES = 500
BATCH_S = 0.1
TIMEOUT_S = 10.0
BLOB_QUEUE = 64  # side-folder items waiting to be sent; each is at most 10 MB
# Put on the queue at exit to end the sender's wait. Compared by identity, never sent.
WAKE: dict[str, object] = {}
T = TypeVar("T")


class ControlError(Exception):
    """The control server could not be reached, refused us, or sent a bad reply."""


def hint(reply: dict[str, object]) -> float | None:
    """The reply's retry_s, or None. It is optional: a runner from before it sends none, and
    one that sends something unreadable leaves the gate at its own pace, not refused."""
    try:
        return number(reply, "retry_s")
    except ValueError:
        return None


class Busy(ControlError):
    """No control slot was free: the process already has CAP requests in flight."""


class ClaimRefused(ControlError):
    """The runner would not give the run's plan: no_such_run, run_closed or claimed."""

    def __init__(self, code: str) -> None:
        super().__init__(f"the runner refused the claim: {code}")
        self.code = code


CAP = 8  # control requests one process sends at once; 8 processes fill the server's 64
_waits = threading.local()


def never_wait() -> None:
    """This thread runs only work that holds its slot already: it never waits for one."""
    _waits.never = True


ROUND_TRIPS = 1024  # the last round trips kept, for a run's control_p99_ms


class ControlClient:
    def __init__(self, url: str, token: str, context: ssl.SSLContext | None = None) -> None:
        """context: the TLS settings of an https:// control URL, which cannot be used without."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ControlError(f"SCRIMMAGE_CONTROL_URL {url!r} is not http://host:port")
        if (parts.scheme == "https") != (context is not None):
            raise ControlError(
                f"SCRIMMAGE_CONTROL_URL {url!r} needs a TLS context if and only if it is https://"
            )
        self.context = context
        self.host = parts.hostname
        self.port = parts.port or (443 if context is not None else 80)
        self.token = token
        self.offset = 0.0  # the server's clock minus ours, learned from its replies
        # Each request holds a slot while it is in flight. A sync caller waits for one on its
        # own thread; a claim or an async budget ask takes one only if free, on the loop.
        self.slots = threading.BoundedSemaphore(CAP)
        self.trips: deque[tuple[float, float]] = deque(maxlen=ROUND_TRIPS)  # (end, ms)
        self.busy: deque[float] = deque(maxlen=ROUND_TRIPS)  # when a request found no slot
        self._load = threading.Lock()
        self._held = threading.local()  # this thread runs work for a slot taken on a loop

    def try_slot(self, *, count: bool = True) -> bool:
        """Take a slot if one is free; if not, count the request as turned away, unless the
        caller will try again and counts it once itself."""
        if self.slots.acquire(blocking=False):
            return True
        if count:
            self.turned_away()
        return False

    def turned_away(self) -> None:
        """A control request this process refused itself, for no free slot."""
        with self._load:
            self.busy.append(time.monotonic())

    def run_held(self, work: Callable[[], T]) -> T:
        """Run work with the slot this thread's caller took: its first request uses it, and a
        work that sends none gives it back."""
        self._held.on = True
        try:
            return work()
        finally:
            if self._held.on:
                self._held.on = False
                self.slots.release()

    def load_since(self, since: float) -> tuple[int, float | None]:
        """Requests turned away for no free slot, and the 99th percentile round trip in ms,
        from a moment on: what a run's last batch says about the process's control load."""
        with self._load:
            busy = sum(1 for at in self.busy if at >= since)
            times = sorted(ms for end, ms in self.trips if end >= since)
        if not times:
            return busy, None
        return busy, round(times[min(len(times) - 1, int(0.99 * len(times)))], 1)

    def request(
        self, method: str, path: str, payload: object = None, *, held: bool = False
    ) -> tuple[int, dict[str, object]]:
        """Send one signed request; retry once if the server says our clock is off. held:
        the caller took a slot already, and this releases it."""
        if getattr(self._held, "on", False):
            self._held.on, held = False, True
        if not held:
            if getattr(_waits, "never", False):
                raise ControlError(
                    "a Scrimmage claim thread asked for a control slot it did not hold; it "
                    "would wait on the claims queued behind it"
                )
            self.slots.acquire()
        try:
            status, reply = self._timed(method, path, payload)
            if status == 401 and reply.get("error") == "clock":
                status, reply = self._timed(method, path, payload)
            return status, reply
        finally:
            self.slots.release()

    def _timed(self, method: str, path: str, payload: object) -> tuple[int, dict[str, object]]:
        began = time.monotonic()
        try:
            return self._send(method, path, payload)
        finally:
            ended = time.monotonic()
            with self._load:
                self.trips.append((ended, (ended - began) * 1000))

    def claim(self, run: str, proc: object, *, held: bool = False) -> object:
        """A service run's plan, claimed for this process. Raises ClaimRefused or
        ControlError."""
        status, reply = self.request("POST", "/v1/claims", {"run": run, "proc": proc}, held=held)
        if status != 200:
            raise ClaimRefused(str(reply.get("error")))
        return reply.get("plan")

    def abandoned(self, run: str) -> None:
        """The claimed plan cannot be used: the runner ends the run at once."""
        with contextlib.suppress(ControlError):
            self.request("POST", "/v1/abandoned", {"run": run})

    def _send(self, method: str, path: str, payload: object) -> tuple[int, dict[str, object]]:
        body = b"" if payload is None else json.dumps(payload).encode()
        headers = protocol.request_headers(
            self.token, method, path, body, time.time() + self.offset
        )
        if body:
            headers["Content-Type"] = "application/json"
        conn = self._connection()
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            signature = response.getheader(protocol.SIGNATURE) or ""
        except (OSError, http.client.HTTPException) as exc:
            raise ControlError(f"cannot reach the control server: {exc!r}") from exc
        finally:
            conn.close()
        expected = protocol.sign_reply(
            self.token, headers[protocol.SIGNATURE], response.status, data
        )
        if not protocol.same(signature, expected):
            raise ControlError("the control server's reply is not signed for this request")
        try:
            reply = obj(json.loads(data))
            server_time = float(str(reply["server_time"]))
        except (ValueError, KeyError) as exc:
            raise ControlError("the control server's reply is not valid") from exc
        self.offset = server_time - time.time()
        return response.status, reply

    def _connection(self) -> http.client.HTTPConnection:
        """The one place a control connection is made: TLS when the URL is https://."""
        if self.context is None:
            return http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT_S)
        return http.client.HTTPSConnection(
            self.host, self.port, timeout=TIMEOUT_S, context=self.context
        )

    def fetch_plan(self, run: str) -> object:
        status, reply = self.request("GET", f"/v1/plans/{run}")
        if status != 200:
            raise ControlError(f"no plan for {run}: {reply.get('error')}")
        return reply.get("plan")

    def reserve(
        self, run: str, usd: float, label: str = "", ticket: int | None = None
    ) -> int | Shortfall:
        """Reserve what a model call could cost, or say why the budget cannot cover it now.

        The server answers at once. A caller that waits asks again with its ticket.
        """
        payload = {"run": run, "usd": usd, "label": label, "ticket": ticket}
        status, reply = self.request("POST", "/v1/budget/reserve", payload)
        if status != 200:
            raise ControlError(f"the budget could not be checked: {reply.get('error')}")
        try:
            if reply.get("ok") is True:
                return int(str(reply["reservation"]))
            place = reply.get("ticket")
            retry = hint(reply)
            return Shortfall(
                str(reply["why"]),
                number(reply, "usd"),
                number(reply, "spent"),
                number(reply, "held"),
                number(reply, "limit"),
                None if place is None else int(str(place)),
                tuple(
                    Held(str(h["run"]), str(h["label"]), number(h, "usd"), h.get("ended") is True)
                    for h in map(obj, items(reply, "holds"))
                ),
                retry,
            )
        except (ValueError, KeyError) as exc:
            raise ControlError("the control server's budget reply is not valid") from exc

    def release(self, ticket: int) -> None:
        """Stop waiting, so the calls behind this one are served. Best effort: a ticket
        that is not asked for again is dropped after budget.LOST_S, or when its run ends. A
        claim thread, which has used its slot, hands it to a thread that may wait for one."""
        if getattr(_waits, "never", False):
            threads.start(threading.Thread(target=self.release, args=(ticket,), daemon=True))
            return
        with contextlib.suppress(ControlError):
            self.request("POST", "/v1/budget/release", {"ticket": ticket})


class Sender:
    """Sends record lines in the background, in batches.

    A batch that fails to send is kept and sent again with the next one; the record
    store writes a line once however often it arrives. The last batch, sent at exit,
    says how many lines this process made, so the runner can tell a record with a gap
    from a complete one.
    """

    def __init__(
        self, client: ControlClient, run: str, proc: object = None, *, eager: bool = False
    ) -> None:
        self.client = client
        self.run = run
        self.proc = proc  # this process, as its record lines name it
        self.eager = eager  # send each line as it is made, with no thread
        self.on_closed: Callable[[], None] = lambda: None  # the run has ended
        self.since = time.monotonic()  # the control load in the last batch counts from here
        self._fresh()

    def _fresh(self) -> None:
        self.queue: queue.Queue[dict[str, object]] = queue.Queue(QUEUE_SIZE)
        self.made = 0  # lines handed to put, dropped ones included
        self.dropped = 0
        self.unsent: list[dict[str, object]] = []
        # Side-folder items, sent one at a time by this thread, kept and retried on
        # failure. Bounded, because each can be 10 MB.
        self.posts: queue.Queue[tuple[str, dict[str, object]]] = queue.Queue(BLOB_QUEUE)
        self.unsent_posts: list[tuple[str, dict[str, object]]] = []
        # Settlements have their own line, with no limit: each is small, and losing one
        # would leave money reserved for the rest of the test.
        self.settles: deque[dict[str, object]] = deque()
        # The thread draining at exit, which holds _sending over the network; and what a
        # SIGTERM that arrived inside that drain left to do when it ends.
        self.draining: int | None = None
        self.after_drain: Callable[[], None] | None = None
        self._counts = threading.Lock()  # made and dropped; never held over the network
        self._sending = threading.Lock()  # one batch in flight at a time
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scrimmage-sender", daemon=True)

    def start(self) -> None:
        threads.start(self._thread)

    def forked(self, proc: object) -> None:
        """This process is a copy of its parent. The lines waiting are the parent's to send,
        no thread came across, and a lock another thread held would stay locked for good."""
        self.proc = proc
        self.eager = True
        self._fresh()

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        """Queue a line. keep: wait for room instead of dropping (refusals, model calls).

        Never waits for the network: a gate only takes a short lock to count the line.
        """
        with self._counts:
            self.made += 1
        if keep:
            self.queue.put(line)
        else:
            try:
                self.queue.put_nowait(line)
            except queue.Full:
                with self._counts:
                    self.dropped += 1
        self._send_now()

    def _send_now(self) -> None:
        if self.eager:
            self._send_batch(block=False, final=True)

    def put_blob(self, sha: str, data: bytes) -> bool:
        """Queue one side-folder item. False, and the line says so, when there is no room."""
        payload: dict[str, object] = {
            "run": self.run,
            "sha256": sha,
            "data": base64.b64encode(data).decode(),
        }
        try:
            self.posts.put_nowait(("/v1/blobs", payload))
        except queue.Full:
            return False
        return True

    def put_settle(self, reservation: int, usd: float) -> None:
        """Queue the real cost of a call. Never dropped."""
        self.settles.append({"run": self.run, "reservation": reservation, "usd": usd})
        self._send_now()

    def flush(self) -> None:
        """Send everything queued now, then the closing batch. Called at exit.

        The sender thread may hold a batch it has taken from the queue but not sent, so
        wait for it to finish that batch before sending the rest.

        A child has sent all it made, so it sends again only what failed the first time.
        A child that made no lines sends nothing.
        """
        if self.eager:
            if self.unsent or self.unsent_posts or self.settles:
                self._send_batch(block=False, final=True)
            return
        self._stop.set()  # before the wake, or the thread could open a new window
        if self._thread.is_alive():
            with contextlib.suppress(queue.Full):  # a full queue means get is not waiting
                self.queue.put_nowait(WAKE)
            self._thread.join(timeout=2 * TIMEOUT_S)
        self.draining = threading.get_ident()
        try:
            while self._send_batch(block=False):
                pass
            self._send_batch(block=False, final=True)
        finally:
            self.draining = None
            after, self.after_drain = self.after_drain, None
            if after is not None:
                after()

    def defer_to_drain(self, then: Callable[[], None]) -> bool:
        """Called by a signal handler: if this thread is inside the exit drain, run then when
        the drain ends and return True. The drain holds the send lock, and the handler runs on
        this very thread, so anything it waited for that needs the lock would wait for nothing.
        A handler runs between two steps of the drain, so no lock is needed here.

        It holds one drain at a time. A second thread flushing at once replaces draining, and
        a signal then takes the old path, which is no worse than before this was added."""
        if self.draining != threading.get_ident():
            return False
        self.after_drain = then
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._send_batch(block=True)

    def _send_batch(self, *, block: bool, final: bool = False) -> bool:
        """Send one batch. False when there was nothing to send or it failed."""
        lines: list[dict[str, object]] = []
        deadline = time.monotonic() + BATCH_S
        while len(lines) < BATCH_LINES:
            wait = deadline - time.monotonic()
            try:
                if block and wait > 0:
                    line = self.queue.get(timeout=wait)
                else:
                    line = self.queue.get_nowait()
            except queue.Empty:
                break
            if line is WAKE:  # send what is here now; lines behind it still go
                block = False
                continue
            lines.append(line)
        if not self.eager:
            with self._sending:
                self._send_posts()
                return self._post(lines, final=final)
        # No lock over the network here: each send has its own connection and the store
        # writes a line once. A main thread stopped inside a send must not hold up the line
        # of a call stopped on another thread.
        if self._sending.acquire(blocking=False):
            try:
                self._send_posts()
            finally:
                self._sending.release()
        return self._post(lines, final=final)

    def _post(self, lines: list[dict[str, object]], *, final: bool) -> bool:
        with self._counts:
            lines = self.unsent + lines
            self.unsent = []
            if len(lines) > QUEUE_SIZE:  # the server has been away a long time
                self.dropped += len(lines) - QUEUE_SIZE
                lines = lines[:QUEUE_SIZE]
            dropped, self.dropped = self.dropped, 0
            made = self.made
        if not lines and not dropped and not final:
            return False
        batch: dict[str, object] = {"run": self.run, "lines": lines, "dropped": dropped}
        if final:
            batch["made"] = made
            batch["proc"] = self.proc
            # The process's control load from this run's start or claim on. It describes the
            # process: two runs that overlap in one worker count the same requests.
            busy, p99 = self.client.load_since(self.since)
            batch.update(control_busy=busy, control_p99_ms=p99)
        try:
            status, _ = self.client.request("POST", "/v1/records", batch)
        except ControlError:
            status = 0
        if status == 200:
            return True
        if status == 409:
            self.on_closed()
        with self._counts:
            self.unsent = lines + self.unsent
            self.dropped += dropped
        return False

    def _send_posts(self) -> None:
        while self.settles:
            try:
                status, _ = self.client.request("POST", "/v1/budget/settle", self.settles[0])
            except ControlError:
                break
            if status != 200:
                break
            self.settles.popleft()
        pending = self.unsent_posts
        while True:
            try:
                pending.append(self.posts.get_nowait())
            except queue.Empty:
                break
        self.unsent_posts = []
        for path, payload in pending:
            try:
                status, _ = self.client.request("POST", path, payload)
            except ControlError:
                status = 0
            if status != 200:
                self.unsent_posts.append((path, payload))


class RunSender(Sender):
    """One service run's lines: its own queue, counts and close, with no thread of its own.
    The process's one Senders thread sends a batch for each run that has lines."""

    def start(self) -> None:
        """The process's Senders thread sends this run's batches."""

    def send_some(self) -> None:
        """One batch, if there are lines: called by the Senders thread every BATCH_S."""
        self._send_batch(block=False)

    def close(self) -> None:
        """The run's record closes: send what is queued, then the last batch, with made."""
        while self._send_batch(block=False):
            pass
        self._send_batch(block=False, final=True)


class Senders:
    """The one sender thread of a service process, over every open run's RunSender. A
    thread per run would be 64 threads polling every 100 ms at 64 runs."""

    def __init__(self) -> None:
        self.runs: dict[int, RunSender] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="scrimmage-sender", daemon=True)
        self._wake = threading.Event()

    def start(self) -> None:
        threads.start(self._thread)

    def stop(self) -> None:
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(2 * TIMEOUT_S)

    def add(self, sender: RunSender) -> None:
        with self._lock:
            self.runs[id(sender)] = sender

    def remove(self, sender: RunSender) -> None:
        with self._lock:
            self.runs.pop(id(sender), None)

    def _loop(self) -> None:
        while not self._wake.wait(BATCH_S):
            with self._lock:
                senders = list(self.runs.values())
            for sender in senders:
                sender.send_some()
