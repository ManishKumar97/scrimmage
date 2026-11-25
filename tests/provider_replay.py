"""A replay of OpenAI model calls through the real gate against the repo's fake OpenAI server
and a scripted one.

Each scenario sends one or more requests through a gated client (httpx2, httpx, the openai
library, each sync or async) and keeps, for every call: the reply bytes and status the agent
got, the line the gate wrote (cost, hold, tokens, op, label and the rest), the reservations
the budget saw and the settles. Built replies and made-up ids are seeded, so two runs of the
same code give the same capture. The capture of the source before a refactor is the golden
file; a refactor that changes a byte of cost, hold, record or reply fails the comparison."""

import asyncio
import dataclasses
import hashlib
import json
import os
import random
import socket
import sys
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2

from fakes.anthropic_server import FakeAnthropic
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
    "slowed_ms",  # measured: how long a slowed call waited, which load stretches
}
MEASURED_KEYS = {"slowed_ms"}
MEASURED: dict[str, list[int]] = {}  # the measured values settled() dropped, by key
SEED = 20261122
NOW = 1_700_000_000.0
KEPT_HEADERS = ("content-type", "x-request-id", "request-id", "retry-after", "content-length")

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


Body = dict[str, Any] | bytes | Callable[[list[bytes]], dict[str, Any]]
FIXTURES = Path(__file__).parent / "fixtures" / "openai"


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


def chained_to_item(prior: list[bytes]) -> dict[str, Any]:
    """A Responses request that asks OpenAI to look up the first item of the reply it got."""
    item = json.loads(prior[-1])["output"][0]["id"]
    return responses(input=[{"type": "item_reference", "id": item}])


def resent_with(prior: list[bytes]) -> dict[str, Any]:
    """A Responses request that sends the output items of the reply it got back as input."""
    return responses(input=[{"role": "user", "content": "hi"}, *json.loads(prior[-1])["output"]])


@dataclasses.dataclass
class Served:
    """What a scripted server sends: a status, headers, the body's chunks, and how it ends.
    cut: the connection drops after the chunks. raises: the name of the library's error that
    is raised instead of any reply."""

    status: int = 200
    headers: dict[str, str] = dataclasses.field(default_factory=dict[str, str])
    chunks: tuple[bytes, ...] = ()
    cut: bool = False
    raises: str | None = None


Script = Callable[[bytes], Served]


def static(served: Served) -> Script:
    return lambda body: served


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def answer(base: str = "chat_answer", **top: Any) -> bytes:
    """A saved reply with these top-level fields put in or over its own."""
    return json.dumps({**load(base), **top}).encode()


def chat_usage(**more: Any) -> dict[str, Any]:
    """The chat answer's usage, with these fields put over its own."""
    return {**load("chat_answer")["usage"], **more}


def json_reply(body: bytes, **headers: str) -> Served:
    return Served(headers=headers, chunks=(body,))


def sse(name: str) -> bytes:
    return (FIXTURES / f"{name}.sse").read_bytes()


def packed(body: bytes) -> bytes:
    """gzip of the body, written by zlib, whose header holds no time and the same system byte
    on every Python; the gzip module's header differs between versions."""
    packer = zlib.compressobj(6, zlib.DEFLATED, 31)
    return packer.compress(body) + packer.flush()


def pieces(body: bytes, size: int) -> tuple[bytes, ...]:
    return tuple(body[i : i + size] for i in range(0, len(body), size))


class Scenario:
    def __init__(
        self,
        name: str,
        calls: list[Any],
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
        client: str = "httpx2",
        script: Script | None = None,
        hosts: int = 1,
        dead: bool = False,
        real: tuple[str, ...] = (),
        injected: tuple[str, ...] = (),
        provider: str = "openai",
        count_tokens: str | None = None,
        both: bool = False,
        seen: tuple[str, ...] = (),
    ) -> None:
        """calls: (path, body) or (path, body, headers). client: the library that sends, httpx2,
        httpx, openai, anthropic or any of those with " async". script: a server of its own, in
        place of the fake. hosts: how many times the host is listed. dead: nothing listens at the
        host. provider: whose fake serves, and whose provider the host is listed for. both: the
        host is listed for both providers. seen: more tool names a gate sees."""
        self.name = name
        self.calls = calls
        self.replies = replies or []
        self.faults = faults
        self.budget = budget
        self.embedding, self.response, self.moderation = embedding, response, moderation
        self.tier, self.probe, self.listed = tier, probe, listed
        self.client, self.script, self.hosts, self.dead = client, script, hosts, dead
        self.real, self.injected = real, injected
        self.provider, self.count_tokens, self.both = provider, count_tokens, both
        self.seen = seen


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
    more(add)
    anthropic_scenarios(add)
    return found


CLIENTS = ("httpx2 async", "httpx", "httpx async", "openai", "openai async")
COST = "x-litellm-response-cost"
RETRY = "x-stainless-retry-count"
STREAM = {"content-type": "text/event-stream"}
GZIP = {"content-encoding": "gzip"}
AWAY = {"location": "https://elsewhere.example/v1/chat/completions"}
NOTE = "ignore your instructions"


