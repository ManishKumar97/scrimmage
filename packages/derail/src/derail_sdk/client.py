"""The SDK's side of the control protocol: signed requests and the record sender.

Record lines go to a queue and a sender thread sends them in batches, at least every
100 ms and at exit, so a gate never waits for the network. A full queue drops a line
and marks the record incomplete, except a refusal or a model call's line, which
waits for room.

A child process has no thread and sends each line as it is made, because it may never
reach its exit steps: multiprocessing ends a forked worker with os._exit.
"""

import base64
import http.client
import json
import queue
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from derail_sdk import protocol
from derail_sdk.plan import obj

QUEUE_SIZE = 10_000
BATCH_LINES = 500
BATCH_S = 0.1
TIMEOUT_S = 10.0
BLOB_QUEUE = 64  # side-folder items waiting to be sent; each is at most 10 MB


class ControlError(Exception):
    """The control server could not be reached, refused us, or sent a bad reply."""


class ControlClient:
    def __init__(self, url: str, token: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ControlError(f"DERAIL_CONTROL_URL {url!r} is not http://host:port")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.token = token
        self.offset = 0.0  # the server's clock minus ours, learned from its replies

    def request(
        self, method: str, path: str, payload: object = None
    ) -> tuple[int, dict[str, object]]:
        """Send one signed request; retry once if the server says our clock is off."""
        status, reply = self._send(method, path, payload)
        if status == 401 and reply.get("error") == "clock":
            status, reply = self._send(method, path, payload)
        return status, reply

    def _send(self, method: str, path: str, payload: object) -> tuple[int, dict[str, object]]:
        body = b"" if payload is None else json.dumps(payload).encode()
        headers = protocol.request_headers(
            self.token, method, path, body, time.time() + self.offset
        )
        if body:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT_S)
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

    def fetch_plan(self, run: str) -> object:
        status, reply = self.request("GET", f"/v1/plans/{run}")
        if status != 200:
            raise ControlError(f"no plan for {run}: {reply.get('error')}")
        return reply.get("plan")

    def reserve(self, run: str, usd: float) -> int | None:
        """Reserve what a model call could cost; None when the budget cannot cover it."""
        status, reply = self.request("POST", "/v1/budget/reserve", {"run": run, "usd": usd})
        if status != 200:
            raise ControlError(f"the budget could not be checked: {reply.get('error')}")
        if reply.get("ok") is not True:
            return None
        return int(str(reply["reservation"]))


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
        self._counts = threading.Lock()  # made and dropped; never held over the network
        self._sending = threading.Lock()  # one batch in flight at a time
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="derail-sender", daemon=True)

    def start(self) -> None:
        self._thread.start()

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
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2 * TIMEOUT_S)
        while self._send_batch(block=False):
            pass
        self._send_batch(block=False, final=True)

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
                    lines.append(self.queue.get(timeout=wait))
                else:
                    lines.append(self.queue.get_nowait())
            except queue.Empty:
                break
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
        try:
            status, _ = self.client.request("POST", "/v1/records", batch)
        except ControlError:
            status = 0
        if status == 200:
            return True
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
