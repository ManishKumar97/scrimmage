"""A replay of OpenAI model calls through the real gate against the repo's fake OpenAI server.

Each scenario sends one or more requests through the gated httpx2 client and keeps, for every
call: the reply bytes and status the agent got, the line the gate wrote (cost, hold, tokens,
op, label and the rest), the reservations the budget saw and the settles. Built replies and
made-up ids are seeded, so two runs of the same code give the same capture. The capture of
origin/main before the provider seam is the golden file; a refactor that changes a byte of
cost, hold, record or reply fails the comparison."""

import hashlib
import json
import os
import random
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from fakes.openai_server import FakeOpenAI
from scrimmage import http_gate, state
from scrimmage.budget import Ledger, Shortfall
from scrimmage.config import Fault, ModelHost
from scrimmage.plan import Plan
from scrimmage.prices import Rates

MODEL = "gpt-4o-mini"  # in the shipped table
CUSTOM = "replay-model"  # priced below, with every kind of rate
PRICES = (
    (
        CUSTOM,
        Rates(
            2.0,
            8.0,
            0.5,
            3.0,
            4.0,
            tiers=(("priority", Rates(4.0, 16.0)), ("flex", Rates(1.0, 4.0))),
            above=200,
            long=Rates(4.0, 12.0),
        ),
    ),
)
VOLATILE = {
    "ts",
    "t",
    "at",
    "started",
    "ended",
    "elapsed",
    "duration",
    "ms",
    "time",
    "wall",
    "pid",
    "start",
    "host",
}
SEED = 20261122
NOW = 1_700_000_000.0
KEPT_HEADERS = ("content-type", "x-request-id", "retry-after", "content-length")

CHAT = "/chat/completions"
RESPONSES = "/responses"
CHAT_BODY: dict[str, Any] = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
TOOL = {
    "type": "function",
    "function": {"name": "get_order", "parameters": {"type": "object", "properties": {}}},
}
WITH_TOOL = {**CHAT_BODY, "tools": [TOOL]}
RESPONSES_BODY: dict[str, Any] = {"model": MODEL, "input": "hi"}


def chat(**more: Any) -> dict[str, Any]:
    return {**CHAT_BODY, **more}


def responses(**more: Any) -> dict[str, Any]:
    return {**RESPONSES_BODY, **more}


Body = dict[str, Any] | Callable[[list[bytes]], dict[str, Any]]


def built_from(prior: list[bytes]) -> dict[str, Any]:
    """The next chat request an agent builds from the reply it got: that reply's message, then
    the result of each tool call it made."""
    choices = json.loads(prior[-1]).get("choices") or [{"message": {"role": "assistant"}}]
    message: dict[str, Any] = choices[0]["message"]
    calls: list[dict[str, Any]] = message.get("tool_calls") or []
    results = [{"role": "tool", "tool_call_id": call["id"], "content": "ok"} for call in calls]
    return {**WITH_TOOL, "messages": [*CHAT_BODY["messages"], message, *results]}


def chained_to(prior: list[bytes]) -> dict[str, Any]:
    """A Responses request that asks OpenAI to look up the reply it got."""
    return responses(previous_response_id=json.loads(prior[-1]).get("id", "resp_0"))


class Scenario:
    def __init__(
        self,
        name: str,
        calls: list[tuple[str, Body]],
        replies: list[str] | None = None,
        *,
        faults: tuple[Fault, ...] = (),
        budget: float | None = 5.0,
        embedding: str | None = None,
        response: str | None = None,
        moderation: str | None = None,
        tier: str | None = None,
        probe: bool = False,
        listed: bool = True,
    ) -> None:
        self.name = name
        self.calls = calls
        self.replies = replies or []
        self.faults = faults
        self.budget = budget
        self.embedding, self.response, self.moderation = embedding, response, moderation
        self.tier, self.probe, self.listed = tier, probe, listed


