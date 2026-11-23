"""Anthropic's address table, error bodies, token counts, hosted tools and prices.

Pure functions of a request's host, path and body, or of a reply's body: the same jobs as
provider_openai, over the Messages API. Anthropic's replies are never put in OpenAI's form.
The faults that change or build a reply are not done yet: each says so, and the gate refuses
the row.
"""

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from scrimmage import prices
from scrimmage.judge import normal
from scrimmage.prices import Rates as Rates
from scrimmage.providers.base import Fields as Fields
from scrimmage.providers.base import Offered as Offered
from scrimmage.providers.base import Reply as Reply
from scrimmage.providers.base import Usage as Usage
from scrimmage.providers.impl.wire import fields_dict, reply_value, wrap_fields

HOSTS = ("api.anthropic.com",)
CHAT_SUFFIX = "/messages"
COUNT_SUFFIX = "/messages/count_tokens"
FREE_OPS = ("input_tokens",)  # a token count bills nothing, so nothing is reserved for it
MAX_TOKENS = 1_000_000_000  # a larger count is not a count
WRITE_5M = "cache_write_5m"  # the Rates fields that price a cache write, by its lifetime
WRITE_1H = "cache_write_1h"
REQUEST_ID = "req_scrimmage_injected"
NOT_YET = "not yet for Anthropic"
# The words whose reply an Anthropic call cannot be given yet.
PENDING = ("replace", "garble", "truncate", "unknown tool", "bad tool args", "refuse")

# Every body is {"type": "error", "error": {"type", "message"}, "request_id"}. The type is by
# status, from Anthropic's errors page; any other 4xx is invalid_request_error and any other 5xx
# api_error. The 401 message is a real 401's (tests/fixtures/anthropic/real_error_401.json), the
# 404's is the errors page's example, and "Overloaded" is the streaming page's own text. A status
# with no documented message gets a plain one.
TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    504: "timeout_error",
    529: "overloaded_error",
}
MESSAGES = {
    401: "invalid x-api-key",
    404: "The requested resource could not be found.",
    529: "Overloaded",
}
# What bad body sends a model call: the first 60 bytes of the answer example on the Messages
# page, so a reader that logs the body sees a real reply's start, and it cannot parse. A token
# count gets the start of its own reply.
BROKEN_BODY = b'{"id":"msg_01XFDUDYJgAACzvnptvVoYEL","type":"message","role"'
BROKEN_COUNT = b'{"input_tok'
# What Anthropic's clients report for a prompt over the context window, with status 400. The
# numbers are those of a request of 210000 tokens, not the faulted one's: the gate does not
# count a request's tokens. From memory of those reports, to be checked against a capture.
CONTEXT_TOO_LONG = "prompt is too long: 210000 tokens > 200000 maximum"

# Tool types, by family, with the date of the version, _YYYYMMDD, taken off. Run by the agent:
# the model asks, the agent's code runs it. Run at Anthropic and only read.
AGENT_FAMILIES = {"bash", "text_editor", "memory", "computer"}
READ_FAMILIES = {"web_search", "web_fetch", "code_execution", "tool_search_tool"}
VERSION = re.compile(r"_\d{8}$")
# What a change names in an Anthropic reply: a content block's text, or a tool call's input.
CONTENT_PART = re.compile(r"content\[(\d+)\]\.(text|input)")


@dataclass
class AnthropicUsage(Usage):
    """Usage, with Anthropic's two cache lifetimes named, which its tests and docs speak of."""

    @property
    def written_5m(self) -> int:
        return self.writes.get(WRITE_5M, 0)

    @property
    def written_1h(self) -> int:
        return self.writes.get(WRITE_1H, 0)


def op_for(path: str, method: str) -> str | None:
    """chat for a Messages call, input_tokens for a token count, else None. Only a POST is, and
    a path under /threads/ is another API's."""
    if method != "POST":
        return None
    clean = path.split("?", 1)[0].rstrip("/")
    if "/threads/" in clean:
        return None
    if clean.endswith(COUNT_SUFFIX):
        return "input_tokens"
    return "chat" if clean.endswith(CHAT_SUFFIX) else None


