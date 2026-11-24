"""Anthropic's address table, error bodies, token counts, hosted tools and prices.

Pure functions of a request's host, path and body, or of a reply's body: the same jobs as
provider_openai, over the Messages API. Anthropic's replies are never put in OpenAI's form.
The faults that change a reply live in providers/impl/anthropic_changes. unknown tool is not
done yet: it says so, and the gate refuses the row.
"""

import dataclasses
import json
import math
import re
import string
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from scrimmage import prices
from scrimmage.judge import normal
from scrimmage.prices import Rates as Rates
from scrimmage.providers.base import NO_FIELDS
from scrimmage.providers.base import Fields as Fields
from scrimmage.providers.base import Given as Given
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
PENDING = ("unknown tool",)
# The words no Anthropic reply can take, whatever the request: a mistake in the test, known
# before the run. In a reply that is not streamed, a tool call's input is a JSON object, not
# JSON text; a streamed reply sends it as text, but Scrimmage changes no streamed reply.
NEVER = {
    "bad tool args": (
        "is an Anthropic call, and bad tool args cannot be built: in a reply that is not "
        "streamed a tool call's input is a JSON object, so it cannot fail to parse, and "
        "Scrimmage changes no streamed reply; use a status, bad body, context too long or "
        "timeout on this one"
    )
}

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
# What bad body sends a model call: the first 60 bytes of a real Messages reply
# (tests/fixtures/anthropic/real_answer.json), so a reader that logs the body sees a real reply's
# start, and it cannot parse. A real reply starts with its model, not its id as the docs' example
# does. A token count gets the start of its own reply.
BROKEN_BODY = b'{"model":"claude-haiku-5-5","id":"msg_011ChLK89QC5hfqpA15gZ9'
BROKEN_COUNT = b'{"input_tok'
# A real reply to a Messages request of 210023 tokens for claude-haiku-4-5-20251001, whose window
# is 200000, saved on 23 November 2026 (tests/fixtures/anthropic/real_context_too_long.json), with
# status 400. The numbers are that request's, not the faulted one's: the gate does not count a
# request's tokens.
CONTEXT_TOO_LONG = "prompt is too long: 210023 tokens > 200000 maximum"

# Tool types, by family, with the date of the version, _YYYYMMDD, taken off. Run by the agent:
# the model asks, the agent's code runs it. Run at Anthropic and only read.
AGENT_FAMILIES = {"bash", "text_editor", "memory", "computer"}
READ_FAMILIES = {"web_search", "web_fetch", "code_execution", "tool_search_tool"}
VERSION = re.compile(r"_\d{8}$")
# What a change names in an Anthropic reply: a content block's text, or a tool call's input.
CONTENT_PART = re.compile(r"content\[(\d+)\]\.(text|input)")


@dataclass(frozen=True)
class Attempt:
    """One billed attempt of a call that more than one model served, as usage.iterations lists
    it: its model and its own counts. input holds the cache reads and writes, as Usage.input
    does."""

    model: str
    input: int
    output: int
    cached: int
    written_5m: int
    written_1h: int
    # The speed the attempt ran at, by the tier that prices it: "fast", or None for standard.
    # The reply never says it, so it comes from the request; speed_known is False when the
    # request was not read, and the attempt is then settled as the reply's own tier says.
    tier: str | None = None
    speed_known: bool = False


@dataclass
class AnthropicUsage(Usage):
    """Usage, with Anthropic's two cache lifetimes named, which its tests and docs speak of.
    attempts holds each billed attempt of a call several models served, in order, and is empty
    for a call one model answered. searches is the number of web searches the reply counts."""

    attempts: tuple[Attempt, ...] = ()
    searches: int = 0

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
    """Why a fault cannot be carried out on this request: the mistake in the test. A fault not
    done yet for Anthropic says so."""
    del path, wire
    if word in NEVER:
        return NEVER[word]
    if word in PENDING:
        return (
            f"is an Anthropic call, and {word} is {NOT_YET}; use a status, bad body, "
            "context too long or timeout on this one"
        )
    return None