def scenarios() -> list[Scenario]:
    found: list[Scenario] = []

    def add(*args: Any, **kw: Any) -> None:
        found.append(Scenario(*args, **kw))

    # Plain calls, each fixture, with the budget on.
    for fixture in (
        "chat_answer",
        "chat_answer_cached",
        "chat_tool_call",
        "chat_stream",
        "chat_stream_usage",
        "chat_stream_usage_cached",
        "error_429",
        "error_500",
        "real_chat_answer",
        "real_chat_tool_call",
        "real_chat_truncated",
        "real_chat_tool_call_truncated",
        "real_error_401",
        "real_error_context_length",
        "real_error_tool_name",
        "429:error_429",
        "500:error_500",
    ):
        streaming = fixture.endswith("stream") or "stream_usage" in fixture
        body = chat(stream=True) if streaming else chat()
        add(f"chat {fixture}", [(CHAT, body)], [fixture])
        add(f"chat {fixture} custom", [(CHAT, {**body, "model": CUSTOM})], [fixture])
    add("chat n and cap", [(CHAT, chat(n=3, max_tokens=50))], ["chat_answer"])
    add("chat completion cap", [(CHAT, chat(max_completion_tokens=77))], ["chat_answer"])
    add("chat n bad", [(CHAT, chat(n=True, max_tokens=-4))], ["chat_answer"])
    for tier in ("priority", "auto", "flex", "scale", "default"):
        body = {**chat(service_tier=tier), "model": CUSTOM}
        add(f"chat tier {tier}", [(CHAT, body)], ["chat_answer"])
    add("chat plan tier", [(CHAT, {**chat(), "model": CUSTOM})], ["chat_answer"], tier="flex")
    add("chat no budget", [(CHAT, chat())], ["chat_answer"], budget=None)
    add("chat unpriced", [(CHAT, chat(model="no-such-model"))], ["chat_answer"])
    add(
        "chat no model",
        [(CHAT, {"messages": [{"role": "user", "content": "hi"}]})],
        ["chat_answer"],
    )
    add(
        "chat long input",
        [(CHAT, chat(model=CUSTOM, messages=[{"role": "user", "content": "x" * 4000}]))],
        ["chat_answer"],
    )
    add("chat retry header", [(CHAT, chat())], ["chat_answer"])
    add(
        "chat two turns",
        [
            (CHAT, chat()),
            (
                CHAT,
                chat(
                    messages=[
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "a"},
                        {"role": "user", "content": "more"},
                    ]
                ),
            ),
        ],
        ["chat_tool_call", "chat_answer"],
    )
    for fixture in (
        "response_text",
        "response_stream",
        "response_stream_cached",
        "real_response_answer",
        "real_response_incomplete",
        "real_response_function_call_truncated",
    ):
        body = responses(stream=True) if "stream" in fixture else responses()
        add(f"responses {fixture}", [(RESPONSES, body)], response=fixture)
        add(
            f"responses {fixture} custom",
            [(RESPONSES, {**body, "model": CUSTOM})],
            response=fixture,
        )
    add(
        "responses cap",
        [(RESPONSES, responses(max_output_tokens=33, service_tier="priority", model=CUSTOM))],
        response="response_text",
    )
    add("embeddings", [("/embeddings", {"model": MODEL, "input": "x"})], embedding="embedding")
    add(
        "embeddings real",
        [("/embeddings", {"model": MODEL, "input": "x"})],
        embedding="real_embedding",
    )
    add("moderations", [("/moderations", {"input": "x"})], moderation="moderation")
    add("input tokens", [("/responses/input_tokens", responses())], response="response_text")
    add("read path 404", [("/models", {})], ["chat_answer"])
    add("unlisted host, not probing", [(CHAT, chat())], ["chat_answer"], listed=False)
    add("unlisted host, probing", [(CHAT, chat())], ["chat_answer"], listed=False, probe=True)
    # Tools the request offers.
    mcp = responses(tools=[{"type": "mcp", "server_label": "docs", "server_url": "https://x"}])
    add("hosted write refused", [(RESPONSES, mcp)], response="response_text")
    odd = responses(tools=[{"type": "made_up_tool"}])
    add("unknown tool type", [(RESPONSES, odd)], response="response_text")
    add(
        "hosted read",
        [(RESPONSES, responses(tools=[{"type": "web_search"}]))],
        response="response_text",
    )
    other = {**WITH_TOOL, "tools": [{**TOOL, "function": {"name": "get_other"}}]}
    add("unseen agent tool", [(CHAT, other)], ["chat_answer"])
    add("offered tool is seen", [(CHAT, WITH_TOOL)], ["chat_answer"])
    add(
        "offered tool, responses",
        [(RESPONSES, responses(tools=[{"type": "function", "name": "get_order"}]))],
        response="response_text",
    )
    add(
        "old functions form seen",
        [(CHAT, chat(functions=[{"name": "get_order"}]))],
        ["chat_answer"],
    )
    add(
        "old functions form",
        [(CHAT, chat(functions=[{"name": "f"}], web_search_options={}))],
        ["chat_answer"],
    )

    # Faults.
    def fault(word: str, **kw: Any) -> tuple[Fault, ...]:
        return (Fault("model", word, "always", **kw),)

    for status in (400, 401, 404, 418, 429, 500, 503):
        retry = {"retry_after": 7} if status in (429, 503) else {}
        add(
            f"fault status {status}",
            [(CHAT, chat())],
            ["chat_answer"],
            faults=fault(f"status {status}", **retry),
        )
    add(
        "fault status on responses",
        [(RESPONSES, responses())],
        response="response_text",
        faults=fault("status 429"),
    )
    for word in ("timeout", "refused", "reset", "dns", "empty", "bad body", "context too long"):
        add(f"fault {word}", [(CHAT, chat())], ["chat_answer"], faults=fault(word))
    add(
        "fault bad body responses",
        [(RESPONSES, responses())],
        response="response_text",
        faults=fault("bad body"),
    )
    add(
        "fault bad body embeddings",
        [("/embeddings", {"model": MODEL, "input": "x"})],
        embedding="embedding",
        faults=fault("bad body"),
    )
    add(
        "fault bad body moderations",
        [("/moderations", {"input": "x"})],
        moderation="moderation",
        faults=fault("bad body"),
    )
    add(
        "fault bad body stream",
        [(CHAT, chat(stream=True))],
        ["chat_stream"],
        faults=fault("bad body"),
    )
    add(
        "fault context too long responses",
        [(RESPONSES, responses())],
        response="response_text",
        faults=fault("context too long"),
    )
    add(
        "fault model narrowed",
        [(CHAT, chat())],
        ["chat_answer"],
        faults=fault("status 500", model="other"),
    )
    add("fault refuse chat", [(CHAT, chat())], ["chat_answer"], faults=fault("refuse"))
    add(
        "fault refuse structured",
        [
            (
                CHAT,
                chat(
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "x", "schema": {}},
                    }
                ),
            )
        ],
        ["chat_answer"],
        faults=fault("refuse"),
    )
    add(
        "fault refuse responses",
        [(RESPONSES, responses())],
        response="response_text",
        faults=fault("refuse"),
    )
    add("fault refuse stream", [(CHAT, chat(stream=True))], ["chat_stream"], faults=fault("refuse"))
    add("fault unknown tool", [(CHAT, WITH_TOOL)], ["chat_answer"], faults=fault("unknown tool"))
    add(
        "fault unknown tool no tools",
        [(CHAT, chat())],
        ["chat_answer"],
        faults=fault("unknown tool"),
    )
    add(
        "fault unknown tool none",
        [(CHAT, {**WITH_TOOL, "tool_choice": "none"})],
        ["chat_answer"],
        faults=fault("unknown tool"),
    )
    add(
        "fault unknown tool responses",
        [(RESPONSES, responses())],
        response="response_text",
        faults=fault("unknown tool"),
    )
    for word, extra in (
        ("replace", {"text": "NEW", "find": "order"}),
        ("replace", {"text": "NEW"}),
        ("garble", {}),
        ("truncate", {}),
        ("bad tool args", {}),
    ):
        for fixture in ("chat_answer", "chat_tool_call", "real_chat_answer", "real_chat_tool_call"):
            add(
                f"fault {word} chat {fixture}",
                [(CHAT, chat())],
                [fixture],
                faults=fault(word, **extra),
            )
        for fixture in ("response_text", "real_response_answer"):
            add(
                f"fault {word} responses {fixture}",
                [(RESPONSES, responses())],
                response=fixture,
                faults=fault(word, **extra),
            )
        add(
            f"fault {word} stream",
            [(CHAT, chat(stream=True))],
            ["chat_stream"],
            faults=fault(word, **extra),
        )
    add("fault slow", [(CHAT, chat())], ["chat_answer"], faults=fault("slow 0.01s"))
    add(
        "fault responses conversation",
        [(RESPONSES, responses(conversation="conv_1"))],
        response="response_text",
        faults=fault("garble"),
    )
    # A faulted reply, then the request the agent builds from it.
    add(
        "marker chat",
        [(CHAT, WITH_TOOL), (CHAT, built_from)],
        ["chat_answer", "real_error_context_length"],
        faults=(Fault("model", "unknown tool", "first 1"),),
    )
    add(
        "marker garble",
        [(CHAT, WITH_TOOL), (CHAT, built_from)],
        ["chat_tool_call", "real_error_context_length"],
        faults=(Fault("model", "garble", "first 1"),),
    )
    add(
        "marker truncate",
        [(CHAT, WITH_TOOL), (CHAT, built_from)],
        ["chat_tool_call", "real_error_tool_name"],
        faults=(Fault("model", "truncate", "first 1"),),
    )
    add(
        "marker bad tool args",
        [(CHAT, WITH_TOOL), (CHAT, built_from)],
        ["chat_tool_call", "real_error_tool_name"],
        faults=(Fault("model", "bad tool args", "first 1"),),
    )
    add(
        "chain responses",
        [(RESPONSES, responses()), (RESPONSES, chained_to)],
        response="response_text",
        faults=(Fault("model", "garble", "first 1"),),
    )
    add(
        "chain refuse responses",
        [(RESPONSES, responses()), (RESPONSES, chained_to)],
        response="response_text",
        faults=(Fault("model", "refuse", "first 1"),),
    )
    return found