def error_body(status: int) -> bytes:
    kind = TYPES.get(status, "api_error" if status >= 500 else "invalid_request_error")
    error = {
        "type": kind,
        "message": MESSAGES.get(status, f"The request failed with status {status}."),
    }
    return _error_json(error)


def _error_json(error: dict[str, str]) -> bytes:
    """An error body as Anthropic writes it, with no spaces, as the real 401 does."""
    body = {"type": "error", "error": error, "request_id": REQUEST_ID}
    return json.dumps(body, separators=(",", ":")).encode()


def error_content_type(status: int) -> str:
    """The content type of an error reply with this status."""
    del status
    return "application/json"


def fault_headers(content_type: str) -> dict[str, str]:
    return {"content-type": content_type, "request-id": REQUEST_ID}


def broken_body(path: str, op: str) -> bytes:
    """bad body's bytes for this call. A token count gets the start of its own reply."""
    del path
    return BROKEN_COUNT if op == "input_tokens" else BROKEN_BODY


def context_too_long_body() -> bytes:
    """What Anthropic sends, with status 400, for a prompt longer than the context window."""
    error = {"type": "invalid_request_error", "message": CONTEXT_TOO_LONG}
    return _error_json(error)


def not_carried_out(word: str, path: str, wire: Fields) -> str | None:
    """Why a fault cannot be carried out on this request: the mistake in the test. The faults
    that change or build a reply are not done yet for Anthropic, so each says so."""
    del path, wire
    if word in PENDING:
        return (
            f"is an Anthropic call, and {word} is {NOT_YET}; use a status, bad body, "
            "context too long or timeout on this one"
        )
    return None


def refusal_reply(path: str, wire: Fields, tokens_in: int) -> bytes:
    del path, wire, tokens_in
    raise NotImplementedError(f"refuse is {NOT_YET}")


def unknown_tool_reply(wire: Fields, tokens_in: int, tool: str) -> bytes:
    del wire, tokens_in, tool
    raise NotImplementedError(f"unknown tool is {NOT_YET}")


def tool_call_tools(path: str, wire: Fields) -> list[str]:
    """The tools a request offers that the agent runs, which a tool_use reply may name."""
    return offered(path, wire).agent


def no_free_tool_call(path: str, wire: Fields) -> str | None:
    """Why the model could not pick a tool of its own on this request, or None when it could."""
    if not tool_call_tools(path, wire):
        return "the request offers no tools the agent runs, so a model would call none"
    choice = _obj(fields_dict(wire).get("tool_choice")).get("type")
    if choice == "none":
        return 'the request sets tool_choice "none", so a model would call no tool'
    if choice == "tool":
        return "the request's tool_choice names the tool to call, so a model would call that one"
    return None


def parse_reply(data: bytes) -> Reply:
    """A recorded reply's bytes, parsed. A reply that is not JSON reads as nothing."""
    try:
        return Reply(json.loads(data.decode("utf-8", "replace")))
    except (ValueError, RecursionError):
        return Reply(None)


def called_tool(wire: Reply) -> str | None:
    """The name of the first tool a Messages reply calls."""
    for block in _content(reply_value(wire)):
        name = block.get("name")
        if block.get("type") == "tool_use" and isinstance(name, str):
            return name
    return None


def said(wire: Reply, parts: Sequence[object]) -> str | None:
    """The string at the first of these parts a change named, such as content[0].text, or at
    the reply's first text block if it named none. A tool call's input is said as the JSON the
    model sent. None when the reply holds no string there."""
    blocks = _content(reply_value(wire))
    for part in parts:
        found = CONTENT_PART.fullmatch(str(part))
        if found is None:
            continue
        index = int(found.group(1))
        block = blocks[index] if index < len(blocks) else {}
        if found.group(2) == "input":
            call = block.get("input")
            return json.dumps(call, separators=(",", ":")) if isinstance(call, dict) else None
        text = block.get("text")
        return text if isinstance(text, str) else None
    texts = [b.get("text") for b in blocks if b.get("type") == "text"]
    first = texts[0] if texts else None
    return first if isinstance(first, str) else None


