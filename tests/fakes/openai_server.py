"""A fake OpenAI API for tests.

It answers only from fixture files and has no logic of its own. The test lists one
fixture per turn of a conversation: a request that already holds n assistant messages
gets the fixture at position n. So several runs at once each get the same scripted
conversation. A request it has no reply for gets an error, and the error is kept, so a
test cannot pass by accident. This is test support code; it is never in a wheel.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parents[1] / "fixtures" / "openai"
CHAT_PATH = "/v1/chat/completions"


class FakeOpenAI:
    """Serves the listed fixtures, one per turn, on a free local port."""

    def __init__(self, replies: list[str]) -> None:
        # Read every file up front, so a missing fixture fails the test before it runs.
        self._replies = [(name, (FIXTURES / f"{name}.json").read_bytes()) for name in replies]
        self._used: set[int] = set()
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/v1"

    @property
    def unused(self) -> list[str]:
        """Fixtures the test listed that no request asked for."""
        with self._lock:
            return [n for i, (n, _) in enumerate(self._replies) if i not in self._used]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _next(self, path: str, body: bytes) -> tuple[int, bytes]:
        with self._lock:
            if path != CHAT_PATH:
                error = f"no fixture serves {path}"
            else:
                request = json.loads(body)
                turn = sum(1 for m in request["messages"] if m.get("role") == "assistant")
                if turn < len(self._replies):
                    self.requests.append(request)
                    self._used.add(turn)
                    return 200, self._replies[turn][1]
                error = f"no reply for turn {turn + 1}"
            self.errors.append(error)
        return 500, json.dumps({"error": {"message": error, "type": "fake"}}).encode()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                status, reply = fake._next(self.path, body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args  # keep test output quiet

        return Handler