def settled(value: object) -> object:
    """A line without the keys that hold a clock reading."""
    if isinstance(value, dict):
        return {k: settled(v) for k, v in sorted(value.items()) if k not in VOLATILE}
    if isinstance(value, list):
        return [settled(v) for v in value]
    return value


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


class Budget:
    def __init__(self, limit: float | None) -> None:
        self.ledger = Ledger(limit)
        self.reserved: list[float] = []

    def reserve(
        self, run: str, usd: float, label: str = "", ticket: int | None = None
    ) -> int | Shortfall:
        self.reserved.append(round(usd, 12))
        return self.ledger.reserve(usd, run, label, ticket)

    def release(self, ticket: int) -> None:
        self.ledger.release(ticket)


SENDS = (httpx2.Client._send_single_request, httpx2.AsyncClient._send_single_request)


def run_one(scenario: Scenario) -> dict[str, object]:
    httpx2.Client._send_single_request, httpx2.AsyncClient._send_single_request = SENDS
    random.seed(SEED)
    fake = FakeOpenAI(
        scenario.replies,
        embedding=scenario.embedding,
        response=scenario.response,
        moderation=scenario.moderation,
    )
    # The fake polls for a stop every half second, so each scenario would wait that long.
    fake._thread = threading.Thread(  # type: ignore[reportPrivateUsage]
        target=fake._server.serve_forever,
        kwargs={"poll_interval": 0.005},
        daemon=True,  # type: ignore[reportPrivateUsage]
    )
    fake.start()
    try:
        hosts = (ModelHost("127.0.0.1", "openai"),) if scenario.listed else ()
        plan = Plan(
            "run-1",
            "t",
            "row",
            (),
            scenario.faults,
            ("acme.tools:get_order",),
            (),
            60.0,
            budget_usd=scenario.budget,
            model_hosts=hosts,
            prices=PRICES,
            service_tier=scenario.tier,
            probe=scenario.probe,
        )
        sink, budget = Sink(), Budget(scenario.budget)
        run = state.RunState(plan, sink, [])
        state.install(run)
        stops: list[str] = []
        ctx = http_gate.Context(run, budget, sink, stops.append)
        http_gate.patch(httpx2, ctx)
        replies: list[object] = []
        contents: list[bytes] = []
        with httpx2.Client() as client:
            for path, made in scenario.calls:
                url = fake.base_url + path
                body = made if isinstance(made, dict) else made(contents)
                payload = json.dumps(body).encode()
                try:
                    got = client.post(
                        url, content=payload, headers={"content-type": "application/json"}
                    )
                    got.read()
                except BaseException as error:  # a fault raises what the library would see
                    replies.append({"raised": type(error).__name__, "text": str(error)[:200]})
                    continue
                contents.append(got.content)
                replies.append(
                    {
                        "status": got.status_code,
                        "headers": {k: got.headers[k] for k in KEPT_HEADERS if k in got.headers},
                        "body": hashlib.sha256(got.content).hexdigest(),
                        "bytes": got.content.decode("latin-1"),
                    }
                )
        state.install(None)
        blobs = {sha: hashlib.sha256(data).hexdigest() for sha, data in sink.blobs.items()}
        return {
            "replies": replies,
            "lines": settled(sink.lines),
            "reserved": budget.reserved,
            "settles": sink.settles,
            "blobs": sorted(blobs),
            "sent": [hashlib.sha256(b).hexdigest() for _, b in fake.received],
            "stops": stops,
        }
    finally:
        fake.stop()
        httpx2.Client._send_single_request, httpx2.AsyncClient._send_single_request = SENDS