def carries_text(wire: Fields, texts: Sequence[str]) -> bool:
    """Whether a request holds a text a fault put in a result. Only the strings of the parsed
    body are searched: each message's content as text or as blocks, and a tool_result's content
    as text or as blocks, compared as a rule compares text; the raw body would hold the text
    escaped. A heuristic."""
    messages = _list(fields_dict(wire).get("messages"))
    if not messages:
        return False
    wanted = [normal(text) for text in texts]
    for message in messages:
        if any(w in normal(text) for text in _texts(_obj(message).get("content")) for w in wanted):
            return True
    return False


def _texts(content: object) -> list[str]:
    """The strings in a content value: a text, or the text and tool_result blocks of a list,
    a result's own content read the same way."""
    if isinstance(content, str):
        return [content]
    found: list[str] = []
    for raw in _list(content):
        block = _obj(raw)
        if block.get("type") == "tool_result":
            found += _texts(block.get("content"))
        elif isinstance(block.get("text"), str):
            found.append(str(block["text"]))
    return found


def request_fields(body: bytes) -> Fields | None:
    """A request's JSON fields, or None when the body cannot be read as a JSON object."""
    try:
        value: object = json.loads(body)
    except (ValueError, RecursionError):  # not JSON, not text, or empty
        return None
    found = _obj(value)
    return wrap_fields(found) if found else None


def model_name(wire: Fields | None) -> str | None:
    """The model a request names, or None when it names none that can be read."""
    value = fields_dict(wire).get("model")
    return value if isinstance(value, str) and value else None


def streams(wire: Fields | None) -> bool:
    return fields_dict(wire).get("stream") is True


def output_cap(wire: Fields, path: str, default: int) -> int:
    """The most output tokens a call could bill: max_tokens, which every request must give and
    which thinking counts against, else the default. A value the API refuses is never billed.
    There are no choices, so one reply."""
    del path
    value = fields_dict(wire).get("max_tokens")
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_TOKENS:
        return value
    return default


def request_tier(wire: Fields | None) -> str | None:
    """The tier a request names. service_tier auto and standard_only name none; speed fast is
    the fast tier, which costs more."""
    return "fast" if fields_dict(wire).get("speed") == "fast" else None


def usage(body: bytes, *, output_required: bool = True) -> AnthropicUsage:
    """Token counts from a reply: a JSON body, or the events of a streamed one.

    The total input is input_tokens plus the cache reads plus the cache writes, as Anthropic's
    input_tokens leaves both out. A stream gives the input and cache counts in message_start and
    the output count, cumulative, in each message_delta, whose other fields may be null: the
    events are folded in order and a later count that is not null wins. A stream with no
    message_delta, cut short, has no final count and is not known. A count that is not a
    sensible whole number is not trusted, and neither is a reply whose cache writes do not add
    up: its writes are read high, as one hour writes. A token count has no usage of its own: with
    output_required False its top-level input_tokens is read.
    """
    text = body.decode("utf-8", "replace")
    # Split on newlines only: text in a reply can hold other line separators.
    data = [line[5:].strip() for line in text.split("\n") if line.startswith("data:")]
    counts: dict[str, int] = {}
    split: dict[str, int] = {}
    streamed = ended = bad = False
    tier: object = None
    speed: object = None
    cost: float | None = None
    for doc in data or [text]:
        try:
            parsed = _obj(json.loads(doc))
        except (ValueError, RecursionError):
            continue
        kind = parsed.get("type")
        numbers = _obj(parsed.get("usage")) or _obj(_obj(parsed.get("message")).get("usage"))
        if not numbers and not output_required:
            numbers = parsed  # a token count's reply: {"input_tokens": 14}
        if kind == "message_start":
            streamed = True
        if kind == "message_delta" and numbers.get("output_tokens") is not None:
            ended = True
        for key in COUNT_KEYS:
            if numbers.get(key) is not None:
                value = _count(numbers, key)
                bad = bad or value is None
                if value is not None:
                    counts[key] = value
        for key, value in _obj(numbers.get("cache_creation")).items():
            if key in SPLIT_KEYS and value is not None:
                found = _count({key: value}, key)
                bad = bad or found is None
                if found is not None:
                    split[key] = found
        tier = numbers.get("service_tier") or tier
        speed = numbers.get("speed") or speed
        cost = gateway_cost(numbers.get("cost"), cost)
    result = AnthropicUsage(tier=_tier(tier, speed), cost=cost)
    if bad or "input_tokens" not in counts or (streamed and not ended):
        return result
    if output_required and "output_tokens" not in counts:
        return result
    read = counts.get("cache_read_input_tokens", 0)
    short, long = _written(counts, split)
    result.input = counts["input_tokens"] + read + short + long
    result.output = counts.get("output_tokens", 0)
    result.known = True
    result.cached = read
    result.writes = {WRITE_5M: short, WRITE_1H: long}
    return result