def never_carried_out(word: str) -> str | None:
    """Why this fault can never be carried out on an Anthropic call, whatever the request."""
    return NEVER.get(word)


# refuse. The plain form is how Claude declines in its own words, with stop_reason end_turn: its
# text is the first sentence of a real decline (tests/fixtures/anthropic/real_decline_address.json,
# saved on 24 November 2026), which names no subject, so it fits any request. The classifier form
# is the refusal of Anthropic's safety classifier: no content, stop_reason refusal and
# stop_details (tests/fixtures/anthropic/doc_refusal.json shows the shape). Its category and
# explanation are null, which the library's RefusalStopDetails allows "when the refusal doesn't map
# to a named category": no named category is claimed, so no harm is named.
FORMS = ("plain", "classifier")
PLAIN_REFUSAL = "I can't provide that."
CLASSIFIER_DETAILS: dict[str, object] = {
    "type": "refusal",
    "category": None,
    "explanation": None,
}
BUILT_OUTPUT_TOKENS = 10  # a built plain reply's output count: about its own length
ID_LETTERS = string.ascii_letters + string.digits
ID_LENGTH = 24  # after msg_, as in a real reply's id


def made_up_id(prefix: str, length: int) -> str:
    """An id in Anthropic's form. secrets, so the agent's own random sequence is left alone."""
    import secrets  # not at import: only a made-up reply needs it, and it pulls in random

    return prefix + "".join(secrets.choice(ID_LETTERS) for _ in range(length))


def refusal_form_problem(form: str) -> str | None:
    """None for each form this provider builds, else why not."""
    if form in FORMS:
        return None
    return f'is an Anthropic call, and refuse has the forms "plain" and "classifier", not {form!r}'


def refusal_reply(path: str, wire: Fields, tokens_in: int, form: str | None = None) -> bytes:
    """refuse: a whole Messages reply that declines, laid out as a real one is, with its keys in
    the real order. Plain by default; form "classifier" builds the classifier's refusal, which
    stopped before any output."""
    del path
    classifier = form == "classifier"
    reply: dict[str, object] = {
        "model": model_name(wire) or "",
        "id": made_up_id("msg_", ID_LENGTH),
        "type": "message",
        "role": "assistant",
        "content": [] if classifier else [{"type": "text", "text": PLAIN_REFUSAL}],
        "container": None,
        "stop_reason": "refusal" if classifier else "end_turn",
        "stop_sequence": None,
        "stop_details": CLASSIFIER_DETAILS if classifier else None,
        "usage": {
            "input_tokens": tokens_in,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 0 if classifier else BUILT_OUTPUT_TOKENS,
            "output_tokens_details": {"thinking_tokens": 0},
            "service_tier": "standard",
            "inference_geo": "global",
            "speed": "standard",
        },
        "diagnostics": None,
    }
    return json.dumps(reply, separators=(",", ":"), ensure_ascii=False).encode()


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


def given(wire: Fields, reply: bytes, *, ids_only: bool) -> Given:
    """What this reply to this request gave the agent. ids_only: the tool call ids alone, for a
    reply whose message any model may send, such as a call to a tool no one offered."""
    message = _assistant(request_fields(reply))
    ids = frozenset(
        found
        for block in _list(message.get("content"))
        if _obj(block).get("type") == "tool_use"
        and isinstance(found := _obj(block).get("id"), str)
        and found
    )
    if ids_only:
        return Given(ids)
    asked = [_normal(_whole(item)) for item in _list(fields_dict(wire).get("messages"))]
    prefixes = frozenset[tuple[int, bytes]]()
    if message:
        prefixes = frozenset([_digest([*asked, _normal(message)])])
    return Given(ids, prefixes)


