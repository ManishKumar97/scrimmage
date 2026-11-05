"""A fake OpenAI API for tests.

It answers only from fixture files and has no logic of its own. The test lists one
fixture per turn of a conversation: a request that already holds n assistant messages
gets the fixture at position n. So several runs at once each get the same scripted
conversation, and a retry of a request gets the same reply as its first attempt.

A fixture name may start with a status, such as "429:error_429", for a real error
reply. A ".sse" fixture is sent as a stream of server-sent events. Embeddings and the
Responses API each have one fixture of their own. A request it has no reply for gets an
error, and the error is kept, so a test cannot pass by accident. This is test support
code; it is never in a wheel.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parents[1] / "fixtures" / "openai"
CHAT_PATH = "/v1/chat/completions"
EMBEDDINGS_PATH = "/v1/embeddings"
RESPONSES_PATH = "/v1/responses"


class Reply:
    def __init__(self, name: str) -> None:
        status, _, fixture = name.rpartition(":")
        self.name = name
        self.status = int(status) if status else 200
        path = FIXTURES / fixture
        if not path.suffix:
            path = (
                path.with_suffix(".sse")
                if (FIXTURES / f"{fixture}.sse").exists()
                else path.with_suffix(".json")
            )
        # Read every file up front, so a missing fixture fails the test before it runs.
        self.body = path.read_bytes()
        self.stream = path.suffix == ".sse"


class FakeOpenAI:
    """Serves the listed fixtures, one per turn, on a free local port."""

    def __init__(
        self, replies: list[str], *, embedding: str | None = None, response: str | None = None
    ) -> None:
        self._replies = [Reply(name) for name in replies]
        self._embedding = Reply(embedding) if embedding else None
        self._response = Reply(response) if response else None
        self._used: set[str] = set()
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []  # every path asked for, in order, retries included
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
        listed = [r.name for r in self._replies] + [
            r.name for r in (self._embedding, self._response) if r is not None
        ]
        with self._lock:
            return [name for name in dict.fromkeys(listed) if name not in self._used]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _next(self, path: str, body: bytes) -> Reply | str:
        with self._lock:
            self.paths.append(path)
            request = json.loads(body) if body else {}
            single = {EMBEDDINGS_PATH: self._embedding, RESPONSES_PATH: self._response}
            if path in single:
                reply = single[path]
                if reply is None:
                    return self._error(f"no fixture serves {path}")
            elif path == CHAT_PATH:
                turn = sum(1 for m in request["messages"] if m.get("role") == "assistant")
                if turn >= len(self._replies):
                    return self._error(f"no reply for turn {turn + 1}")
                reply = self._replies[turn]
            else:
                return self._error(f"no fixture serves {path}")
            self.requests.append(request)
            self._used.add(reply.name)
            return reply

    def _error(self, error: str) -> str:
        self.errors.append(error)
        return error

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                reply = fake._next(self.path, body)
                if isinstance(reply, str):
                    status, content, kind = (
                        500,
                        json.dumps({"error": {"message": reply, "type": "fake"}}).encode(),
                        "application/json",
                    )
                else:
                    status, content = reply.status, reply.body
                    kind = "text/event-stream" if reply.stream else "application/json"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args  # keep test output quiet

        return Handler