COUNT_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
SPLIT_KEYS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def _written(counts: dict[str, int], split: dict[str, int]) -> tuple[int, int]:
    """The input tokens written to the cache for 5 minutes and for 1 hour. The total is the sum
    of the two, which the reply names in cache_creation. A total with no split, or a split that
    does not add up to it, says writes were made but not for how long: every one is read as a
    1 hour write, the dearest."""
    total = counts.get("cache_creation_input_tokens")
    short, long = split.get(SPLIT_KEYS[0], 0), split.get(SPLIT_KEYS[1], 0)
    if total is None:
        return short, long
    if split and short + long == total:
        return short, long
    return 0, total


def _tier(service_tier: object, speed: object) -> str | None:
    """The tier a reply says it was billed at: fast for speed fast, else the service tier,
    where standard is the default tier and so names none."""
    if speed == "fast":
        return "fast"
    if isinstance(service_tier, str) and service_tier not in ("", "standard"):
        return service_tier
    return None


def gateway_cost(value: object, before: float | None = None) -> float | None:
    """A cost in dollars a gateway put in its reply, or in the LiteLLM proxy's header, which is
    text. Anything that is not a finite number of at least 0 is no cost, and the call is priced
    from its tokens."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return before
    if isinstance(value, bool) or not isinstance(value, int | float):
        return before
    return float(value) if math.isfinite(value) and value >= 0 else before


def snapshot_family(model: str) -> str:
    """A dated snapshot's family: Anthropic dates one -YYYYMMDD. The provider's one rule, read by
    the price lookup below and by the plan's dedupe of the price rows."""
    return prices.compact_family(model)


def price(model: str, overrides: dict[str, Rates]) -> Rates | None:
    """The team's price first, whole, then the shipped table: see prices.price."""
    return prices.priced("anthropic", model, overrides, family=snapshot_family)


def cost(
    tokens_in: int,
    tokens_out: int,
    rates: Rates,
    cached: int = 0,
    written_5m: int = 0,
    written_1h: int = 0,
) -> float:
    """What a call costs. cached and the written counts are parts of tokens_in. A call over the
    long-context threshold costs every token at the long rates, not only those past it."""
    rates = rates.sized(tokens_in)
    plain = tokens_in - cached - written_5m - written_1h
    full = plain * rates.input + cached * _or_input(rates.cached_input, rates)
    full += written_5m * _write_rate(rates, WRITE_5M)
    full += written_1h * _write_rate(rates, WRITE_1H)
    return (full + tokens_out * rates.output) / 1_000_000


def dearest(
    rates: Rates, tokens_in: int, tokens_out: int, cached: int, written_5m: int, written_1h: int
) -> Rates:
    """Of the default rates and every tier the row prices, the ones this call costs most at,
    long rates included at the call's size: for a reply that names a tier the row does not price."""
    every = (rates, *(tier for _, tier in rates.tiers))
    return max(every, key=lambda r: cost(tokens_in, tokens_out, r, cached, written_5m, written_1h))


