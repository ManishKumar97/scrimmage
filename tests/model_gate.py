"""A model call through the real gate in one process: the openai library, a mock transport
standing in for the provider, a budget that notes each reservation, and lines kept in a
list. Shared by the tests of replace and slow on a model call."""

from collections.abc import Callable
from typing import Any

import httpx2
import openai

from scrimmage import http_gate, provider_openai, state
from scrimmage.budget import Ledger, Shortfall
from scrimmage.config import Fault, ModelHost
from scrimmage.plan import Plan

GUARD = "llama-guard-test"
URL = "https://guard.test/v1"
USAGE = {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}
PRICE = provider_openai.Rates(1.0, 2.0)  # dollars per million tokens
Handler = Callable[[httpx2.Request], httpx2.Response]


def completion(content: object = "unsafe\nS14", **message: object) -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": GUARD,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": "stop",
            }
        ],
        "usage": USAGE,
    }


class Provider:
    """The mock transport's other end: what it was sent, in order, and one reply for each."""

    def __init__(self, events: list[str], reply: Handler | None = None) -> None:
        self.events = events
        self.sent: list[bytes] = []
        self.reply = reply or (lambda request: httpx2.Response(200, json=completion()))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.events.append("sent")
        self.sent.append(request.read())
        return self.reply(request)


class Budget:
    """Stands in for the runner's ledger, and notes each reservation in the shared events."""

    def __init__(self, events: list[str]) -> None:
        self.ledger = Ledger(10.0)
        self.events = events

    def reserve(
        self, run: str, usd: float, label: str = "", ticket: int | None = None
    ) -> int | Shortfall:
        self.events.append("reserve")
        return self.ledger.reserve(usd, run, label, ticket)

    def release(self, ticket: int) -> None:
        self.ledger.release(ticket)


class Sink:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.blobs: dict[str, bytes] = {}
        self.settles: list[tuple[int, float]] = []

    def put(self, line: dict[str, Any], *, keep: bool = False) -> None:
        self.lines.append(line)

    def put_blob(self, sha: str, data: bytes) -> bool:
        self.blobs[sha] = data
        return True

    def put_settle(self, reservation: int, usd: float) -> None:
        self.settles.append((reservation, usd))


# The library's own functions, so each Gate patches them afresh with its own run.
SENDS = (httpx2.Client._send_single_request, httpx2.AsyncClient._send_single_request)


class Gate:
    def __init__(self, *faults: Fault) -> None:
        httpx2.Client._send_single_request, httpx2.AsyncClient._send_single_request = SENDS
        self.events: list[str] = []
        self.sink = Sink()
        plan = Plan(
            "run-1", "t", "row", (), faults, (), (), 60.0, budget_usd=10.0,
            model_hosts=(ModelHost("guard.test", "openai"),), prices=((GUARD, PRICE),),
        )  # fmt: skip
        self.run = state.RunState(plan, self.sink, [])
        state.install(self.run)
        self.budget = Budget(self.events)
        ctx = http_gate.Context(self.run, self.budget, self.sink, lambda why: None)
        http_gate.patch(httpx2, ctx)

    @property
    def line(self) -> dict[str, Any]:
        return next(x for x in self.sink.lines if x.get("kind") == "model")

    def result(self, line: dict[str, Any] | None = None) -> str:
        return self.sink.blobs[(line or self.line)["result"]["sha256"]].decode()


def guard(provider: Provider, **kw: Any) -> openai.OpenAI:
    http = openai.DefaultHttpxClient(transport=httpx2.MockTransport(provider))
    return openai.OpenAI(base_url=URL, api_key="k", max_retries=0, http_client=http, **kw)


def ask(client: openai.OpenAI, text: str = "Order 1042 shipped.") -> str:
    reply = client.chat.completions.create(
        model=GUARD, messages=[{"role": "user", "content": text}]
    )
    return reply.choices[0].message.content or ""