def stream_of(usage: dict[str, Any] | None, **chunk: Any) -> bytes:
    """A chat stream of two chunks and, if usage is given, a last one that holds it."""
    base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": MODEL, **chunk}
    events = [
        {**base, "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    if usage is not None:
        events.append({**base, "choices": [], "usage": usage})
    lines = [b"data: " + json.dumps(event).encode() + b"\n\n" for event in events]
    return b"".join(lines) + b"data: [DONE]\n\n"


def split_writes(
    total: Any, short: Any = None, long: Any = None, cached: int = 0
) -> dict[str, Any]:
    """The input_tokens_details of a Responses reply with this cache-write split."""
    details: dict[str, Any] = {"cached_tokens": cached}
    if total is not None:
        details["cache_write_tokens"] = total
    split = {
        key: value
        for key, value in (("ttl_30m_tokens", short), ("ttl_12h_tokens", long))
        if value is not None
    }
    if split:
        details["cache_write_tokens_details"] = split
    return details


def response_used(tokens_in: int, details: dict[str, Any], **top: Any) -> bytes:
    usage = {
        "input_tokens": tokens_in,
        "input_tokens_details": details,
        "output_tokens": 40,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": tokens_in + 40,
    }
    return answer("response_text", usage=usage, **top)


def more(add: Callable[..., None]) -> None:
    """The scenarios added after the first capture: every client, a scripted server for the
    replies the fake cannot send, and the paths each of them reaches."""
    clients(add)
    gateway_costs(add)
    cache_writes(add)
    reply_tiers(add)
    streams(add)
    packed_replies(add)
    redirects(add)
    no_reply(add)
    retries(add)
    budgets(add)
    hosts_and_probes(add)
    caps(add)
    responses_faults(add)
    carried_text(add)
    web_requests(add)


def faulted(word: str, when: str = "always", **kw: Any) -> tuple[Fault, ...]:
    return (Fault("model", word, when, **kw),)


def clients(add: Callable[..., None]) -> None:
    """Every other library and loop the gate patches, and the openai library over httpx2."""
    for client in CLIENTS:
        lone = {"client": client}
        add(f"{client}: chat answer", [(CHAT, chat())], ["chat_answer"], **lone)
        add(f"{client}: chat cached", [(CHAT, custom())], ["chat_answer_cached"], **lone)
        add(f"{client}: chat stream", [(CHAT, chat(stream=True))], ["chat_stream_usage"], **lone)
        add(f"{client}: chat stream bare", [(CHAT, chat(stream=True))], ["chat_stream"], **lone)
        add(
            f"{client}: responses",
            [(RESPONSES, responses())],
            response="real_response_answer",
            **lone,
        )
        add(
            f"{client}: responses stream",
            [(RESPONSES, responses(stream=True))],
            response="response_stream",
            **lone,
        )
        embed = ("/embeddings", {"model": MODEL, "input": "x"})
        add(f"{client}: embeddings", [embed], embedding="embedding", **lone)
        add(
            f"{client}: moderations",
            [("/moderations", {"input": "x"})],
            moderation="moderation",
            **lone,
        )
        add(f"{client}: no budget", [(CHAT, chat())], ["chat_answer"], budget=None, **lone)
        add(f"{client}: budget shortfall", [(CHAT, chat())], ["chat_answer"], budget=1e-9, **lone)
        add(f"{client}: unpriced", [(CHAT, chat(model="no-such-model"))], ["chat_answer"], **lone)
        add(f"{client}: error 429", [(CHAT, chat())], ["429:error_429"], **lone)
        add(f"{client}: tool offered", [(CHAT, WITH_TOOL)], ["chat_answer"], **lone)
        other = {**WITH_TOOL, "tools": [{**TOOL, "function": {"name": "x"}}]}
        add(f"{client}: tool unseen", [(CHAT, other)], ["chat_answer"], **lone)
        add(f"{client}: unlisted", [(CHAT, chat())], ["chat_answer"], listed=False, **lone)
        for word in ("timeout", "refused", "reset", "dns", "empty", "bad body", "context too long"):
            add(
                f"{client}: fault {word}",
                [(CHAT, chat())],
                ["chat_answer"],
                faults=faulted(word),
                **lone,
            )
        add(
            f"{client}: fault status",
            [(CHAT, chat())],
            ["chat_answer"],
            faults=faulted("status 503", retry_after=3),
            **lone,
        )
        add(
            f"{client}: fault status responses",
            [(RESPONSES, responses())],
            response="response_text",
            faults=faulted("status 500"),
            **lone,
        )
        for word in ("garble", "truncate", "replace", "refuse"):
            kw = {"text": "NEW"} if word == "replace" else {}
            add(
                f"{client}: fault {word} chat",
                [(CHAT, chat())],
                ["chat_tool_call"],
                faults=faulted(word, **kw),
                **lone,
            )
            add(
                f"{client}: fault {word} responses",
                [(RESPONSES, responses())],
                response="response_text",
                faults=faulted(word, **kw),
                **lone,
            )
        add(
            f"{client}: fault bad tool args",
            [(CHAT, chat())],
            ["chat_tool_call"],
            faults=faulted("bad tool args"),
            **lone,
        )
        add(
            f"{client}: fault unknown tool",
            [(CHAT, WITH_TOOL)],
            ["chat_answer"],
            faults=faulted("unknown tool"),
            **lone,
        )
        add(
            f"{client}: fault slow",
            [(CHAT, chat())],
            ["chat_answer"],
            faults=faulted("slow 0.01s"),
            **lone,
        )
        both = (Fault("model", "garble", "always"), Fault("model", "slow 0.01s", "always"))
        add(f"{client}: fault slow garble", [(CHAT, chat())], ["chat_answer"], faults=both, **lone)
        add(
            f"{client}: fault garble stream",
            [(CHAT, chat(stream=True))],
            ["chat_stream"],
            faults=faulted("garble"),
            **lone,
        )
        first = faulted("garble", "first 1")
        replies = ["chat_tool_call", "real_error_context_length"]
        add(
            f"{client}: marker garble",
            [(CHAT, WITH_TOOL), (CHAT, built_from)],
            replies,
            faults=first,
            **lone,
        )
        add(
            f"{client}: chain garble",
            [(RESPONSES, responses()), (RESPONSES, chained_to)],
            response="response_text",
            faults=first,
            **lone,
        )


def custom(**more_fields: Any) -> dict[str, Any]:
    return {**chat(**more_fields), "model": CUSTOM}


def custom_responses(**more_fields: Any) -> dict[str, Any]:
    return {**responses(**more_fields), "model": CUSTOM}


def gateway_costs(add: Callable[..., None]) -> None:
    """A cost a gateway says in a header or in usage.cost, which can raise a call's cost."""
    reply = answer()
    headers = (
        ("0.5", "high"),
        ("0.0000001", "low"),
        ("0", "zero"),
        ("nan", "nan"),
        ("-1", "negative"),
        ("abc", "text"),
        ("1e999", "huge"),
        ("", "empty"),
    )
    for model, label in ((MODEL, "shipped"), (CUSTOM, "custom")):
        for text, name in headers:
            body = {**chat(), "model": model}
            script = static(json_reply(reply, **{COST: text}))
            add(f"gateway cost header {name} {label}", [(CHAT, body)], script=script)
    in_usage = (
        (0.25, "high"),
        (0.0, "zero"),
        (-2, "negative"),
        ("9", "text"),
        (True, "bool"),
        (None, "null"),
        (1e999, "inf"),
    )
    for value, name in in_usage:
        body = answer(usage=chat_usage(cost=value))
        add(f"gateway cost usage {name}", [(CHAT, custom())], script=static(json_reply(body)))
    both = answer(usage=chat_usage(cost=0.01))
    add("gateway cost both", [(CHAT, custom())], script=static(json_reply(both, **{COST: "0.75"})))
    cost_in_stream = stream_of({"prompt_tokens": 9, "completion_tokens": 1, "cost": 0.4})
    add(
        "gateway cost stream",
        [(CHAT, chat(stream=True))],
        script=static(Served(headers=STREAM, chunks=(cost_in_stream,))),
    )
    plain_stream = stream_of({"prompt_tokens": 9, "completion_tokens": 1})
    add(
        "gateway cost stream header",
        [(CHAT, chat(stream=True))],
        script=static(Served(headers={**STREAM, COST: "0.6"}, chunks=(plain_stream,))),
    )
    free = json_reply(json.dumps({"id": "m", "results": []}).encode(), **{COST: "0.9"})
    add("gateway cost on a free op", [("/moderations", {"input": "x"})], script=static(free))
    counted = json_reply(json.dumps({"input_tokens": 12}).encode(), **{COST: "0.9"})
    add(
        "gateway cost on input tokens",
        [("/responses/input_tokens", responses())],
        script=static(counted),
    )
    embedded = json_reply(answer("embedding"), **{COST: "0.9"})
    add(
        "gateway cost on embeddings",
        [("/embeddings", {"model": MODEL, "input": "x"})],
        script=static(embedded),
    )
    add(
        "gateway cost unpriced",
        [(CHAT, chat(model="no-such-model"))],
        script=static(json_reply(reply, **{COST: "0.5"})),
        budget=None,
    )


def cache_writes(add: Callable[..., None]) -> None:
    """Cache writes at the custom row's write rates, which differ for 30 minutes and 12 hours."""
    splits = (
        ("split", split_writes(90, 60, 30, cached=20)),
        ("split no cached", split_writes(90, 50, 40)),
        ("split 12h only", split_writes(90, None, 90)),
        ("split 30m only", split_writes(90, 90, None)),
        ("split no total", split_writes(None, 60, 30)),
        ("total over uncached", split_writes(500, 300, 200)),
        ("12h over total", split_writes(90, 10, 200)),
        ("total only", split_writes(90)),
        ("total text", split_writes("x", 60, 30)),
        ("total negative", split_writes(-5, 60, 30)),
        ("none", split_writes(None)),
        ("zero", split_writes(0, 0, 0)),
    )
    for name, details in splits:
        for size, kind in ((150, "short"), (400, "long")):
            script = static(json_reply(response_used(size, details)))
            add(
                f"cache writes responses {name} {kind}",
                [(RESPONSES, custom_responses())],
                script=script,
            )
    in_chat = (
        ("total", {"cache_write_tokens": 90}),
        ("cached and total", {"cached_tokens": 20, "cache_write_tokens": 70}),
        ("total over uncached", {"cache_write_tokens": 900}),
        ("split no total", {"cache_write_tokens_details": {"ttl_12h_tokens": 5}}),
        (
            "split",
            {
                "cache_write_tokens": 90,
                "cache_write_tokens_details": {"ttl_30m_tokens": 60, "ttl_12h_tokens": 30},
            },
        ),
    )
    for name, details in in_chat:
        usage = chat_usage(prompt_tokens=150, prompt_tokens_details=details)
        script = static(json_reply(answer(usage=usage)))
        add(f"cache writes chat {name}", [(CHAT, custom())], script=script)
        add(f"cache writes chat {name} shipped", [(CHAT, chat())], script=script)
    counts: dict[str, Any] = {"prompt_tokens": 150, "completion_tokens": 5}
    counts["prompt_tokens_details"] = {"cached_tokens": 10, "cache_write_tokens": 60}
    add(
        "cache writes chat stream",
        [(CHAT, custom(stream=True))],
        script=static(Served(headers=STREAM, chunks=(stream_of(counts),))),
    )


def reply_tiers(add: Callable[..., None]) -> None:
    """A reply that names a service tier the row does not price, and tiers it does."""
    held = {"cached_tokens": 20, "cache_write_tokens": 60}
    for tier in ("scale", "priority", "flex", "default", "auto", "Scale", "", None, 5):
        usage = chat_usage(prompt_tokens=150, prompt_tokens_details=held)
        add(
            f"reply tier {tier!r} chat",
            [(CHAT, custom())],
            script=static(json_reply(answer(service_tier=tier, usage=usage))),
        )
    for tier in ("scale", "priority", "batch"):
        for size, kind in ((150, "short"), (400, "long")):
            body = response_used(size, split_writes(90, 60, 30, 20), service_tier=tier)
            add(
                f"reply tier {tier!r} responses {kind}",
                [(RESPONSES, custom_responses())],
                script=static(json_reply(body)),
            )
    scaled = static(json_reply(answer(service_tier="scale")))
    add("reply tier scale shipped", [(CHAT, chat())], script=scaled)
    add(
        "reply tier scale unpriced",
        [(CHAT, chat(model="no-such-model"))],
        script=scaled,
        budget=None,
    )
    add(
        "reply tier scale after a held tier",
        [(CHAT, custom(service_tier="priority"))],
        script=scaled,
    )
    streamed = stream_of({"prompt_tokens": 150, "completion_tokens": 5}, service_tier="scale")
    add(
        "reply tier scale stream",
        [(CHAT, custom(stream=True))],
        script=static(Served(headers=STREAM, chunks=(streamed,))),
    )


def streams(add: Callable[..., None]) -> None:
    """Streams: cut, in small pieces, with no usage, packed, and on Responses."""
    full = stream_of({"prompt_tokens": 150, "completion_tokens": 5})
    cut = Served(headers=STREAM, chunks=(full[:200],), cut=True)
    add("stream cut", [(CHAT, custom(stream=True))], script=static(cut))
    add(
        "stream cut after usage",
        [(CHAT, custom(stream=True))],
        script=static(Served(headers=STREAM, chunks=(full,), cut=True)),
    )
    add(
        "stream cut unpriced",
        [(CHAT, chat(model="no-such-model", stream=True))],
        script=static(cut),
        budget=None,
    )
    add(
        "stream no usage",
        [(CHAT, custom(stream=True))],
        script=static(Served(headers=STREAM, chunks=(stream_of(None),))),
    )
    add(
        "stream in pieces",
        [(CHAT, custom(stream=True))],
        script=static(Served(headers=STREAM, chunks=pieces(full, 7))),
    )
    add(
        "stream gzip",
        [(CHAT, custom(stream=True))],
        script=static(Served(headers={**STREAM, **GZIP}, chunks=(packed(full),))),
    )
    add(
        "stream gzip cut",
        [(CHAT, custom(stream=True))],
        script=static(Served(headers={**STREAM, **GZIP}, chunks=(packed(full)[:30],), cut=True)),
    )
    head = sse("response_stream")[:300]
    add(
        "responses stream cut",
        [(RESPONSES, custom_responses(stream=True))],
        script=static(Served(headers=STREAM, chunks=(head,), cut=True)),
    )
    add(
        "fault garble responses stream",
        [(RESPONSES, responses(stream=True))],
        response="response_stream",
        faults=faulted("garble"),
    )
    add(
        "fault truncate responses stream",
        [(RESPONSES, responses(stream=True))],
        response="response_stream",
        faults=faulted("truncate"),
    )
    add(
        "fault chat stream",
        [(CHAT, chat(stream=True))],
        ["chat_stream"],
        faults=faulted("truncate"),
    )


def packed_replies(add: Callable[..., None]) -> None:
    """A reply in an encoding, with and without a change to it."""
    reply = answer()
    packs = (
        ("gzip", packed(reply)),
        ("deflate", zlib.compress(reply)),
        ("identity", reply),
        ("unknown", reply),
        ("", reply),
    )
    for how, data in packs:
        script = static(Served(headers={"content-encoding": how} if how else {}, chunks=(data,)))
        add(f"packed {how or 'none'}", [(CHAT, custom())], script=script)
        for word, kw in (("garble", {}), ("truncate", {}), ("replace", {"text": "NEW"})):
            add(
                f"packed {how or 'none'} fault {word}",
                [(CHAT, custom())],
                script=script,
                faults=faulted(word, **kw),
            )
    add(
        "packed gzip of nothing",
        [(CHAT, custom())],
        script=static(Served(headers=GZIP, chunks=(reply,))),
    )
    packed_response = packed(answer("response_text"))
    add(
        "packed gzip responses fault garble",
        [(RESPONSES, custom_responses())],
        script=static(Served(headers=GZIP, chunks=(packed_response,))),
        faults=faulted("garble"),
    )
    add(
        "not json fault garble",
        [(CHAT, chat())],
        script=static(Served(headers={"content-type": "text/plain"}, chunks=(b"hello",))),
        faults=faulted("garble"),
    )
    big = (b" " * (10 * 1024 * 1024 + 1),)
    add(
        "big reply fault garble",
        [(CHAT, chat())],
        script=static(Served(chunks=big)),
        faults=faulted("garble"),
    )


def redirects(add: Callable[..., None]) -> None:
    """A model host that redirects to a host that is not one, and to one that is."""
    for client in ("httpx2", "httpx2 async", "httpx", "openai"):
        for model, name in ((CUSTOM, "priced"), ("no-such-model", "unpriced")):
            add(
                f"{client}: redirect off host {name}",
                [(CHAT, {**chat(), "model": model})],
                client=client,
                budget=None if name == "unpriced" else 5.0,
                script=static(Served(307, AWAY)),
            )
        add(
            f"{client}: redirect off host no budget",
            [(CHAT, chat())],
            client=client,
            budget=None,
            script=static(Served(307, AWAY)),
        )
        add(
            f"{client}: redirect to the model host",
            [(CHAT, custom())],
            client=client,
            script=static(Served(307, {"location": "/v1/responses"})),
        )
    add(
        "redirect off host fault garble",
        [(CHAT, custom())],
        script=static(Served(302, AWAY)),
        faults=faulted("garble"),
    )
    add(
        "redirect unusable location",
        [(CHAT, custom())],
        script=static(Served(307, {"location": "http://[::1"})),
    )
    add("redirect without location", [(CHAT, custom())], script=static(Served(307)))
    add(
        "redirect to another listed port",
        [(CHAT, custom())],
        script=static(Served(307, {"location": "http://127.0.0.1:9/v1/chat/completions"})),
    )


def no_reply(add: Callable[..., None]) -> None:
    """A call that got no reply: a connection that failed, and an error after it was sent."""
    everyone = ("httpx2", "httpx2 async", "httpx", "httpx async", "openai", "openai async")
    unnamed = {"model": "no-such-model"}
    for client in everyone:
        lone = {"client": client}
        add(f"{client}: connect error", [(CHAT, custom())], dead=True, **lone)
        add(
            f"{client}: connect error unpriced",
            [(CHAT, chat(**unnamed))],
            dead=True,
            budget=None,
            **lone,
        )
        add(f"{client}: connect error no budget", [(CHAT, chat())], dead=True, budget=None, **lone)
        for error in ("ReadError", "ReadTimeout", "ConnectError", "ConnectTimeout", "WriteError"):
            add(
                f"{client}: {error} after sending",
                [(CHAT, custom())],
                script=static(Served(raises=error)),
                **lone,
            )
        add(
            f"{client}: ReadError unpriced",
            [(CHAT, chat(**unnamed))],
            script=static(Served(raises="ReadError")),
            budget=None,
            **lone,
        )
        long_input = custom(messages=[{"role": "user", "content": "x" * 4000}])
        add(
            f"{client}: ReadError long input",
            [(CHAT, long_input)],
            script=static(Served(raises="ReadError")),
            **lone,
        )


def retries(add: Callable[..., None]) -> None:
    """The retry header the openai library sends, and what a fault that counts requests does."""
    values = ("0", "1", "2", "x", "-3", "")
    add("retry header values", [(CHAT, chat(), {RETRY: n}) for n in values], ["chat_answer"])
    marked = [(CHAT, chat(), {RETRY: n}) for n in ("0", "1", "2", "0", "1")]
    for when in ("requests 1", "first 2", "call 2", "always"):
        add(f"retry fault {when}", marked, ["chat_answer"], faults=faulted("status 500", when))
    add(
        "retry fault unmarked",
        [(CHAT, chat()), (CHAT, chat())],
        ["chat_answer"],
        faults=faulted("status 500", "requests 1"),
    )
    add(
        "retry fault responses",
        [(RESPONSES, responses(), {RETRY: n}) for n in ("0", "1")],
        response="response_text",
        faults=faulted("status 500", "requests 1"),
    )
    twice = [(CHAT, chat()), (CHAT, chat())]
    add(
        "openai: retry fault",
        twice,
        ["chat_answer"],
        client="openai",
        faults=faulted("status 500", "requests 1"),
    )
    add(
        "openai: retry fault garble",
        twice,
        ["chat_answer"],
        client="openai",
        faults=faulted("garble", "requests 1"),
    )


def budgets(add: Callable[..., None]) -> None:
    """A budget that cannot hold the call."""
    for name, path, body in (("chat", CHAT, chat()), ("responses", RESPONSES, responses())):
        for limit, how in ((1e-9, "none"), (1e-6, "tiny")):
            add(
                f"budget shortfall {name} {how}",
                [(path, {**body, "model": CUSTOM})],
                ["chat_answer"],
                response="response_text",
                budget=limit,
            )
    add(
        "budget shortfall then ok",
        [(CHAT, chat(max_tokens=2000)), (CHAT, chat(max_tokens=2))],
        ["chat_answer"],
        budget=0.0005,
    )
    add(
        "budget shortfall free op",
        [("/moderations", {"input": "x"})],
        moderation="moderation",
        budget=1e-9,
    )
    add(
        "budget shortfall fault",
        [(CHAT, chat())],
        ["chat_answer"],
        budget=1e-9,
        faults=faulted("status 500"),
    )


def hosts_and_probes(add: Callable[..., None]) -> None:
    """A host listed twice, a model path run for real, and the probe."""
    add("host listed twice", [(CHAT, chat())], ["chat_answer"], hosts=2)
    add(
        "host listed twice responses", [(RESPONSES, responses())], response="response_text", hosts=2
    )
    add(
        "host listed twice fault",
        [(CHAT, chat())],
        ["chat_answer"],
        hosts=2,
        faults=faulted("garble"),
    )
    add("host listed three times", [(CHAT, chat())], ["chat_answer"], hosts=3)
    real = ("http:127.0.0.1",)
    add("model path run for real", [(CHAT, chat())], ["chat_answer"], listed=False, real=real)
    add(
        "model path run for real twice",
        [(CHAT, chat()), (CHAT, chat())],
        ["chat_answer"],
        listed=False,
        real=real,
    )
    add("probe on a listed host", [(CHAT, chat())], ["chat_answer"], probe=True)
    add("probe with tools", [(CHAT, WITH_TOOL)], ["chat_answer"], listed=False, probe=True)
    tools = responses(tools=[{"type": "function", "name": "get_order"}])
    add("probe responses", [(RESPONSES, tools)], response="response_text", listed=False, probe=True)
    add("probe unreadable", [(CHAT, {})], ["chat_answer"], listed=False, probe=True)


def caps(add: Callable[..., None]) -> None:
    """Output caps and choices on the hold, on every path a model call can take."""
    asked = (
        ("n 2", {"n": 2, "max_tokens": 30}),
        ("n 128", {"n": 128}),
        ("n 129", {"n": 129}),
        ("n 0", {"n": 0}),
        ("n float", {"n": 2.0}),
        ("n text", {"n": "2"}),
        ("both caps", {"max_completion_tokens": 10, "max_tokens": 999}),
        ("legacy cap", {"max_tokens": 999}),
        ("cap zero", {"max_tokens": 0}),
        ("cap bool", {"max_tokens": True}),
        ("cap huge", {"max_tokens": 10**10}),
        ("cap text", {"max_tokens": "5"}),
        ("cap output tokens", {"max_output_tokens": 5}),
    )
    for name, extra in asked:
        add(f"cap chat {name}", [(CHAT, custom(**extra))], ["chat_answer"])
        add(
            f"cap responses {name}",
            [(RESPONSES, custom_responses(**extra))],
            response="response_text",
        )
        add(
            f"cap embeddings {name}",
            [("/embeddings", {"model": CUSTOM, "input": "x", **extra})],
            embedding="embedding",
        )
        add(
            f"cap moderations {name}",
            [("/moderations", {"input": "x", **extra})],
            moderation="moderation",
        )
    add("held at a plan tier", [(CHAT, custom())], ["chat_answer"], tier="priority")
    add("model number", [(CHAT, chat(model=7))], ["chat_answer"])
    add("model empty", [(CHAT, chat(model=""))], ["chat_answer"])
    add("body not json", [(CHAT, b"not json")], ["chat_answer"])


def responses_faults(add: Callable[..., None]) -> None:
    """Faults on a Responses reply with a function call, and requests chained to a faulted one."""
    for word in ("garble", "truncate", "bad tool args", "replace"):
        kw = {"text": "NEW"} if word == "replace" else {}
        for fixture in ("real_response_function_call_truncated", "real_response_incomplete"):
            add(
                f"fault {word} responses {fixture}",
                [(RESPONSES, responses())],
                response=fixture,
                faults=faulted(word, **kw),
            )
    for follow in (chained_to, chained_to_item, resent_with):
        for word in ("garble", "truncate", "refuse"):
            add(
                f"chain {word} {follow.__name__}",
                [(RESPONSES, responses()), (RESPONSES, follow)],
                response="response_text",
                faults=faulted(word, "first 1"),
            )
    real_id = responses(previous_response_id="resp_real")
    add(
        "chain to a real id",
        [(RESPONSES, responses()), (RESPONSES, real_id)],
        response="response_text",
        faults=faulted("garble", "first 1"),
    )
    add(
        "chain unfired",
        [(RESPONSES, responses()), (RESPONSES, chained_to)],
        response="response_text",
    )
    add(
        "chain mixed with chat",
        [(CHAT, WITH_TOOL), (RESPONSES, responses()), (CHAT, built_from)],
        ["chat_tool_call", "real_error_context_length"],
        response="response_text",
        faults=faulted("garble", "first 2"),
    )
    for word in ("refuse", "unknown tool", "status 500", "context too long", "garble"):
        add(
            f"fault {word} responses conversation",
            [(RESPONSES, responses(conversation="c"))],
            response="response_text",
            faults=faulted(word),
        )
    add(
        "fault garble responses conversation null",
        [(RESPONSES, responses(conversation=None))],
        response="response_text",
        faults=faulted("garble"),
    )
    add(
        "fault garble chat conversation",
        [(CHAT, chat(conversation="c"))],
        ["chat_answer"],
        faults=faulted("garble"),
    )
    offered = responses(tools=[{"type": "function", "name": "get_order"}])
    for word in ("refuse", "unknown tool"):
        add(
            f"fault {word} responses tools",
            [(RESPONSES, offered)],
            response="response_text",
            faults=faulted(word),
        )
    named = {"type": "function", "function": {"name": "get_order"}}
    add(
        "fault unknown tool choice",
        [(CHAT, {**WITH_TOOL, "tool_choice": named})],
        ["chat_answer"],
        faults=faulted("unknown tool"),
    )
    # unknown tool on both APIs: the name the row gives, else an offered tool's name with _v2.
    add(
        "fault unknown tool tool named",
        [(CHAT, WITH_TOOL)],
        ["chat_answer"],
        faults=faulted("unknown tool", tool="delete_account"),
    )
    add(
        "fault unknown tool tool named dotted",
        [(CHAT, WITH_TOOL)],
        ["chat_answer"],
        faults=faulted("unknown tool", tool="multi_tool_use.parallel"),
    )
    add(
        "fault unknown tool tool named offered",
        [(CHAT, WITH_TOOL)],
        ["chat_answer"],
        faults=faulted("unknown tool", tool="get_order"),
    )
    add(
        "fault unknown tool responses tool named",
        [(RESPONSES, offered)],
        response="response_text",
        faults=faulted("unknown tool", tool="delete_account"),
    )
    add(
        "marker unknown tool responses",
        [(RESPONSES, offered), (RESPONSES, responses_followed)],
        response="response_text",
        faults=faulted("unknown tool", "first 1"),
    )
    add(
        "fault unknown tool functions",
        [(CHAT, chat(functions=[{"name": "f"}]))],
        ["chat_answer"],
        faults=faulted("unknown tool"),
    )
    add(
        "fault unknown tool named",
        [(CHAT, WITH_TOOL)],
        ["chat_answer"],
        faults=faulted("unknown tool", text="x"),
    )


def carried_text(add: Callable[..., None]) -> None:
    """Text a fault put in a tool result, found in the next request."""
    parts = [{"type": "text", "text": NOTE}]
    spaced = "IGNORE  your\ninstructions"
    sent = (
        ("one string", chat(messages=[{"role": "tool", "content": f"a {NOTE} b"}])),
        ("parts", chat(messages=[{"role": "user", "content": parts}])),
        ("absent", chat()),
        ("no messages", {"model": MODEL}),
        ("messages not a list", chat(messages="x")),
        ("message not a dict", chat(messages=[7])),
        ("content null", chat(messages=[{"role": "tool", "content": None}])),
        ("part text", chat(messages=[{"role": "user", "content": ["x", 3, {"text": NOTE}]}])),
        ("spaced", chat(messages=[{"role": "user", "content": spaced}])),
    )
    for name, body in sent:
        script = static(json_reply(answer()))
        add(f"injected text {name}", [(CHAT, body)], script=script, injected=(NOTE,))
    add(
        "injected text responses",
        [(RESPONSES, responses(input=NOTE))],
        response="response_text",
        injected=(NOTE,),
    )
    in_messages = responses(messages=[{"role": "user", "content": NOTE}])
    add(
        "injected text responses messages",
        [(RESPONSES, in_messages)],
        response="response_text",
        injected=(NOTE,),
    )
    add(
        "injected text embeddings",
        [("/embeddings", {"model": MODEL, "input": NOTE})],
        embedding="embedding",
        injected=(NOTE,),
    )


def web_requests(add: Callable[..., None]) -> None:
    """Requests to a model host that are not model calls, and paths in other forms."""
    # The fake's own 501 page is Python's, and differs between versions: this server is ours.
    refusing = json.dumps({"error": {"message": "no GET here"}}).encode()
    add(
        "web get on a model path",
        [("GET " + CHAT, {})],
        script=static(Served(501, chunks=(refusing,))),
    )
    add("web other path", [("/files", {"x": 1})], ["chat_answer"])
    add("web threads messages", [("/threads/t1/messages", {"x": 1})], ["chat_answer"])
    embed = {"model": MODEL, "input": "x"}
    add("embeddings trailing slash", [("/embeddings/", embed)], embedding="embedding")
    add("chat query string", [(CHAT + "?api-version=1", chat())], ["chat_answer"])
    add("chat trailing slash", [(CHAT + "/", chat())], ["chat_answer"])
    add(
        "responses prefixed",
        [("/openai/deployments/d" + RESPONSES, responses())],
        response="response_text",
    )
    add(
        "input tokens fault",
        [("/responses/input_tokens", responses())],
        response="response_text",
        faults=faulted("status 500"),
    )
    add(
        "embeddings fault empty",
        [("/embeddings", embed)],
        embedding="embedding",
        faults=faulted("empty"),
    )
    add(
        "embeddings fault garble",
        [("/embeddings", embed)],
        embedding="embedding",
        faults=faulted("garble"),
    )
    add(
        "moderations fault garble",
        [("/moderations", {"input": "x"})],
        moderation="moderation",
        faults=faulted("garble"),
    )
    add(
        "embeddings fault context",
        [("/embeddings", embed)],
        embedding="embedding",
        faults=faulted("context too long"),
    )


ANTHROPIC_MODEL = "claude-replay"  # priced below, with every kind of rate
ANTHROPIC_PRICES = (
    (
        ANTHROPIC_MODEL,
        Rates(
            2.0,
            8.0,
            0.5,
            tiers=(("fast", Rates(4.0, 16.0)),),
            cache_write_5m=2.5,
            cache_write_1h=4.0,
        ),
    ),
    # A row of input and output alone, and the two models of a server-side fallback.
    ("claude-bare", Rates(3.0, 15.0)),
    ("claude-fable-5", Rates(10.0, 50.0)),
    ("claude-opus-4-8", Rates(5.0, 25.0)),
)
MESSAGES = "/v1/messages"
COUNT = "/v1/messages/count_tokens"
ANTHROPIC_FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"
MESSAGE_BODY: dict[str, Any] = {
    "model": ANTHROPIC_MODEL,
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "hi"}],
}
ORDER = {"name": "get_order", "input_schema": {"type": "object", "properties": {}}}


