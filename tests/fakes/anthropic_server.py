"""A fake Anthropic API for tests.

It answers only from fixture files and has no logic of its own. The test lists one fixture per
turn of a conversation: a request that already holds n assistant messages gets the fixture at
position n. So several runs at once each get the same scripted conversation, and a retry of a
request gets the same reply as its first attempt.

A fixture name may start with a status, such as "401:real_error_401", for a saved real error
reply: a file of its status, headers and body is sent with its own status. "a|b" serves a, or
b to a conversation whose last tool result failed (`is_error`, or a text that starts with
"error"), as a model would change its answer. A ".sse" fixture is sent as a stream of
server-sent events. `/v1/messages/count_tokens` has one fixture of its own. A request it has no
reply for gets an error, and the error is kept, so a test cannot pass by accident. Any other
path is such an error. This is test support code; it is never in a wheel.

`by_model` answers a request by what it holds, not by its turn: a request whose `model` is a key
goes to that rule. A rule reads only the request it answers and keeps no state, so parallel runs
and retries get the same reply.
"""

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parents[1] / "fixtures" / "anthropic"
MESSAGES_PATH = "/v1/messages"
COUNT_PATH = "/v1/messages/count_tokens"
REQUEST_ID = "req_fake_scrimmage"


class Reply:
    def __init__(self, name: str) -> None:
        name, _, after_failure = name.partition("|")
        self.after_failure = Reply(after_failure) if after_failure else None
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
        # A saved real reply holds its status, its content-type and its body: the body is sent,
        # with that status.
        saved = json.loads(self.body) if path.suffix == ".json" else None
        if isinstance(saved, dict) and set(saved) == {"status", "headers", "body"}:
            self.status = int(saved["status"])
            self.body = json.dumps(saved["body"]).encode()


def blocks_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    """A message's content as blocks: a string is one text block."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        found: list[Any] = content  # type: ignore[assignment]
        return [b for b in found if isinstance(b, dict)]
    return []


def tool_failed(request: dict[str, Any]) -> bool:
    """A tool_result in the request failed: it says is_error, or its text starts with error."""
    for message in request.get("messages", []):
        for block in blocks_of(message):
            if block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            text = (
                inner
                if isinstance(inner, str)
                else "".join(str(b.get("text", "")) for b in blocks_of({"content": inner}))
            )
            if block.get("is_error") is True or text.lower().startswith("error"):
                return True
    return False


def turn_of(request: dict[str, Any]) -> int:
    """How many assistant messages the request already holds."""
    return sum(1 for m in request.get("messages", []) if m.get("role") == "assistant")


Rule = Callable[[dict[str, Any]], "Reply | None"]


class FakeAnthropic:
    """Serves the listed fixtures, one per turn, on a free local port."""

    def __init__(
        self,
        replies: list[str],
        *,
        count_tokens: str | None = None,
        by_model: dict[str, Rule] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        """host: the address to listen on. port: 0 picks a free one."""
        self._by_model = dict(by_model or {})
        self._replies = [Reply(name) for name in replies]
        self._count = Reply(count_tokens) if count_tokens else None
        self._used: set[str] = set()
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []  # every path asked for, in order, retries included
        # Every request as it arrived, headers and raw body, so a test can check what a program
        # sent to the model host.
        self.received: list[tuple[dict[str, str], bytes]] = []
        self.errors: list[str] = []
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """The root the anthropic library is given: it adds /v1/messages itself."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def unused(self) -> list[str]:
        """Fixtures the test listed that no request asked for."""
        listed = [r.name for r in self._replies] + ([self._count.name] if self._count else [])
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
            try:
                request = json.loads(body) if body else {}
            except ValueError:
                return self._error(f"the body of {path} is not JSON")
            if path == COUNT_PATH:
                if self._count is None:
                    return self._error(f"no fixture serves {path}")
                self.requests.append(request)
                self._used.add(self._count.name)
                return self._count
            if path != MESSAGES_PATH:
                return self._error(f"no fixture serves {path}")
            if request.get("model") in self._by_model:
                model = request["model"]
                ruled = self._by_model[model](request)
                if ruled is None:
                    return self._error(f"the rule for {model} has no reply")
                self.requests.append(request)
                return ruled
            turn = turn_of(request)
            if turn >= len(self._replies):
                return self._error(f"no reply for turn {turn + 1}")
            reply = self._replies[turn]
            self.requests.append(request)
            self._used.add(reply.name)
            return reply.after_failure if reply.after_failure and tool_failed(request) else reply

    def _error(self, error: str) -> str:
        self.errors.append(error)
        return error

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with fake._lock:
                    fake.received.append((dict(self.headers.items()), body))
                reply = fake._next(self.path, body)
                if isinstance(reply, str):
                    status, kind = 500, "application/json"
                    content = json.dumps(
                        {
                            "type": "error",
                            "error": {"type": "api_error", "message": reply},
                            "request_id": REQUEST_ID,
                        }
                    ).encode()
                else:
                    status, content = reply.status, reply.body
                    kind = "text/event-stream" if reply.stream else "application/json"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("request-id", REQUEST_ID)
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args  # keep test output quiet

        return Handler