def carries(wire: Fields, kept: Sequence[Given]) -> bool:
    """Whether a Messages request carries what any of these faulted replies gave the agent: an
    assistant tool_use or a user tool_result with one of its toolu_ ids, or messages that start
    with a faulted request's messages and its reply's message. The request is hashed once,
    message by message, so the check grows with the request, not with the faults. An id that is
    not a string is the agent's own mistake: it matches nothing, and the request goes on to the
    provider, which answers it."""
    if not kept:
        return False
    ids = frozenset[str]().union(*(gave.ids for gave in kept))
    messages = [_obj(item) for item in _list(fields_dict(wire).get("messages"))]
    for message in messages:
        for block in _list(message.get("content")):
            held = _obj(block)
            kind = held.get("type")
            found = held.get("id") if kind == "tool_use" else held.get("tool_use_id")
            if kind in ("tool_use", "tool_result") and _among(found, ids):
                return True
    wanted = frozenset[tuple[int, bytes]]().union(*(gave.prefixes for gave in kept))
    counts = {count for count, _ in wanted}
    if not counts:
        return False
    sent = (_normal(_whole(message)) for message in messages[: max(counts)])
    return any(found in wanted for found in _running(sent, counts))


def _assistant(reply: Fields | None) -> dict[str, object]:
    """The assistant message a Messages reply is: its role and its content, which the agent
    sends back as the next turn. Empty for a reply that is not one."""
    content = fields_dict(reply).get("content")
    return {"role": "assistant", "content": content} if isinstance(content, list) else {}


def _whole(message: object) -> object:
    """A message with its content as blocks: text sent as a string is one text block."""
    held = _obj(message)
    if isinstance(held.get("content"), str):
        return {**held, "content": [{"type": "text", "text": held["content"]}]}
    return message


def _among(found: object, ids: frozenset[str]) -> bool:
    """Whether an id a request sends is one of these. A list or an object as an id cannot be
    looked up in a set, and is never one of them."""
    return isinstance(found, str) and found in ids


def _digest(prefix: list[object]) -> tuple[int, bytes]:
    """The count and digest of these messages, as a request that starts with them gets."""
    *_, last = _running(prefix, {len(prefix)})
    return last


def _running(messages: Iterable[object], counts: set[int]) -> Iterator[tuple[int, bytes]]:
    """For each count in counts, the count and the sha256 of the canonical JSON of the list of
    the first that many messages. One hash is built message by message, so a long request is
    read once. A key order or a space does not change it; a value does."""
    import hashlib

    whole = hashlib.sha256(b"[")
    for count, message in enumerate(messages, 1):
        if count > 1:
            whole.update(b",")
        whole.update(json.dumps(message, sort_keys=True, separators=(",", ":")).encode())
        if count in counts:
            part = whole.copy()
            part.update(b"]")
            yield count, part.digest()


def _normal(value: object) -> object:
    """A message as a later request may write it again: None, empty and missing are alike, as a
    client that drops None fields sends them."""
    if isinstance(value, dict):
        kept = {str(k): _normal(v) for k, v in cast("dict[object, object]", value).items()}
        return {k: v for k, v in kept.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_normal(item) for item in cast("list[object]", value)]
    return value


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


