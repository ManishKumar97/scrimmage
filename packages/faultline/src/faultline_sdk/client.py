"""The SDK's side of the control protocol: signed requests and the record sender.

Record lines go to a queue and a sender thread sends them in batches, at least every
100 ms and at exit, so a gate never waits for the network. A full queue drops a line
and marks the record incomplete, except a refusal, which waits for room.
"""

import http.client
import json
import queue
import threading
import time
from urllib.parse import urlsplit

from faultline_sdk import protocol
from faultline_sdk.plan import obj

QUEUE_SIZE = 10_000
BATCH_LINES = 500
BATCH_S = 0.1
TIMEOUT_S = 10.0


class ControlError(Exception):
    """The control server could not be reached, refused us, or sent a bad reply."""


class ControlClient:
    def __init__(self, url: str, token: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ControlError(f"FAULTLINE_CONTROL_URL {url!r} is not http://host:port")
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


class Sender:
    """Sends record lines in the background, in batches.

    A batch that fails to send is kept and sent again with the next one; the record
    store writes a line once however often it arrives. The last batch, sent at exit,
    says how many lines this process made, so the runner can tell a record with a gap
    from a complete one.
    """

    def __init__(self, client: ControlClient, run: str) -> None:
        self.client = client
        self.run = run
        self.queue: queue.Queue[dict[str, object]] = queue.Queue(QUEUE_SIZE)
        self.made = 0  # lines handed to put, dropped ones included
        self.dropped = 0
        self.unsent: list[dict[str, object]] = []
        self._lock = threading.Lock()  # one batch in flight at a time
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="faultline-sender", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        """Queue a line. keep: wait for room instead of dropping (refusals)."""
        with self._lock:
            self.made += 1
        if keep:
            self.queue.put(line)
            return
        try:
            self.queue.put_nowait(line)
        except queue.Full:
            with self._lock:
                self.dropped += 1

    def flush(self) -> None:
        """Send everything queued now, then the closing batch. Called at exit.

        The sender thread may hold a batch it has taken from the queue but not sent, so
        wait for it to finish that batch before sending the rest.
        """
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
        with self._lock:
            lines = self.unsent + lines
            if len(lines) > QUEUE_SIZE:  # the server has been away a long time
                self.dropped += len(lines) - QUEUE_SIZE
                lines = lines[:QUEUE_SIZE]
            dropped, self.dropped = self.dropped, 0
            if not lines and not dropped and not final:
                return False
            batch: dict[str, object] = {"run": self.run, "lines": lines, "dropped": dropped}
            if final:
                batch["made"] = self.made
            try:
                status, _ = self.client.request("POST", "/v1/records", batch)
            except ControlError:
                status = 0
            if status == 200:
                self.unsent = []
                return True
            self.unsent = lines
            self.dropped += dropped
            return False