def most_cost(tokens_in: int, tokens_out: int, rates: Rates, *, fields: Fields) -> float:
    """What a call could cost at most. A request that sets no cache_control is never written to
    the cache, so each input token costs the input rate. One that sets it may write every input
    token: for 5 minutes, or for 1 hour if any of its cache_control says ttl 1h. An input over the
    long-context threshold is held at the long rates."""
    rates = rates.sized(tokens_in)
    marks = _cache_controls(fields_dict(fields))
    rate = rates.input
    if marks:
        rate = max(rate, _write_rate(rates, WRITE_5M))
    if any(_obj(mark).get("ttl") == "1h" for mark in marks):
        rate = max(rate, _write_rate(rates, WRITE_1H))
    return (tokens_in * rate + tokens_out * rates.output) / 1_000_000


def _cache_controls(fields: dict[str, object]) -> list[object]:
    """Every cache_control in the request, at the top level or on any block: system, tools,
    and each message's content, at any depth."""
    found: list[object] = []
    stack: list[object] = [fields]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in cast("dict[object, object]", node).items():
                if key == "cache_control" and value is not None:
                    found.append(value)
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(cast("list[object]", node))
    return found


def offered(path: str, wire: Fields) -> Offered:
    """The tools a Messages request offers, sorted. A tool with no type, or the type custom, is
    the agent's, by its name. So is a built-in the agent runs: bash, a text editor, memory and
    computer use. Anthropic runs, and only reads: web search, web fetch, code execution and tool
    search. An MCP server the request names runs at Anthropic and can change data. Any other type,
    the computer and browser toolsets included, is not known."""
    del path
    fields = fields_dict(wire)
    found = Offered()
    for raw in _list(fields.get("tools")):
        tool = _obj(raw)
        kind = str(tool.get("type") or "")
        family = VERSION.sub("", kind)
        if kind in ("", "custom"):
            found.agent.append(str(tool.get("name", "")))
        elif family in AGENT_FAMILIES:
            found.agent.append(str(tool.get("name") or family))
        elif family in READ_FAMILIES or family.startswith("tool_search_tool"):
            found.hosted_reads.append(family)
        elif kind == "mcp_toolset":
            continue  # each server it names is a hosted write below
        else:
            found.unknown.append(kind)
    servers = [str(_obj(s).get("name", "")) for s in _list(fields.get("mcp_servers"))]
    servers += [
        str(_obj(t).get("mcp_server_name", ""))
        for t in _list(fields.get("tools"))
        if _obj(t).get("type") == "mcp_toolset"
    ]
    found.hosted_writes = list(dict.fromkeys(servers))
    return found


def _content(value: object) -> list[dict[str, object]]:
    """The content blocks of a parsed Messages reply, or none if it is not one."""
    return [_obj(block) for block in _list(_obj(value).get("content"))]


def _obj(value: object) -> dict[str, object]:
    """A dict's items under string keys, or {} for anything else. A Fields or a Reply is no dict:
    handed one, the caller forgot fields_dict or reply_value, and {} would read it as empty."""
    if isinstance(value, Fields | Reply):
        raise TypeError("_obj was handed an opaque wire value; open it with fields_dict first")
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


def _list(value: object) -> list[object]:
    return list(cast("list[object]", value)) if isinstance(value, list) else []


def _count(numbers: dict[str, object], key: str) -> int | None:
    """A token count, or None when the reply has none or it is not a sensible number."""
    value = numbers.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKENS:
        return None
    return value


def _or_input(rate: float | None, rates: Rates) -> float:
    return rates.input if rate is None else rate


# What Anthropic charges to write the cache, as a multiple of the model's input price: 1.25 times
# for 5 minutes and 2 times for 1 hour (prompt caching page, "Pricing"; the pricing page's table).
# A row that leaves a write rate out is a team's row of input and output alone, and its writes
# cost these, not the input rate, which would be 20% and 50% low. A cached read left out stays at
# the input rate, which is high and so safe.
WRITE_MULTIPLE = {WRITE_5M: 1.25, WRITE_1H: 2.0}


def _write_rate(rates: Rates, kind: str) -> float:
    """The price of a token written to the cache for this long: the row's own, else Anthropic's
    multiple of its input price."""
    own: float | None = getattr(rates, kind)
    return WRITE_MULTIPLE[kind] * rates.input if own is None else own