def usage(
    body: bytes, *, output_required: bool = True, asked: Fields = NO_FIELDS
) -> AnthropicUsage:
    """Token counts from a reply: a JSON body, or the events of a streamed one.

    The total input is input_tokens plus the cache reads plus the cache writes, as Anthropic's
    input_tokens leaves both out. A stream gives the input and cache counts in message_start and
    the output count, cumulative, in each message_delta, whose other fields may be null: the
    events are folded in order and a later count that is not null wins. A stream with no
    message_delta, cut short, has no final count and is not known. A count that is not a
    sensible whole number is not trusted, and neither is a reply whose cache writes do not add
    up: its writes are read high, as one hour writes. A token count has no usage of its own: with
    output_required False its top-level input_tokens is read.

    When the reply has usage.iterations beyond one plain message, the attempts that were billed
    are each in it, with their own model: a server-side fallback, an advisor, a compaction. The
    top-level counts describe only the attempt that produced the message, so the settle is the
    sum over the iterations, each at its own model's rates. Each billed attempt is kept in
    attempts, and the totals are the sums. The searches a reply counts are kept in searches,
    which the fee prices. writes holds only the cache writes.

    No iteration says the speed it ran at, so each attempt's is read from asked, the request
    the reply answers: see attempt_tier.
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
    searches = 0
    iterations: list[object] = []
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
        bad = _fold(numbers, counts, split) or bad
        used = _obj(numbers.get("server_tool_use"))
        if used.get("web_search_requests") is not None:
            found = _count(used, "web_search_requests")
            bad = bad or found is None
            searches = found or 0
        listed = _list(numbers.get("iterations"))
        if listed:
            iterations = listed
        tier = numbers.get("service_tier") or tier
        speed = numbers.get("speed") or speed
        cost = gateway_cost(numbers.get("cost"), cost)
    result = AnthropicUsage(tier=_tier(tier, speed), cost=cost)
    hops = _hops(iterations)
    if bad or hops is None or (streamed and not ended):
        return result
    billed = len(hops) > 1 or any(kind != "message" for kind, *_ in hops)
    if not billed and "input_tokens" not in counts:
        return result
    if not billed and output_required and "output_tokens" not in counts:
        return result
    result.known = True
    if billed:
        request = fields_dict(asked)
        result.attempts = tuple(
            Attempt(m, *_totals(c, sp), *attempt_tier(kind, m, request)) for kind, m, c, sp in hops
        )
        result.input = sum(a.input for a in result.attempts)
        result.output = sum(a.output for a in result.attempts)
        result.cached = sum(a.cached for a in result.attempts)
        short = sum(a.written_5m for a in result.attempts)
        long = sum(a.written_1h for a in result.attempts)
    else:
        result.input, result.output, result.cached, short, long = _totals(counts, split)
    result.writes = {WRITE_5M: short, WRITE_1H: long}
    result.searches = searches
    return result


# The Rates fee a web search is priced by, and the key a reply's count of searches is kept under.
SEARCH = "web_search_request"
# What Anthropic charges for a web search: $10 for each 1,000 searches, on top of the tokens
# (pricing page, "Web search tool"). A search that fails with an error is not billed, and the
# reply counts only the searches it did. A price file's [anthropic.fees] and a row's own
# web_search_request come before this.
DEFAULT_FEES = {SEARCH: 0.01}
# How many searches a request that offers web search with no max_uses is held for. The docs give
# no default; they say a question of one fact takes 1 to 3 searches and research 10 or more.
DEFAULT_SEARCHES = 10


def _fold(numbers: dict[str, object], counts: dict[str, int], split: dict[str, int]) -> bool:
    """Fold one usage object into the counts, a later count that is not null winning. True when
    a count in it is not a sensible whole number."""
    bad = False
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
    return bad


def _totals(counts: dict[str, int], split: dict[str, int]) -> tuple[int, int, int, int, int]:
    """The total input, the output, the cache reads and the 5 minute and 1 hour writes."""
    read = counts.get("cache_read_input_tokens", 0)
    short, long = _written(counts, split)
    return (
        counts.get("input_tokens", 0) + read + short + long,
        counts.get("output_tokens", 0),
        read,
        short,
        long,
    )


def _hops(
    iterations: list[object],
) -> list[tuple[str, str, dict[str, int], dict[str, int]]] | None:
    """The attempts of usage.iterations: type, model and counts of each. None when one cannot be
    read, which makes the reply not known."""
    found: list[tuple[str, str, dict[str, int], dict[str, int]]] = []
    for raw in iterations:
        hop = _obj(raw)
        counts: dict[str, int] = {}
        split: dict[str, int] = {}
        if (
            _fold(hop, counts, split)
            or "input_tokens" not in counts
            or "output_tokens" not in counts
        ):
            return None
        found.append((str(hop.get("type") or ""), str(hop.get("model") or ""), counts, split))
    return found


def attempt_tier(kind: str, model: str, request: dict[str, object]) -> tuple[str | None, bool]:
    """The tier one attempt ran at, and whether the request said. The first attempt, a message,
    runs at the request's speed. A fallback runs at the speed of the entry that names its model,
    or the request's when the entry sets none, as the hold has it. An entry of speed null runs
    at standard. When several entries name the model, or none does, the dearest speed any of
    them could run at is taken, so the settle is never under the bill. Any other kind, such as
    an advisor or a compaction, is not known, nor is any attempt of a request not read."""
    if not request:
        return None, False
    own = _speed(request.get("speed"))
    if kind == "message":
        return own, True
    if kind != "fallback_message":
        return None, False
    fallbacks = request.get("fallbacks")
    if not isinstance(fallbacks, list):
        return own, True  # "default": no entry may set a speed
    entries = [_obj(raw) for raw in cast("list[object]", fallbacks)]
    tiers = {
        _speed(entry.get("speed")) if "speed" in entry else own
        for entry in entries
        if isinstance(entry.get("model"), str)
        and snapshot_family(str(entry["model"])) == snapshot_family(model)
    }
    if not tiers:
        tiers = {own, *(_speed(entry.get("speed")) for entry in entries if "speed" in entry)}
    return ("fast" if "fast" in tiers else None), True


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


def price(
    model: str, overrides: dict[str, Rates], team_fees: Mapping[str, float] | None = None
) -> Rates | None:
    """The team's price first, whole, then the shipped table: see prices.price. The row comes with
    what a call needs beyond its own rates: the fee of a web search, where a row's own beats the
    team price file's [anthropic.fees], which beats the shipped file's, which beats Anthropic's;
    and the provider's other rows, for a call that more than one model served. Anthropic's tiers
    have no alias: fast mode is no Priority Tier."""
    found = prices.priced("anthropic", model, overrides, family=snapshot_family)
    if found is None:
        return None
    section = prices.shipped().section("anthropic")
    shipped_fees = dict(section.fees if section else ())
    fees = {**DEFAULT_FEES, **shipped_fees, **dict(team_fees or {}), **dict(found.fees)}
    every = (*overrides.items(), *prices.shipped().models("anthropic"))

    def with_extras(row: Rates) -> Rates:
        return dataclasses.replace(row, fees=tuple(fees.items()), models=every, name=model)

    return dataclasses.replace(
        with_extras(found), tiers=tuple((name, with_extras(row)) for name, row in found.tiers)
    )


def cost(
    tokens_in: int,
    tokens_out: int,
    rates: Rates,
    cached: int = 0,
    written_5m: int = 0,
    written_1h: int = 0,
    attempts: Sequence[Attempt] = (),
    searches: int = 0,
) -> float:
    """What a call costs. cached and the written counts are parts of tokens_in. A call over the
    long-context threshold costs every token at the long rates, not only those past it. A call
    several models served is priced attempt by attempt, each at its own model's row, and its
    searches at their fee."""
    extra = searches * dict(rates.fees).get(SEARCH, 0.0)
    if not attempts:
        return _tokens_cost(tokens_in, tokens_out, rates, cached, written_5m, written_1h) + extra
    return sum(_hop_cost(attempt, rates) for attempt in attempts) + extra


def _tokens_cost(
    tokens_in: int, tokens_out: int, rates: Rates, cached: int, written_5m: int, written_1h: int
) -> float:
    rates = rates.sized(tokens_in)
    plain = tokens_in - cached - written_5m - written_1h
    full = plain * rates.input + cached * _or_input(rates.cached_input, rates)
    full += written_5m * _write_rate(rates, WRITE_5M)
    full += written_1h * _write_rate(rates, WRITE_1H)
    return (full + tokens_out * rates.output) / 1_000_000


def _row_for(model: str, rates: Rates) -> Rates | None:
    """The row that prices this model: the call's own row for its own model and for a hop that
    names none, else the provider's row by name or by its family. None for a model no row prices."""
    if not model or not rates.models or snapshot_family(model) == snapshot_family(rates.name):
        return rates
    return _listed_row(model, rates)