def timed(scenario: Scenario) -> dict[str, object]:
    began = time.monotonic()
    found = run_one(scenario)
    if os.environ.get("REPLAY_VERBOSE"):
        print(f"{time.monotonic() - began:6.2f}s {scenario.name}", file=sys.stderr, flush=True)
    return found


def capture(only: Callable[[Scenario], bool] | None = None) -> dict[str, object]:
    import secrets

    real = time.time, secrets.choice, secrets.token_hex
    dice = random.Random(SEED)  # the made-up ids of a built reply
    time.time = lambda: NOW  # a built reply's created field
    secrets.choice = dice.choice  # type: ignore[assignment]
    secrets.token_hex = lambda n=32: dice.randbytes(n).hex()  # type: ignore[assignment]
    try:
        return {s.name: timed(s) for s in scenarios() if only is None or only(s)}
    finally:
        time.time, secrets.choice, secrets.token_hex = real


GOLDEN = Path(__file__).parent / "golden" / "provider_replay.json"


def dumps(found: dict[str, object]) -> str:
    rows = [
        f"{json.dumps(k)}: {json.dumps(v, sort_keys=True, default=str)}" for k, v in found.items()
    ]
    return "{\n" + ",\n".join(rows) + "\n}\n"  # one scenario a line, so a change diffs by line


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else str(GOLDEN)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(dumps(capture()))
    print(f"wrote {out}")