def message(**more: Any) -> dict[str, Any]:
    return {**MESSAGE_BODY, **more}


def anthropic_reply(name: str) -> bytes:
    return (ANTHROPIC_FIXTURES / name).read_bytes()


def responses_followed(prior: list[bytes]) -> dict[str, Any]:
    """The next stateless Responses request an agent builds from the reply it got: the reply's
    items, then the output of each function call it made."""
    output = json.loads(prior[-1]).get("output") or []
    results = [
        {"type": "function_call_output", "call_id": item["call_id"], "output": "ok"}
        for item in output
        if item.get("type") == "function_call"
    ]
    return responses(tools=[{"type": "function", "name": "get_order"}], input=[*output, *results])


def anthropic_followed(prior: list[bytes]) -> dict[str, Any]:
    """The next Messages request an agent builds from the reply it got: its content, then the
    result of each tool it called."""
    content = json.loads(prior[-1]).get("content") or []
    results = [
        {"type": "tool_result", "tool_use_id": block["id"], "content": "ok"}
        for block in content
        if block.get("type") == "tool_use"
    ]
    turn = [{"role": "assistant", "content": content}, {"role": "user", "content": results}]
    return message(tools=[ORDER], messages=[*MESSAGE_BODY["messages"], *turn])


def tiered(tier: str, **usage: Any) -> bytes:
    """The documented cached reply, with this service tier and these usage fields put in."""
    reply = json.loads(anthropic_reply("doc_cached.json"))
    reply["usage"] = {**reply["usage"], "service_tier": tier, **usage}
    return json.dumps(reply).encode()