def _listed_row(model: str, rates: Rates) -> Rates | None:
    """The model's own row among the provider's rows, by name or by its family, with its tiers:
    rates may be one tier of the call's row, which has none of its own."""
    for name, row in rates.models:
        if name == model:
            return row
    for name, row in rates.models:
        if name == snapshot_family(model):
            return row
    return None


def _row_at(model: str, rates: Rates, tier: str | None) -> Rates | None:
    """The row this model runs at on this tier. The call's own model is found among the rows
    first, so a tier other than the call's is priced too; a call row that lists no rows, as a
    test's may, is the call's own."""
    listed = _listed_row(model or rates.name, rates) if rates.models else None
    if listed is not None:
        return _at_speed(listed, tier)
    row = _row_for(model, rates)
    return None if row is None else _at_speed(row, tier)


def _hop_cost(attempt: Attempt, rates: Rates) -> float:
    """One attempt at its model's row. A model no row prices is priced at the dearest row there
    is, so an attempt never costs less than its model could."""
    if attempt.speed_known:
        row = _row_at(attempt.model, rates, attempt.tier)
        every = [rates, *(_at_speed(r, attempt.tier) for _, r in rates.models)]
    else:
        row = _row_for(attempt.model, rates)
        every = [rates, *(r for _, r in rates.models)]
    rows = [row] if row is not None else every
    return max(
        _tokens_cost(
            attempt.input,
            attempt.output,
            candidate,
            attempt.cached,
            attempt.written_5m,
            attempt.written_1h,
        )
        for candidate in rows
    )


