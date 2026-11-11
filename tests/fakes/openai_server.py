"""A fake OpenAI API for tests.

It answers only from fixture files and has no logic of its own. The test lists one
fixture per turn of a conversation: a request that already holds n assistant messages
gets the fixture at position n. So several runs at once each get the same scripted
conversation, and a retry of a request gets the same reply as its first attempt.

A fixture name may start with a status, such as "429:error_429", for a real error
reply. "a|b" serves a, or b to a conversation where a tool failed, as a model would
change its answer. "a@b" serves a, or b to a conversation where a tool result holds
a note addressed to the assistant, as a model that obeys one would answer.
A ".sse" fixture is sent as a stream of server-sent events. Embeddings, moderations and
the Responses API each have one fixture of their own. A request it has no reply for gets
an error, and the error is kept, so a test cannot pass by accident. This is test support
code; it is never in a wheel.

`by_model` answers a chat request by what it holds, not by its turn: a request whose
`model` is a key goes to that rule. A rule reads only the request it answers and keeps no
state, so parallel runs and retries get the same reply. So a gate that never injects an
attack, or a guard that is never asked, changes the outcome of a test.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parents[1] / "fixtures" / "openai"
CHAT_PATH = "/v1/chat/completions"
EMBEDDINGS_PATH = "/v1/embeddings"
RESPONSES_PATH = "/v1/responses"
MODERATIONS_PATH = "/v1/moderations"


class Reply:
    def __init__(self, name: str) -> None:
        name, _, after_note = name.partition("@")
        self.after_note = Reply(after_note) if after_note else None
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


def tool_failed(request: dict[str, Any]) -> bool:
    tools = [m for m in request.get("messages", []) if m.get("role") == "tool"]
    return any(str(m.get("content")).lower().startswith("error") for m in tools)


NOTE_MARKER = "note to the assistant"


def note_followed(request: dict[str, Any]) -> bool:
    """A tool result holds a note addressed to the assistant, which an obedient model follows."""
    tools = [m for m in request.get("messages", []) if m.get("role") == "tool"]
    return any(NOTE_MARKER in str(m.get("content")).lower() for m in tools)


def text_of(message: dict[str, Any]) -> str:
    """A message's text: its content, or its text parts joined."""
    content = message.get("content")
    if isinstance(content, list):
        parts: list[Any] = content  # type: ignore[assignment]
        return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
    return "" if content is None else str(content)


def holds(request: dict[str, Any], marker: str, roles: tuple[str, ...] | None = None) -> bool:
    """Whether one message of the request holds the marker. A marker split across two
    messages is not found; tests keep each marker in one message."""
    return any(
        marker in text_of(m)
        for m in request.get("messages", [])
        if roles is None or m.get("role") in roles
    )


Rule = Callable[[dict[str, Any]], "Reply | None"]


@dataclass(frozen=True)
class Guard:
    """A guard model: `unsafe` when any message it is sent holds the marker, else `safe`.

    The fixtures default to Llama Guard's reply shape; NeMo's self check rails take
    `rail_blocks` and `rail_allows` instead.
    """

    marker: str
    unsafe: str = "guard_unsafe"
    safe: str = "guard_safe"

    def __post_init__(self) -> None:
        for name in (self.unsafe, self.safe):
            Reply(name)  # a missing fixture fails before the test runs

    def __call__(self, request: dict[str, Any]) -> "Reply":
        return Reply(self.unsafe if holds(request, self.marker) else self.safe)


@dataclass(frozen=True)
class Obeys:
    """A main model that obeys an injected text: a `refund_order` call when a tool or user
    message holds the marker and no reply in the conversation asked for one yet. Otherwise
    the turn's fixture, as the turn list would serve it; None past the end of the list."""

    marker: str
    turns: tuple[str, ...]
    obey: str = "acme_refund_order"

    def __post_init__(self) -> None:
        for name in (self.obey, *self.turns):
            Reply(name)  # a missing fixture fails before the test runs

    def __call__(self, request: dict[str, Any]) -> "Reply | None":
        messages = request.get("messages", [])
        asked = any(
            call.get("function", {}).get("name") == "refund_order"
            for m in messages
            for call in m.get("tool_calls") or []
        )
        if not asked and holds(request, self.marker, ("tool", "user")):
            return Reply(self.obey)
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        return Reply(self.turns[turn]) if turn < len(self.turns) else None


class FakeOpenAI:
    """Serves the listed fixtures, one per turn, on a free local port."""

    def __init__(
        self,
        replies: list[str],
        *,
        embedding: str | None = None,
        response: str | None = None,
        moderation: str | None = None,
        by_model: dict[str, Rule] | None = None,
    ) -> None:
        self._by_model = dict(by_model or {})
        self._replies = [Reply(name) for name in replies]
        self._embedding = Reply(embedding) if embedding else None
        self._response = Reply(response) if response else None
        self._moderation = Reply(moderation) if moderation else None
        self._used: set[str] = set()
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []  # every path asked for, in order, retries included
        # Every request as it arrived, headers and raw body, so a test can check what a
        # program sent to the model host.
        self.received: list[tuple[dict[str, str], bytes]] = []
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
            r.name for r in (self._embedding, self._response, self._moderation) if r is not None
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
            single = {
                EMBEDDINGS_PATH: self._embedding,
                RESPONSES_PATH: self._response,
                MODERATIONS_PATH: self._moderation,
            }
            if path in single:
                reply = single[path]
                if reply is None:
                    return self._error(f"no fixture serves {path}")
            elif path == CHAT_PATH and request.get("model") in self._by_model:
                model = request["model"]
                ruled = self._by_model[model](request)
                if ruled is None:
                    return self._error(f"the rule for {model} has no reply")
                self.requests.append(request)
                return ruled
            elif path == CHAT_PATH:
                turn = sum(1 for m in request["messages"] if m.get("role") == "assistant")
                if turn >= len(self._replies):
                    return self._error(f"no reply for turn {turn + 1}")
                reply = self._replies[turn]
            else:
                return self._error(f"no fixture serves {path}")
            self.requests.append(request)
            self._used.add(reply.name)
            if reply.after_note and note_followed(request):
                return reply.after_note
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