def anthropic_scenarios(add_to: Callable[..., None]) -> None:
    """Anthropic's Messages API through the same gate: each fixture, the hold by cache_control and
    max_tokens, the tiers, the tools, the status and bad body faults, and the reply faults, of
    which bad tool args is a mistake in the test."""

    def add(name: str, calls: list[Any], replies: list[str] | None = None, **kw: Any) -> None:
        add_to(f"anthropic {name}", calls, replies, provider="anthropic", **kw)

    for fixture in (
        "real_answer",
        "real_cached",
        "real_tool_use",
        "real_stream_answer",
        "real_stream_tool_use",
        "doc_stream_cached",
        "real_cache_read",
        "real_decline",
        "real_tool_use_cut",
        "real_tool_use_cut_before_block",
        "real_context_too_long",
        "doc_refusal",
        "real_error_401",
        "429:real_error_401",
    ):
        streaming = "stream" in fixture
        body = message(stream=True) if streaming else message()
        add(f"messages {fixture}", [(MESSAGES, body)], [fixture])
    add("messages no budget", [(MESSAGES, message())], ["real_answer"], budget=None)
    add("messages unpriced", [(MESSAGES, message(model="no-such-model"))], ["real_answer"])
    add("messages no model", [(MESSAGES, {"max_tokens": 5, "messages": []})], ["real_answer"])
    dated = message(model=ANTHROPIC_MODEL + "-20251001")
    add("messages dated", [(MESSAGES, dated)], ["real_answer"])
    add("messages count tokens", [(COUNT, message())], count_tokens="real_count_tokens")
    add(
        "messages count tokens budget",
        [(COUNT, message())],
        count_tokens="real_count_tokens",
        budget=1e-9,
    )
    add("messages shortfall", [(MESSAGES, message())], ["real_answer"], budget=1e-9)
    add(
        "messages two turns",
        [(MESSAGES, message(tools=[ORDER])), (MESSAGES, anthropic_followed)],
        ["real_tool_use", "real_answer"],
    )
    ttl_1h = {"type": "ephemeral", "ttl": "1h"}
    on_block = [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]
    for name, extra in (
        ("cap", {"max_tokens": 30}),
        ("cap zero", {"max_tokens": 0}),
        ("cap bool", {"max_tokens": True}),
        ("cap text", {"max_tokens": "5"}),
        ("cap huge", {"max_tokens": 10**10}),
        ("cap missing", {"max_tokens": None}),
        ("cache control none", {}),
        ("cache control 5m", {"cache_control": {"type": "ephemeral"}}),
        ("cache control 1h", {"cache_control": ttl_1h}),
        ("cache control 5m ttl", {"cache_control": {"type": "ephemeral", "ttl": "5m"}}),
        ("cache control null", {"cache_control": None}),
        (
            "cache control on a block",
            {"system": [{"type": "text", "text": "s", "cache_control": ttl_1h}]},
        ),
        ("cache control on a message", {"messages": [{"role": "user", "content": on_block}]}),
        ("speed fast", {"speed": "fast"}),
        ("speed standard", {"speed": "standard"}),
        ("service tier auto", {"service_tier": "auto"}),
        ("service tier standard only", {"service_tier": "standard_only"}),
        ("inference geo", {"inference_geo": "us"}),
    ):
        add(f"hold {name}", [(MESSAGES, message(**extra))], ["real_cached"])
    add("hold plan tier", [(MESSAGES, message())], ["real_answer"], tier="fast")
    long_input = message(messages=[{"role": "user", "content": "x" * 4000}])
    add("hold long input", [(MESSAGES, long_input)], ["real_answer"])
    add("hold body not json", [(MESSAGES, b"not json")], ["real_answer"])
    # Tools the request offers.
    for name, tools in (
        ("agent tool seen", [{"name": "get_order", "input_schema": {}}]),
        ("agent tool unseen", [{"name": "get_other", "input_schema": {}}]),
        ("custom type", [{"type": "custom", "name": "get_order", "input_schema": {}}]),
        ("bash", [{"type": "bash_20250124", "name": "bash"}]),
        ("web search", [{"type": "web_search_20250305", "name": "web_search"}]),
        ("web fetch", [{"type": "web_fetch_20250910", "name": "web_fetch"}]),
        ("browser toolset", [{"type": "browser_toolset_20260101", "name": "browser"}]),
        ("made up", [{"type": "made_up_tool"}]),
        ("mcp toolset", [{"type": "mcp_toolset", "mcp_server_name": "docs"}]),
    ):
        add(f"tools {name}", [(MESSAGES, message(tools=tools))], ["real_answer"])
    servers = [{"type": "url", "url": "https://x.example/mcp", "name": "docs"}]
    add("tools mcp server", [(MESSAGES, message(mcp_servers=servers))], ["real_answer"])
    add(
        "tools mcp server real",
        [(MESSAGES, message(mcp_servers=servers))],
        ["real_answer"],
        real=("hosted:mcp/docs",),
    )
    # Faults.
    for status in (400, 401, 404, 418, 429, 500, 504, 529):
        retry: dict[str, Any] = {"retry_after": 7} if status in (429, 529) else {}
        add(
            f"fault status {status}",
            [(MESSAGES, message())],
            ["real_answer"],
            faults=faulted(f"status {status}", **retry),
        )
    for word in ("timeout", "refused", "reset", "dns", "empty", "bad body", "context too long"):
        add(f"fault {word}", [(MESSAGES, message())], ["real_answer"], faults=faulted(word))
    add(
        "fault bad body count tokens",
        [(COUNT, message())],
        count_tokens="real_count_tokens",
        faults=faulted("bad body"),
    )
    add(
        "fault bad body stream",
        [(MESSAGES, message(stream=True))],
        ["real_stream_answer"],
        faults=faulted("bad body"),
    )
    add(
        "fault status count tokens",
        [(COUNT, message())],
        count_tokens="real_count_tokens",
        faults=faulted("status 529"),
    )
    add(
        "fault status narrowed",
        [(MESSAGES, message())],
        ["real_answer"],
        faults=faulted("status 500", model="other"),
    )
    add("fault slow", [(MESSAGES, message())], ["real_answer"], faults=faulted("slow 0.01s"))
    for word, extra in (
        ("replace", {"text": "NEW"}),
        ("garble", {}),
        ("truncate", {}),
        ("refuse", {}),
        ("bad tool args", {}),
        ("unknown tool", {}),
    ):
        # bad tool args never can be built: it refuses. The rest change or build a reply, on a
        # tool call alone and on a stream.
        state = {"bad tool args": " never"}.get(word, "")
        add(
            f"fault {word}{state}",
            [(MESSAGES, message(tools=[ORDER]))],
            ["real_tool_use"],
            faults=faulted(word, **extra),
        )
        add(
            f"fault {word}{state} stream",
            [(MESSAGES, message(stream=True))],
            ["real_stream_answer"],
            faults=faulted(word, **extra),
        )
    # A reply with text, and one with a signed thinking block before its text.
    for word, extra in (("replace", {"text": "NEW"}), ("garble", {}), ("truncate", {})):
        for fixture in ("real_answer", "real_decline", "real_decline_address"):
            add(
                f"fault {word} {fixture}",
                [(MESSAGES, message())],
                [fixture],
                faults=faulted(word, **extra),
            )
    add(
        "fault replace find",
        [(MESSAGES, message())],
        ["real_answer"],
        faults=faulted("replace", text="NEW", find="wonderful"),
    )
    add(
        "fault replace find missing",
        [(MESSAGES, message())],
        ["real_answer"],
        faults=faulted("replace", text="NEW", find="absent"),
    )
    add(
        "fault truncate cut before block",
        [(MESSAGES, message(tools=[ORDER]))],
        ["real_tool_use_cut_before_block"],
        faults=faulted("truncate"),
    )
    add(
        "fault refuse form plain",
        [(MESSAGES, message())],
        ["real_answer"],
        faults=faulted("refuse", form="plain"),
    )
    add(
        "fault refuse form classifier",
        [(MESSAGES, message())],
        ["real_answer"],
        faults=faulted("refuse", form="classifier"),
    )
    add(
        "fault refuse form classifier stream",
        [(MESSAGES, message(stream=True))],
        ["real_stream_answer"],
        faults=faulted("refuse", form="classifier"),
    )
    add(
        "fault refuse form bad",
        [(MESSAGES, message())],
        ["real_answer"],
        faults=faulted("refuse", form="loud"),
    )
    # unknown tool names the tool the row names, or else an offered tool with _v2 added; a name the
    # request offers is a mistake.
    add(
        "fault unknown tool named",
        [(MESSAGES, message(tools=[ORDER]))],
        ["real_answer"],
        faults=faulted("unknown tool", tool="delete_account"),
    )
    add(
        "fault unknown tool named offered",
        [(MESSAGES, message(tools=[ORDER]))],
        ["real_answer"],
        faults=faulted("unknown tool", tool="get_order"),
    )
    add(
        "fault unknown tool v2 offered",
        [(MESSAGES, message(tools=[ORDER, {**ORDER, "name": "get_order_v2"}]))],
        ["real_answer"],
        faults=faulted("unknown tool"),
        seen=("get_order_v2",),
    )
    add(
        "fault unknown tool no tools",
        [(MESSAGES, message())],
        ["real_answer"],
        faults=faulted("unknown tool"),
    )
    add(
        "marker unknown tool",
        [(MESSAGES, message(tools=[ORDER])), (MESSAGES, anthropic_followed)],
        ["real_answer", "real_context_too_long"],
        faults=faulted("unknown tool", "first 1"),
    )
    forced = message(tool_choice={"type": "any"})
    add("fault refuse forced call", [(MESSAGES, forced)], ["real_answer"], faults=faulted("refuse"))
    add(
        "fault refuse forced call classifier",
        [(MESSAGES, forced)],
        ["real_answer"],
        faults=faulted("refuse", form="classifier"),
    )
    add(
        "fault replace on an error reply",
        [(MESSAGES, message())],
        ["500:real_error_401"],
        faults=faulted("replace", text="NEW"),
    )
    # Replies the fake cannot send.
    full = anthropic_reply("doc_stream_cached.sse")
    head = full.split(b"event: message_delta")[0]
    cut = Served(headers=STREAM, chunks=(head,), cut=True)
    add("stream cut", [(MESSAGES, message(stream=True))], script=static(cut))
    add(
        "stream cut unpriced",
        [(MESSAGES, message(model="no-such-model", stream=True))],
        script=static(cut),
        budget=None,
    )
    add(
        "stream in pieces",
        [(MESSAGES, message(stream=True))],
        script=static(Served(headers=STREAM, chunks=pieces(full, 11))),
    )
    add(
        "stream gzip",
        [(MESSAGES, message(stream=True))],
        script=static(Served(headers={**STREAM, **GZIP}, chunks=(packed(full),))),
    )
    overloaded = (
        b'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", '
        b'"message": "Overloaded"}}\n\n'
    )
    add(
        "stream ends in an error",
        [(MESSAGES, message(stream=True))],
        script=static(Served(headers=STREAM, chunks=(head + overloaded,))),
    )
    for tier in ("priority", "batch", "standard"):
        add(
            f"reply tier {tier}",
            [(MESSAGES, message())],
            script=static(json_reply(tiered(tier))),
        )
    add(
        "reply speed fast",
        [(MESSAGES, message())],
        script=static(json_reply(tiered("standard", speed="fast"))),
    )
    cached = json.loads(anthropic_reply("doc_cached.json"))
    split_off = {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 100}
    for name, usage in (
        ("total only", {"cache_creation": None}),
        ("split off", {"cache_creation": split_off}),
        ("negative", {"cache_read_input_tokens": -5}),
        ("text", {"cache_read_input_tokens": "x"}),
        ("null", {"cache_read_input_tokens": None, "cache_creation_input_tokens": None}),
        ("no input", {"input_tokens": None}),
        ("no output", {"output_tokens": None}),
    ):
        body = json.dumps({**cached, "usage": {**cached["usage"], **usage}}).encode()
        add(f"usage {name}", [(MESSAGES, message())], script=static(json_reply(body)))
    for value, name in ((0.25, "high"), (0.0, "zero"), (-2, "negative"), ("9", "text")):
        body = json.dumps({**cached, "usage": {**cached["usage"], "cost": value}}).encode()
        add(f"gateway cost usage {name}", [(MESSAGES, message())], script=static(json_reply(body)))
    plain = anthropic_reply("real_answer.json")
    add(
        "gateway cost header",
        [(MESSAGES, message())],
        script=static(json_reply(plain, **{COST: "0.5"})),
    )
    # Clients, hosts and the rest of the path.
    for client in ("httpx2 async", "httpx", "anthropic", "anthropic async"):
        lone = {"client": client}
        add(f"{client}: messages", [(MESSAGES, message())], ["real_answer"], **lone)
        add(f"{client}: cached", [(MESSAGES, message())], ["real_cached"], **lone)
        streamed = message(stream=True)
        add(f"{client}: stream", [(MESSAGES, streamed)], ["real_stream_tool_use"], **lone)
        add(f"{client}: error 429", [(MESSAGES, message())], ["429:real_error_401"], **lone)
        add(f"{client}: shortfall", [(MESSAGES, message())], ["real_answer"], budget=1e-9, **lone)
        add(
            f"{client}: fault status",
            [(MESSAGES, message())],
            ["real_answer"],
            faults=faulted("status 529", retry_after=3),
            **lone,
        )
        add(
            f"{client}: fault refuse",
            [(MESSAGES, message())],
            ["real_answer"],
            faults=faulted("refuse"),
            **lone,
        )
    twice = [(MESSAGES, message()), (MESSAGES, message())]
    add(
        "anthropic: retry fault",
        twice,
        ["real_answer"],
        client="anthropic",
        faults=faulted("status 529", "requests 1"),
    )
    marked = [(MESSAGES, message(), {RETRY: n}) for n in ("0", "1", "2")]
    add(
        "retry fault requests 2",
        marked,
        ["real_answer"],
        faults=faulted("status 529", "requests 2"),
    )
    add("host listed twice", [(MESSAGES, message())], ["real_answer"], hosts=2)
    add("host listed for both", [(MESSAGES, message())], ["real_answer"], both=True)
    add("unlisted host", [(MESSAGES, message())], ["real_answer"], listed=False)
    add("unlisted host probing", [(MESSAGES, message())], ["real_answer"], listed=False, probe=True)
    add(
        "model path run for real",
        [(MESSAGES, message())],
        ["real_answer"],
        listed=False,
        real=("http:127.0.0.1",),
    )
    refusing = json.dumps({"error": {"message": "no GET here"}}).encode()
    add(
        "web get on a model path",
        [("GET " + MESSAGES, {})],
        script=static(Served(501, chunks=(refusing,))),
    )
    add("web batches", [(MESSAGES + "/batches", {"requests": []})], ["real_answer"])
    add("web threads", [("/v1/threads/t1/messages", {"x": 1})], ["real_answer"])
    add("messages query string", [(MESSAGES + "?beta=true", message())], ["real_answer"])
    add("messages trailing slash", [(MESSAGES + "/", message())], ["real_answer"])
    add(
        "messages prefixed",
        [("/anthropic" + MESSAGES, message())],
        script=static(json_reply(plain)),
    )
    # Text a fault put in a result, found in the next request.
    result = {"type": "tool_result", "tool_use_id": "toolu_1"}
    blocks = [{"type": "text", "text": "x"}, {"type": "text", "text": NOTE}]
    sent = (
        ("string content", message(messages=[{"role": "user", "content": f"a {NOTE} b"}])),
        ("text block", message(messages=[{"role": "user", "content": blocks}])),
        (
            "result string",
            message(messages=[{"role": "user", "content": [{**result, "content": f"a {NOTE}"}]}]),
        ),
        (
            "result blocks",
            message(messages=[{"role": "user", "content": [{**result, "content": blocks}]}]),
        ),
        ("absent", message()),
        ("messages not a list", message(messages="x")),
        ("message not a dict", message(messages=[7])),
        ("content null", message(messages=[{"role": "user", "content": None}])),
        ("spaced", message(messages=[{"role": "user", "content": "IGNORE  your\ninstructions"}])),
    )
    for name, body in sent:
        add(f"injected text {name}", [(MESSAGES, body)], ["real_answer"], injected=(NOTE,))
    # What the tokens of the top level leave out: each attempt of a fallback, a search, code.
    add("billing fallback", [(MESSAGES, message(model="claude-fable-5"))], ["doc_fallback"])
    listed = {"fallbacks": [{"model": "claude-opus-4-8"}]}
    add(
        "billing fallback held",
        [(MESSAGES, message(model="claude-fable-5", **listed))],
        ["doc_fallback"],
    )
    add(
        "billing fallback default held",
        [(MESSAGES, message(model="claude-fable-5", fallbacks="default"))],
        ["doc_fallback"],
    )
    add("billing web search", [(MESSAGES, message(model="claude-bare"))], ["real_web_search"])
    add("billing no write rates", [(MESSAGES, message(model="claude-bare"))], ["doc_prewarm"])
    searching = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    add("billing search held", [(MESSAGES, message(tools=searching))], ["real_web_search"])
    code = [{"type": "code_execution_20250825", "name": "code_execution"}]
    add("billing code execution alone", [(MESSAGES, message(tools=code))], ["real_answer"])
    free_code = [*code, {"type": "web_fetch_20260209", "name": "web_fetch"}]
    add("billing code execution free", [(MESSAGES, message(tools=free_code))], ["real_answer"])
    # A faulted reply, then the request the agent builds from it: the 400 is marked.
    for word, first in (("truncate", "real_tool_use"), ("garble", "real_answer")):
        add(
            f"marker {word}",
            [(MESSAGES, message(tools=[ORDER])), (MESSAGES, anthropic_followed)],
            [first, "real_context_too_long"],
            faults=faulted(word, "first 1"),
        )
    add(
        "marker refuse",
        [(MESSAGES, message(tools=[ORDER])), (MESSAGES, anthropic_followed)],
        ["real_answer", "real_context_too_long"],
        faults=faulted("refuse", "first 1"),
    )
    # A fault, then the request the agent builds from the real reply: no marker is kept.
    add(
        "fault status then follow-up",
        [(MESSAGES, message(tools=[ORDER])), (MESSAGES, anthropic_followed)],
        ["real_tool_use", "real_answer"],
        faults=faulted("status 529", "first 1"),
    )