def dearest(
    rates: Rates,
    tokens_in: int,
    tokens_out: int,
    cached: int,
    written_5m: int,
    written_1h: int,
    attempts: Sequence[Attempt] = (),
    searches: int = 0,
) -> Rates:
    """Of the default rates and every tier the row prices, the ones this call costs most at,
    long rates included at the call's size: for a reply that names a tier the row does not price."""
    every = (rates, *(tier for _, tier in rates.tiers))
    return max(
        every,
        key=lambda r: cost(
            tokens_in, tokens_out, r, cached, written_5m, written_1h, attempts, searches
        ),
    )


def most_cost(tokens_in: int, tokens_out: int, rates: Rates, *, fields: Fields) -> float:
    """What a call could cost at most. A request that sets no cache_control and offers no server
    tool is never written to the cache, so each input token costs the input rate. One that sets
    cache_control may write every input token: for 5 minutes, or for 1 hour if any of its
    cache_control says ttl 1h. A server tool caches its results by itself, as 5 minute writes. An
    input over the long-context threshold is held at the long rates. A web search is held at its
    fee for max_uses searches, or for DEFAULT_SEARCHES.

    A request with fallbacks may run one more attempt for each model it names, and each attempt
    is held on its own: at its model's row, at its own max_tokens if it names one and that is
    over the request's, and at its own speed, which is the request's if it names none. A model
    with no row, and each attempt of "default", whose model is not known until the reply, are held
    at the dearest row there is, so no attempt is held under what it could settle at."""
    asked = fields_dict(fields)
    speed = _speed(asked.get("speed"))
    holds = [_one_attempt(tokens_in, tokens_out, rates, asked)]
    fallbacks = asked.get("fallbacks")
    if fallbacks == "default":
        holds.append(_dearest_attempt(tokens_in, tokens_out, rates, asked, speed))
    elif isinstance(fallbacks, list):
        for raw in _list(cast("list[object]", fallbacks)):
            entry = _obj(raw)
            out = max(tokens_out, _cap(entry.get("max_tokens")))
            tier = _speed(entry.get("speed")) if "speed" in entry else speed
            model = entry.get("model")
            row = _row_at(model, rates, tier) if isinstance(model, str) else None
            if row is None:
                holds.append(_dearest_attempt(tokens_in, out, rates, asked, tier))
            else:
                holds.append(_one_attempt(tokens_in, out, row, asked))
    fee = dict(rates.fees).get(SEARCH, 0.0) * _searches(asked)
    return sum(holds) + len(holds) * fee


