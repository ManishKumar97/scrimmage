"""The fake OpenAI API as a server in a pod: `serve.py FIXTURE...`, one fixture per turn.

It runs the same FakeOpenAI the other tests use, on a fixed port, and answers only from the
fixture files in the image. This is test support code; it is never in a wheel.

A chat request whose user message holds HOLD_MARKER waits, for at most HOLD_S, until a test
releases it, so the test can change the cluster while that run is inside its model call. The
hold is controlled on HOLD_PORT, which listens on the pod's loopback only: a test reaches it
with `kubectl port-forward`, and no other pod can. A request without the marker never waits.
"""

import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fakes.openai_server import CHAT_PATH, FakeOpenAI, Reply, holds

PORT = 8080
HOLD_PORT = 8081
HOLD_MARKER = "[scrimmage-hold]"
HOLD_S = 20.0


class Hold:
    """The held calls. `waiting` counts the calls held now, `held` every call that was."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.lock = threading.Lock()
        self.waiting = 0
        self.held = 0

    def wait(self) -> None:
        with self.lock:
            self.waiting += 1
            self.held += 1
        try:
            self.released.wait(HOLD_S)
        finally:
            with self.lock:
                self.waiting -= 1

    def release(self) -> int:
        """Let every held call go, and every later one pass. Returns how many were held."""
        with self.lock:
            self.released.set()
            return self.waiting


class HoldingFake(FakeOpenAI):
    """FakeOpenAI, with a chat request that holds the marker kept until the hold is released."""

    def __init__(self, replies: list[str], hold: Hold) -> None:
        super().__init__(replies, host="0.0.0.0", port=PORT)
        self.hold = hold

    def _next(self, path: str, body: bytes) -> Reply | str:
        if path == CHAT_PATH and body and holds(json.loads(body), HOLD_MARKER, ("user",)):
            self.hold.wait()  # outside the fake's lock, so other requests are answered meanwhile
        return super()._next(path, body)


def control_server(fake: FakeOpenAI, hold: Hold) -> ThreadingHTTPServer:
    """GET /hold says what is held and how many requests the fake got; POST /hold/release
    lets the held calls go."""

    class Handler(BaseHTTPRequestHandler):
        def answer(self, doc: dict[str, object]) -> None:
            body = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/hold":
                self.send_error(404)
                return
            with hold.lock:
                doc: dict[str, object] = {"waiting": hold.waiting, "held": hold.held}
            doc["released"] = hold.released.is_set()
            doc["requests"] = len(fake.received)
            self.answer(doc)

        def do_POST(self) -> None:
            if self.path != "/hold/release":
                self.send_error(404)
                return
            self.answer({"released": hold.release()})

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    return ThreadingHTTPServer(("127.0.0.1", HOLD_PORT), Handler)


def main(replies: list[str]) -> None:
    hold = Hold()
    fake = HoldingFake(replies, hold)
    fake.start()
    control = control_server(fake, hold)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    stop = threading.Event()

    def on_signal(signum: int, frame: Any) -> None:
        del signum, frame
        stop.set()  # as PID 1 the process gets no default action for SIGTERM

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print(f"fake model on port {PORT}, fixtures {replies}; hold on {HOLD_PORT}", flush=True)
    stop.wait()
    control.shutdown()
    fake.stop()


if __name__ == "__main__":
    main(sys.argv[1:])