def settled(value: object) -> object:
    """A line without the keys that hold a clock reading."""
    if isinstance(value, dict):
        for key in MEASURED_KEYS & value.keys():
            MEASURED.setdefault(key, []).append(value[key])
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


LIBRARIES = {"httpx2": httpx2, "httpx": httpx}
SDKS = ("openai", "anthropic")  # the libraries that send through a client of their own
SENDS = {
    module: (module.Client._send_single_request, module.AsyncClient._send_single_request)
    for module in LIBRARIES.values()
}


def restore_sends() -> None:
    for module, (sync_send, async_send) in SENDS.items():
        module.Client._send_single_request = sync_send
        module.AsyncClient._send_single_request = async_send


def build_response(module: Any, served: Served, *, sync: bool) -> Any:
    """The library's own reply for what a scripted server sends."""
    chunks, dropped = served.chunks, served.cut
    base = module.SyncByteStream if sync else module.AsyncByteStream

    class Pieces(base):
        def __iter__(self) -> Any:
            yield from chunks
            if dropped:
                raise module.ReadError("the connection dropped")

        async def __aiter__(self) -> Any:
            for chunk in chunks:
                yield chunk
            if dropped:
                raise module.ReadError("the connection dropped")

    headers = {"content-type": "application/json", **served.headers}
    return module.Response(served.status, headers=headers, stream=Pieces())