def _speed(value: object) -> str | None:
    """The tier a speed names: fast for fast, none for any other."""
    return "fast" if value == "fast" else None


def _cap(value: object) -> int:
    """A max_tokens that is a count, else 0, which is no cap."""
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_TOKENS
    return value if ok and isinstance(value, int) else 0


def _at_speed(row: Rates, tier: str | None) -> Rates:
    return row.at(tier)


def _dearest_attempt(
    tokens_in: int, tokens_out: int, rates: Rates, asked: dict[str, object], tier: str | None
) -> float:
    """One attempt on whichever row is dearest, the call's own included."""
    rows = [rates, *(_at_speed(row, tier) for _, row in rates.models)]
    return max(_one_attempt(tokens_in, tokens_out, row, asked) for row in rows)


def _one_attempt(tokens_in: int, tokens_out: int, rates: Rates, asked: dict[str, object]) -> float:
    rates = rates.sized(tokens_in)
    marks = _cache_controls(asked)
    rate = rates.input
    if marks or _offers_server_tool(asked):
        rate = max(rate, _write_rate(rates, WRITE_5M))
    if any(_obj(mark).get("ttl") == "1h" for mark in marks):
        rate = max(rate, _write_rate(rates, WRITE_1H))
    return (tokens_in * rate + tokens_out * rates.output) / 1_000_000


def _tool_family(tool: object) -> str:
    return VERSION.sub("", str(_obj(tool).get("type") or ""))


def _offers_server_tool(asked: dict[str, object]) -> bool:
    return any(
        _tool_family(t) in READ_FAMILIES or _tool_family(t).startswith("tool_search_tool")
        for t in _list(asked.get("tools"))
    )


def _searches(asked: dict[str, object]) -> int:
    """The most searches the request's web search tools may run: each one's max_uses, or the
    default."""
    total = 0
    for tool in _list(asked.get("tools")):
        if _tool_family(tool) != "web_search":
            continue
        uses = _obj(tool).get("max_uses")
        ok = isinstance(uses, int) and not isinstance(uses, bool) and 0 < uses <= MAX_TOKENS
        total += uses if ok and isinstance(uses, int) else DEFAULT_SEARCHES
    return total


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
    search. Code execution is refused when its container time is billed, which no reply reports.
    An MCP server the request names runs at Anthropic and can change data. Any other type, the
    computer and browser toolsets included, is not known."""
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
        elif family == "code_execution" and not free_code_execution(fields):
            found.refused.append((kind, CODE_EXECUTION_WHY))
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


# Code execution is billed by the hour of container time, after 1,550 free hours a month for the
# organization, and a reply counts only its requests, never the time (pricing page, "Code execution
# tool"). A budget cannot count it, so it is refused. With web search or web fetch of the 20260209
# versions or later it costs nothing beyond tokens, and that is the one case that is let through.
CODE_EXECUTION_WHY = (
    "code execution is billed by container time, which a reply does not report, so a budget "
    "cannot count it; offer it with web_search_20260209 or web_fetch_20260209 or later, where it "
    "is free"
)
FREE_WITH = ("web_search", "web_fetch")
FREE_FROM = "20260209"


def free_code_execution(fields: dict[str, object]) -> bool:
    """Whether the request offers a web search or web fetch tool of the version that makes code
    execution free."""
    for tool in _list(fields.get("tools")):
        kind = str(_obj(tool).get("type") or "")
        found = VERSION.search(kind)
        if VERSION.sub("", kind) in FREE_WITH and found and found.group(0)[1:] >= FREE_FROM:
            return True
    return False


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