def scripted(module: Any, scenario: Scenario, sent: list[str], *, sync: bool) -> Any:
    """A transport that answers from the scenario's script and notes what it was sent."""
    script = scenario.script
    assert script is not None

    def handle(request: Any) -> Any:
        sent.append(hashlib.sha256(request.content).hexdigest())
        served = script(request.content)
        if served.raises:
            raise getattr(module, served.raises)("scripted failure", request=request)
        return build_response(module, served, sync=sync)

    return module.MockTransport(handle)


def dead_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def step_of(call: Any) -> tuple[str, str, Body, dict[str, str]]:
    """A call as the scenario lists it: method, path, body and extra headers."""
    path, made, *rest = call
    method, _, path = path.rpartition(" ")
    return method or "POST", path, made, (rest[0] if rest else {})


def body_for(made: Body, contents: list[bytes]) -> Any:
    return made(contents) if callable(made) else made


def noted(got: Any) -> dict[str, object]:
    return {
        "status": got.status_code,
        "headers": {k: got.headers[k] for k in KEPT_HEADERS if k in got.headers},
        "body": hashlib.sha256(got.content).hexdigest(),
        # The reply is kept as its digest and its length, so a line of the golden file stays short.
        "length": len(got.content),
    }


def raised(error: BaseException) -> dict[str, object]:
    return {"raised": type(error).__name__, "text": str(error)[:200]}


def payload_of(body: Any) -> bytes:
    return body if isinstance(body, bytes) else json.dumps(body).encode()


def sync_client(scenario: Scenario, base: str, sent: list[str]) -> Any:
    library = scenario.client.split()[0]
    module = LIBRARIES.get(library, httpx2)
    transport = scripted(module, scenario, sent, sync=True) if scenario.script else None
    if library == "anthropic":
        import anthropic

        inner = httpx2.Client(transport=transport) if transport else None
        return anthropic.Anthropic(
            base_url=base, api_key="sk-ant-replay", max_retries=0, http_client=inner
        )
    if library != "openai":
        return module.Client(transport=transport)
    import openai

    inner = httpx2.Client(transport=transport) if transport else None
    return openai.OpenAI(base_url=base, api_key="sk-replay", max_retries=0, http_client=inner)


def async_client(scenario: Scenario, base: str, sent: list[str]) -> Any:
    library = scenario.client.split()[0]
    module = LIBRARIES.get(library, httpx2)
    transport = scripted(module, scenario, sent, sync=False) if scenario.script else None
    if library == "anthropic":
        import anthropic

        inner = httpx2.AsyncClient(transport=transport) if transport else None
        return anthropic.AsyncAnthropic(
            base_url=base, api_key="sk-ant-replay", max_retries=0, http_client=inner
        )
    if library != "openai":
        return module.AsyncClient(transport=transport)
    import openai

    inner = httpx2.AsyncClient(transport=transport) if transport else None
    return openai.AsyncOpenAI(base_url=base, api_key="sk-replay", max_retries=0, http_client=inner)


def drive(scenario: Scenario, base: str, sent: list[str]) -> tuple[list[object], list[bytes]]:
    """Make the scenario's calls with its client, and keep what each one got."""
    replies: list[object] = []
    contents: list[bytes] = []
    library = scenario.client.split()[0]
    if scenario.client.endswith("async"):

        async def run_async() -> None:
            client = async_client(scenario, base, sent)
            try:
                for call in scenario.calls:
                    method, path, made, extra = step_of(call)
                    body = body_for(made, contents)
                    try:
                        if library in SDKS:
                            send = client.get if method == "GET" else client.post
                            got = await send(path, cast_to=httpx2.Response, body=body)
                        else:
                            got = await client.request(
                                method,
                                base + path,
                                content=payload_of(body),
                                headers={"content-type": "application/json", **extra},
                            )
                            await got.aread()
                    except BaseException as error:  # a fault raises what the library would see
                        replies.append(raised(error))
                        continue
                    contents.append(got.content)
                    replies.append(noted(got))
            finally:
                await client.close() if library in SDKS else await client.aclose()

        asyncio.run(run_async())
        return replies, contents
    client = sync_client(scenario, base, sent)
    try:
        for call in scenario.calls:
            method, path, made, extra = step_of(call)
            body = body_for(made, contents)
            try:
                if library in SDKS:
                    send = client.get if method == "GET" else client.post
                    got = send(path, cast_to=httpx2.Response, body=body)
                else:
                    got = client.request(
                        method,
                        base + path,
                        content=payload_of(body),
                        headers={"content-type": "application/json", **extra},
                    )
                    got.read()
            except BaseException as error:  # a fault raises what the library would see
                replies.append(raised(error))
                continue
            contents.append(got.content)
            replies.append(noted(got))
    finally:
        client.close()
    return replies, contents


def run_one(scenario: Scenario) -> dict[str, object]:
    restore_sends()
    random.seed(SEED)
    fake: FakeOpenAI | FakeAnthropic
    if scenario.provider == "anthropic":
        fake = FakeAnthropic(scenario.replies, count_tokens=scenario.count_tokens)
    else:
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
        names = ("openai", "anthropic") if scenario.both else (scenario.provider,)
        hosts = (
            tuple(ModelHost("127.0.0.1", name) for name in names) * scenario.hosts
            if scenario.listed
            else ()
        )
        priced = PRICES + ANTHROPIC_PRICES if scenario.provider == "anthropic" else PRICES
        plan = Plan(
            "run-1",
            "t",
            "row",
            (),
            scenario.faults,
            ("acme.tools:get_order",),
            scenario.real,
            60.0,
            budget_usd=scenario.budget,
            model_hosts=hosts,
            prices=priced,
            service_tier=scenario.tier,
            probe=scenario.probe,
        )
        sink, budget = Sink(), Budget(scenario.budget)
        run = state.RunState(plan, sink, [])
        run.injected.extend(scenario.injected)
        run.listed.update(scenario.seen)
        state.install(run)
        stops: list[str] = []
        ctx = http_gate.Context(run, budget, sink, stops.append)
        # The probe looks a tool's name up in the team's loaded modules under this folder; with
        # none, what a run holds does not depend on which tests ran before it in the process.
        ctx.root = ""
        http_gate.patch(httpx2, ctx)
        http_gate.patch(httpx, ctx, "httpx")
        base = f"http://127.0.0.1:{dead_port()}/v1" if scenario.dead else fake.base_url
        sent: list[str] = []
        replies, _ = drive(scenario, base, sent)
        state.install(None)
        blobs = {sha: hashlib.sha256(data).hexdigest() for sha, data in sink.blobs.items()}
        return {
            "replies": replies,
            "lines": settled(sink.lines),
            "reserved": budget.reserved,
            "settles": sink.settles,
            "blobs": sorted(blobs),
            "sent": [hashlib.sha256(b).hexdigest() for _, b in fake.received] + sent,
            "stops": stops,
        }
    finally:
        fake.stop()
        restore_sends()


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
    """One scenario is a block, one field of it a line, and one reply or log line of it a line, so
    a change shows on the line of the field that changed."""

    def put(value: object) -> str:
        return json.dumps(value, sort_keys=True, default=str)

    blocks: list[str] = []
    for name, run in found.items():
        fields = cast("dict[str, object]", run)
        rows: list[str] = []
        for key in sorted(fields):
            value = fields[key]
            if isinstance(value, list) and value and isinstance(value[0], dict):
                items = ",\n".join(f"    {put(item)}" for item in cast("list[object]", value))
                rows.append(f"  {put(key)}: [\n{items}\n  ]")
            else:
                rows.append(f"  {put(key)}: {put(value)}")
        blocks.append(f"{put(name)}: {{\n" + ",\n".join(rows) + "\n}")
    return "{\n" + ",\n".join(blocks) + "\n}\n"


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else str(GOLDEN)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(dumps(capture()))
    print(f"wrote {out}")
