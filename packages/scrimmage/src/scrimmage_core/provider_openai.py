"""OpenAI's address table, error bodies, token counts, hosted tools and prices.

Pure functions of a request's host, path and body, or of a reply's body. The gate uses
them to label a call, build a fault the openai library reads as a real error, count
tokens and sort the tools the model is offered. A built reply, for refuse and unknown tool,
also takes the time and new ids, as a real one has.
"""

import hashlib
import json
import re
import secrets
import string
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import cast

HOSTS = ("api.openai.com",)
CHAT_PATHS = ("/chat/completions", "/responses")
# Provider calls that only read or compute: they run for real and are counted.
READ_PATHS = {
    "/embeddings": "embeddings",
    "/moderations": "moderations",
    "/responses/input_tokens": "input_tokens",
}
FREE_OPS = ("moderations", "input_tokens")  # no tokens billed, so nothing to reserve

# Every body is the spec's Error object. The 401 message is a real 401's, its key fragment
# made up; the 429 message is as OpenAI's rate-limit cookbook shows it, its code from the
# spec's error code list; the others are from OpenAI's error codes page. A status with no
# documented message gets a plain one.
MESSAGES = {
    401: (
        f"Incorrect API key provided: sk-proj-{'*' * 44}xxxx. "
        "You can find your API key at https://platform.openai.com/account/api-keys."
    ),
    429: (
        "Rate limit reached for default-codex in organization org-{id} on requests per "
        "min. Limit: 20.000000 / min. Current: 24.000000 / min. Contact support@openai.com "
        "if you continue to have issues or if you’d like to request an increase."  # noqa: RUF001  (OpenAI's own text)
    ),
    500: "The server had an error while processing your request. Sorry about that!",
    503: "The engine is currently overloaded, please try again later.",
}
# What bad body sends a model call: the first 60 bytes of the spec's example reply for its
# operation, so a reader that logs the body sees a real reply's start, and it cannot parse.
BROKEN_BODY = b'{\n  "id": "chatcmpl-B9MBs8CjcvOU2jLn4n570S5qMJKcT",\n  "objec'
BROKEN_EMBEDDING = b'{\n  "object": "list",\n  "data": [\n    {\n      "object": "emb'
BROKEN_RESPONSE = b'{\n  "id": "resp_67ccd2bed1ec8190b14f964abc0542670bb6a6b452d3'
TYPES = {429: "requests"}
CODES = {429: "rate_limit_exceeded", 401: "invalid_api_key"}
# A real 401 says it is text/plain, though its body is JSON, and names its status at the top
# level too. The openai library parses an error's body whatever its content type says.
TEXT_PLAIN = (401,)
# A real reply to a gpt-4o-mini request of 140008 tokens. The numbers are that request's,
# not the faulted one's: the gate does not count a request's tokens.
CONTEXT_TOO_LONG = {
    "message": (
        "This model's maximum context length is 128000 tokens. However, your messages "
        "resulted in 140008 tokens. Please reduce the length of the messages."
    ),
    "type": "invalid_request_error",
    "param": "messages",
    "code": "context_length_exceeded",
}


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens. With no cached rate, cached tokens cost the full input
    rate: a price that guesses must guess high, or the budget could let through more. With
    no cache-write rate, a written token costs the full input rate, as on a model that
    charges nothing extra to write its cache."""

    input: float
    output: float
    cached_input: float | None = None
    cache_write_30m: float | None = None  # a token written to the cache for 30 minutes
    cache_write_12h: float | None = None  # for 12 hours, a Responses API choice


# Checked against OpenAI's pricing page on 19 November 2026, standard tier; check again
# before each release. A team overrides or adds a model under [agent.prices]. A model with
# a long-context price, above 272K input tokens, is left out, such as gpt-5.4, gpt-5.5
# and the gpt-5.6 models: one rate per kind of token would price a long call low. Only
# those models price a cache write, so no model here has one.
PRICES: dict[str, Rates] = {
    "gpt-4o": Rates(2.50, 10.00, 1.25),
    # Its own price, not its family's; shut down, so no longer on the pricing page. No
    # cached rate: it had no prompt cache discount. A guess high is safe, low is not.
    "gpt-4o-2024-05-13": Rates(5.00, 15.00),
    "gpt-4o-mini": Rates(0.15, 0.60, 0.075),
    "gpt-4.1": Rates(2.00, 8.00, 0.50),
    "gpt-4.1-mini": Rates(0.40, 1.60, 0.10),
    "gpt-4.1-nano": Rates(0.10, 0.40, 0.025),  # as on 7 November; off the page since
    "gpt-5": Rates(1.25, 10.00, 0.125),
    "gpt-5-mini": Rates(0.25, 2.00, 0.025),
    "gpt-5-nano": Rates(0.05, 0.40, 0.005),
    "gpt-5-pro": Rates(15.00, 120.00),  # the page gives no cached rate
    "gpt-5.1": Rates(1.25, 10.00, 0.125),
    "gpt-5.2": Rates(1.75, 14.00, 0.175),
    "gpt-5.2-pro": Rates(21.00, 168.00),
    "gpt-5.3-codex": Rates(1.75, 14.00, 0.175),
    "gpt-5.4-mini": Rates(0.75, 4.50, 0.075),
    "gpt-5.4-nano": Rates(0.20, 1.25, 0.02),
    "text-embedding-3-small": Rates(0.02, 0.0),
    "text-embedding-3-large": Rates(0.13, 0.0),
}

# Tool types, per API: run at the provider and only read; run at the provider and can
# change data; run by the agent, so a gate must see them.
RESPONSES_READ = {
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "image_generation",
    "programmatic_tool_calling",
}
RESPONSES_AGENT = {
    "function",
    "custom",
    "local_shell",
    "shell",
    "apply_patch",
    "computer",
    "computer_use_preview",
}
CHAT_AGENT = {"function", "custom"}
DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")
MAX_TOKENS = 1_000_000_000  # a larger count is not a count


def op_for(path: str, method: str) -> str | None:
    """ "chat" for a chat path, the read kind for a read path, else None. Only a POST is."""
    if method != "POST":
        return None
    clean = path.split("?", 1)[0].rstrip("/")
    if clean.endswith(CHAT_PATHS):
        return "chat"
    for suffix, op in READ_PATHS.items():
        if clean.endswith(suffix):
            return op
    return None


def is_responses(path: str) -> bool:
    """A Responses API call, not a chat completion."""
    return path.split("?", 1)[0].rstrip("/").endswith("/responses")


def broken_body(path: str, op: str) -> bytes:
    """bad body's bytes for this call. A moderation or a token count gets a chat reply's."""
    if op == "embeddings":
        return BROKEN_EMBEDDING
    if is_responses(path):
        return BROKEN_RESPONSE
    return BROKEN_BODY


def error_body(status: int) -> bytes:
    error = {
        "message": MESSAGES.get(status, f"The request failed with status {status}."),
        "type": TYPES.get(status, "server_error" if status >= 500 else "invalid_request_error"),
        "param": None,
        "code": CODES.get(status),
    }
    if status in TEXT_PLAIN:
        return json.dumps({"error": error, "status": status}).encode()
    return json.dumps({"error": error}).encode()


def error_type(status: int) -> str:
    """The content type of an error reply with this status."""
    return "text/plain" if status in TEXT_PLAIN else "application/json"


def context_too_long_body() -> bytes:
    """What OpenAI sends, with status 400, for a chat request longer than the context."""
    return json.dumps({"error": CONTEXT_TOO_LONG}).encode()


# A real gpt-4o-mini reply, saved on 6 November 2026 (tests/fixtures/openai/real_chat_answer.json
# and real_chat_tool_call.json): every field a built reply has, in the real order. The message,
# the ids, the time, the model and the counts are put in for each reply.
CHAT_REPLY: dict[str, object] = {
    "id": "",
    "object": "chat.completion",
    "created": 0,
    "model": "",
    "choices": [],
    "usage": {},
    "service_tier": "default",
    "system_fingerprint": "fp_9c7097be58",
}
USAGE_DETAILS: dict[str, dict[str, int]] = {
    "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
    "completion_tokens_details": {
        "reasoning_tokens": 0,
        "audio_tokens": 0,
        "accepted_prediction_tokens": 0,
        "rejected_prediction_tokens": 0,
    },
}
BUILT_OUTPUT_TOKENS = 10  # a built reply's output count: about its own length
# A refusal under Structured Outputs is its own field, with OpenAI's example text from the
# Structured Outputs guide; on a plain request a real refusal is ordinary text, as seen from
# gpt-4o-mini in a forum report of 26 August 2024.
STRUCTURED_REFUSAL = "I'm sorry, I cannot assist with that request."
PLAIN_REFUSAL = "I'm sorry, I can't assist with that."
# The tool OpenAI's models have called though no agent offers it, reported from November
# 2023 to July 2024. Its arguments name the offered tool it meant, prefixed with functions.
PARALLEL_TOOL = "multi_tool_use.parallel"
ID_LETTERS = string.ascii_letters + string.digits
CHAT_ID_LENGTH = 29  # after chatcmpl-, as in real ids
CALL_ID_LENGTH = 24  # after call_


def made_up_id(prefix: str, length: int) -> str:
    """An id in OpenAI's form. secrets, so the agent's own random sequence is left alone."""
    return prefix + "".join(secrets.choice(ID_LETTERS) for _ in range(length))


def structured(fields: dict[str, object]) -> bool:
    """A chat request that asks for Structured Outputs: a response_format of type json_schema."""
    return _obj(fields.get("response_format")).get("type") == "json_schema"


def chat_reply(
    fields: dict[str, object], tokens_in: int, message: dict[str, object], finish: str
) -> bytes:
    """A whole chat completion holding this one message, as a real one is laid out."""
    reply = dict(CHAT_REPLY)
    reply.update(
        id=made_up_id("chatcmpl-", CHAT_ID_LENGTH),
        created=int(time.time()),
        model=model_name(fields) or "",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", **message, "annotations": list[object]()},
                "logprobs": None,
                "finish_reason": finish,
            }
        ],
        usage={
            "prompt_tokens": tokens_in,
            "completion_tokens": BUILT_OUTPUT_TOKENS,
            "total_tokens": tokens_in + BUILT_OUTPUT_TOKENS,
            **USAGE_DETAILS,
        },
    )
    return json.dumps(reply, ensure_ascii=False, indent=2).encode()


def refusal_reply(fields: dict[str, object], tokens_in: int) -> bytes:
    """refuse: the refusal field under Structured Outputs, else plain text. Either way the
    model stopped, so finish_reason is stop."""
    if structured(fields):
        message: dict[str, object] = {"content": None, "refusal": STRUCTURED_REFUSAL}
    else:
        message = {"content": PLAIN_REFUSAL, "refusal": None}
    return chat_reply(fields, tokens_in, message, "stop")


# A real gpt-4o-mini Responses reply, saved on 18 November 2026
# (tests/fixtures/openai/real_response_answer.json): every field a built reply has, in the real
# order. The output, the ids, the times, the model and the counts are put in for each reply.
# A field that echoes a request takes the request's value when the request sets it, as a
# real reply does; tools is left empty, as a reply writes each tool in its own longer form.
RESPONSE_REPLY: dict[str, object] = {
    "id": "",
    "object": "response",
    "created_at": 0,
    "status": "completed",
    "access_programs": None,
    "background": False,
    "billing": {"payer": "developer"},
    "completed_at": 0,
    "error": None,
    "frequency_penalty": 0.0,
    "image": {"context_compression": "disabled"},
    "incomplete_details": None,
    "input_transformations": [],
    "instructions": None,
    "max_output_tokens": None,
    "max_tool_calls": None,
    "model": "",
    "moderation": None,
    "output": [],
    "parallel_tool_calls": True,
    "presence_penalty": 0.0,
    "previous_response_id": None,
    "prompt_cache_key": None,
    "prompt_cache_retention": "in_memory",
    "reasoning": {"context": None, "effort": None, "summary": None},
    "safety_identifier": None,
    "service_tier": "default",
    "store": True,
    "temperature": 1.0,
    "text": {"format": {"type": "text"}, "verbosity": "medium"},
    "tool_choice": "auto",
    "tool_usage": {
        "image_gen": {
            "input_tokens": 0,
            "input_tokens_details": {"image_tokens": 0, "text_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"image_tokens": 0, "text_tokens": 0},
            "total_tokens": 0,
        },
        "web_search": {"num_requests": 0},
    },
    "tools": [],
    "top_logprobs": 0,
    "top_p": 1.0,
    "truncation": "disabled",
    "usage": {},
    "user": None,
    "metadata": {},
}
RESPONSE_ECHOED = (
    "instructions",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "parallel_tool_calls",
    "previous_response_id",
    "prompt_cache_key",
    "safety_identifier",
    "store",
    "temperature",
    "tool_choice",
    "top_logprobs",
    "top_p",
    "truncation",
    "user",
)
# Echoed as objects: the request's keys over the reply's own, such as text's verbosity.
RESPONSE_MERGED = ("reasoning", "text")
ITEM_PREFIXES = {"message": "msg_", "function_call": "fc_"}  # the item ids a reply makes
RESPONSE_ID_LENGTH = 50  # after resp_ and msg_, in lowercase hex, as in real ids


def made_up_hex_id(prefix: str) -> str:
    return prefix + secrets.token_hex(RESPONSE_ID_LENGTH // 2)


def structured_response(fields: dict[str, object]) -> bool:
    """A Responses request that asks for Structured Outputs: a text.format of type json_schema."""
    return _obj(_obj(fields.get("text")).get("format")).get("type") == "json_schema"


def response_refusal_reply(fields: dict[str, object], tokens_in: int) -> bytes:
    """refuse on Responses: a refusal content part under Structured Outputs, else plain text,
    in one completed assistant message."""
    if structured_response(fields):
        part: dict[str, object] = {"type": "refusal", "refusal": STRUCTURED_REFUSAL}
    else:
        part = {"type": "output_text", "annotations": [], "logprobs": [], "text": PLAIN_REFUSAL}
    now = int(time.time())
    reply = dict(RESPONSE_REPLY)
    reply.update({key: fields[key] for key in RESPONSE_ECHOED if fields.get(key) is not None})
    for key in RESPONSE_MERGED:
        if asked := _obj(fields.get(key)):
            reply[key] = {**cast("dict[str, object]", reply[key]), **asked}
    reply.update(
        id=made_up_hex_id("resp_"),
        created_at=now,
        completed_at=now,
        model=model_name(fields) or "",
        output=[
            {
                "id": made_up_hex_id("msg_"),
                "type": "message",
                "status": "completed",
                "content": [part],
                "role": "assistant",
            }
        ],
        usage={
            "input_tokens": tokens_in,
            "input_tokens_details": {
                "cache_write_tokens": 0,
                "cache_write_tokens_details": {"ttl_12h_tokens": 0, "ttl_30m_tokens": 0},
                "cached_tokens": 0,
            },
            "output_tokens": BUILT_OUTPUT_TOKENS,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": tokens_in + BUILT_OUTPUT_TOKENS,
        },
    )
    return json.dumps(reply, ensure_ascii=False, indent=2).encode()


def response_ids(reply: bytes) -> list[str]:
    """The ids of a Responses reply and of its output items: a later request may name the
    reply as previous_response_id, and an item in an item_reference."""
    value = _obj(request_fields(reply))
    found = [value.get("id"), *(_obj(item).get("id") for item in _list(value.get("output")))]
    return [i for i in found if isinstance(i, str) and i]


def references(fields: dict[str, object]) -> list[str]:
    """What a Responses request asks OpenAI to look up: its previous_response_id, and the id
    of each item_reference in its input."""
    found = [fields.get("previous_response_id")]
    found += [
        _obj(item).get("id")
        for item in _list(fields.get("input"))
        if _obj(item).get("type") == "item_reference"
    ]
    return [i for i in found if isinstance(i, str)]


def tool_call_tools(path: str, fields: dict[str, object]) -> list[str]:
    """The tools a chat request offers in tools, the field tool_calls answer. The old
    functions field gets a function_call reply instead, never a tool call."""
    return offered(path, {k: v for k, v in fields.items() if k != "functions"}).agent


def no_free_tool_call(path: str, fields: dict[str, object]) -> str | None:
    """Why the model could not pick a tool of its own on this request, or None when it could:
    unknown tool fires only where a real model chose a tool no one offered."""
    if not tool_call_tools(path, fields):
        if _list(fields.get("functions")):
            return (
                "the request offers tools in the old functions field only, which a model "
                "answers with function_call, not tool_calls"
            )
        return "the request offers no tools, so a model would call none"
    choice = fields.get("tool_choice")
    if choice == "none":
        return 'the request sets tool_choice "none", so a model would call no tool'
    if _obj(choice).get("type") in ("function", "custom"):
        return "the request's tool_choice names the tool to call, so a model would call that one"
    return None


def unknown_tool_reply(fields: dict[str, object], tokens_in: int, tool: str) -> bytes:
    """unknown tool: one call to multi_tool_use.parallel, naming the tool the model meant."""
    use: dict[str, object] = {"recipient_name": f"functions.{tool}", "parameters": {}}
    uses = {"tool_uses": [use]}
    call = {
        "id": made_up_id("call_", CALL_ID_LENGTH),
        "type": "function",
        "function": {"name": PARALLEL_TOOL, "arguments": json.dumps(uses, separators=(",", ":"))},
    }
    message: dict[str, object] = {"content": None, "tool_calls": [call], "refusal": None}
    return chat_reply(fields, tokens_in, message, "tool_calls")


@dataclass(frozen=True)
class Given:
    """What a faulted chat reply gave the agent, in the forms a request built from it carries
    back. ids: the reply's tool call ids, made fresh for each reply, so no other conversation
    holds them. prefixes: for each choice, the faulted request's messages and then the
    message the agent got, with None and empty values left out, kept as their count and
    digest. A run that faults every call keeps a few bytes per fault, not the conversation."""

    ids: frozenset[str] = frozenset()
    prefixes: frozenset[tuple[int, bytes]] = frozenset()


def given(fields: dict[str, object], reply: bytes, *, ids_only: bool) -> Given:
    """What this reply to this request gave the agent. ids_only: the tool call ids alone, for
    a reply whose message any model may send, such as a call to multi_tool_use.parallel."""
    messages = [
        _obj(_obj(choice).get("message"))
        for choice in _list(_obj(request_fields(reply)).get("choices"))
    ]
    ids = frozenset(
        found
        for message in messages
        for call in _list(message.get("tool_calls"))
        if isinstance(found := _obj(call).get("id"), str) and found
    )
    if ids_only:
        return Given(ids)
    asked = [_normal(item) for item in _list(fields.get("messages"))]
    prefixes = frozenset(_digest([*asked, _normal(message)]) for message in messages if message)
    return Given(ids, prefixes)


def carries(fields: dict[str, object], kept: Sequence[Given]) -> bool:
    """Whether a chat request's messages carry what any of these faulted replies gave: an
    assistant tool call or a tool result with one of its tool call ids, or messages that
    start with a faulted request's messages and its reply's message. The request is hashed
    once, message by message, so the check grows with the request, not with the faults. An
    id that is not a string is the agent's own mistake: it matches nothing, and the request
    goes on to the provider, which answers it."""
    if not kept:
        return False
    ids = frozenset[str]().union(*(gave.ids for gave in kept))
    messages = [_obj(item) for item in _list(fields.get("messages"))]
    for message in messages:
        calls = [_obj(call).get("id") for call in _list(message.get("tool_calls"))]
        if message.get("role") == "assistant" and any(_among(i, ids) for i in calls):
            return True
        if message.get("role") == "tool" and _among(message.get("tool_call_id"), ids):
            return True
    wanted = frozenset[tuple[int, bytes]]().union(*(gave.prefixes for gave in kept))
    counts = {count for count, _ in wanted}
    if not counts:
        return False
    sent = (_normal(message) for message in messages[: max(counts)])
    return any(found in wanted for found in _running(sent, counts))


def _among(found: object, ids: frozenset[str]) -> bool:
    """Whether a tool call id a request sends is one of these. A list or an object as an id
    cannot be looked up in a set, and is never one of them."""
    return isinstance(found, str) and found in ids


def _digest(prefix: list[object]) -> tuple[int, bytes]:
    """The count and digest of these messages, as a request that starts with them gets."""
    *_, last = _running(prefix, {len(prefix)})
    return last


def _running(messages: Iterable[object], counts: set[int]) -> Iterator[tuple[int, bytes]]:
    """For each count in counts, the count and the sha256 of the canonical JSON of the list
    of the first that many messages. One hash is built message by message, so a long
    request is read once. A key order or a space does not change it; a value does."""
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
    """A message as a later request may write it again: None, empty and missing are alike, as
    a client that drops None fields sends them."""
    if isinstance(value, dict):
        kept = {str(k): _normal(v) for k, v in cast(dict[object, object], value).items()}
        return {k: v for k, v in kept.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_normal(item) for item in cast(list[object], value)]
    return value


def request_fields(body: bytes) -> dict[str, object] | None:
    """A request's JSON fields, or None when the body cannot be read as a JSON object."""
    try:
        value: object = json.loads(body)
    except (ValueError, RecursionError):  # not JSON, not text, or empty
        return None
    return _obj(value) or None


def model_name(fields: dict[str, object] | None) -> str | None:
    """The model a request names, or None when it names none that can be read."""
    value = (fields or {}).get("model")
    return value if isinstance(value, str) and value else None


def max_output(fields: dict[str, object]) -> int | None:
    for key in ("max_completion_tokens", "max_tokens", "max_output_tokens"):
        value = fields.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_TOKENS:
            return value
    return None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    known: bool = False
    cached: int = 0  # input tokens the provider served from its prompt cache
    written_30m: int = 0  # input tokens written to the cache for 30 minutes
    written_12h: int = 0  # and for 12 hours


def usage(body: bytes, *, output_required: bool = True) -> Usage:
    """Token counts from a reply: a JSON body, or the events of a streamed one.

    A stream's lines may start with "event:" or be a ": ping", so only "data:" lines are
    read. Counts are trusted only when they are sensible whole numbers. An embedding has
    no output count; anything else without one has no usable counts. A cached count that
    is not a sensible number up to the input count is read as 0, which prices it high.
    """
    text = body.decode("utf-8", "replace")
    # Split on newlines only: text in a reply can hold other line separators.
    data = [line[5:].strip() for line in text.split("\n") if line.startswith("data:")]
    found = Usage()
    for doc in data or [text]:
        try:
            parsed = _obj(json.loads(doc))
        except (ValueError, RecursionError):
            continue
        numbers = _obj(parsed.get("usage"))
        if not numbers:  # the Responses API puts it in its last event's response
            numbers = _obj(_obj(parsed.get("response")).get("usage"))
        tokens_in = _count(numbers, "prompt_tokens", "input_tokens")
        tokens_out = _count(numbers, "completion_tokens", "output_tokens")
        if tokens_out is None and not output_required:
            tokens_out = 0
        if tokens_in is not None and tokens_out is not None:
            cached = cached_count(numbers, tokens_in)
            found = Usage(tokens_in, tokens_out, True, cached)
            found.written_30m, found.written_12h = written(numbers, tokens_in - cached)
    return found


def _details(numbers: dict[str, object]) -> dict[str, object]:
    """Chat names the block prompt_tokens_details, Responses input_tokens_details. A Chat
    block that is not a table, such as a null beside a filled Responses one, is passed over."""
    for key in ("prompt_tokens_details", "input_tokens_details"):
        block = numbers.get(key)
        if isinstance(block, dict):
            return _obj(cast(object, block))
    return {}


def cached_count(numbers: dict[str, object], tokens_in: int) -> int:
    cached = _count(_details(numbers), "cached_tokens", "cached_tokens")
    return cached if cached is not None and cached <= tokens_in else 0


def written(numbers: dict[str, object], uncached: int) -> tuple[int, int]:
    """The input tokens written to the cache, for 30 minutes and for 12 hours.

    OpenAI bills each input token once: as plain input, as a cached read or as a write, so
    the writes are a part of the input tokens that were not cached. Responses splits them
    by lifetime, ttl_30m_tokens and ttl_12h_tokens, which sum to cache_write_tokens; Chat
    Completions writes for 30 minutes only and gives no split. OpenAI states the relation
    for Responses; Chat's cache_write_tokens is read the same way, as its SDK field names
    it, but no real Chat reply with writes has confirmed it. No write count is no write.
    A count that cannot be trusted is read high: every uncached token written for 12 hours.
    So is a split with no total, which says writes were made but not how many.
    """
    details = _details(numbers)
    if "cache_write_tokens" not in details:
        return (0, uncached) if "cache_write_tokens_details" in details else (0, 0)
    total = _count(details, "cache_write_tokens", "cache_write_tokens")
    if total is None or total > uncached:
        return 0, uncached
    split = _obj(details.get("cache_write_tokens_details"))
    if "ttl_12h_tokens" not in split:
        return total, 0
    long = _count(split, "ttl_12h_tokens", "ttl_12h_tokens")
    if long is None or long > total:
        return 0, total
    return total - long, long


def price(model: str, overrides: dict[str, Rates]) -> Rates | None:
    """The team's price first, whole, then the built-in table.

    A dated snapshot, such as gpt-4o-mini-2024-07-18, uses its family's price. Nothing
    else does: gpt-4o-audio-preview is not priced like gpt-4o.
    """
    family = DATED.sub("", model)
    for name in (model, family):
        for table in (overrides, PRICES):
            if name in table:
                return table[name]
    return None


def cost(
    tokens_in: int,
    tokens_out: int,
    rates: Rates,
    cached: int = 0,
    written_30m: int = 0,
    written_12h: int = 0,
) -> float:
    """What a call costs. cached and the written counts are parts of tokens_in."""
    plain = tokens_in - cached - written_30m - written_12h
    full = plain * rates.input + cached * _or_input(rates.cached_input, rates)
    full += written_30m * _or_input(rates.cache_write_30m, rates)
    full += written_12h * _or_input(rates.cache_write_12h, rates)
    return (full + tokens_out * rates.output) / 1_000_000


def most_cost(tokens_in: int, tokens_out: int, rates: Rates, *, responses: bool) -> float:
    """What a call could cost at most: every input token at the input or cache-write rate,
    whichever is dearer, since the provider may write any of them to its cache. Only the
    Responses API can write for 12 hours; Chat Completions writes for 30 minutes."""
    longest = rates.cache_write_12h if responses else None
    rate = max(rates.input, rates.cache_write_30m or 0.0, longest or 0.0)
    return (tokens_in * rate + tokens_out * rates.output) / 1_000_000


def _or_input(rate: float | None, rates: Rates) -> float:
    return rates.input if rate is None else rate


@dataclass
class Offered:
    """The tools a model request offers, sorted."""

    agent: list[str] = field(default_factory=list[str])  # names the agent runs itself
    hosted_reads: list[str] = field(default_factory=list[str])  # provider tools that only read
    hosted_writes: list[str] = field(default_factory=list[str])  # MCP server labels
    unknown: list[str] = field(default_factory=list[str])  # tool types not in the table


def offered(path: str, fields: dict[str, object]) -> Offered:
    found = Offered()
    responses = path.split("?", 1)[0].rstrip("/").endswith("/responses")
    if not responses:
        found.agent += [_name(_obj(f)) for f in _list(fields.get("functions"))]  # the old form
        if fields.get("web_search_options") is not None:
            found.hosted_reads.append("web_search")
    for raw in _list(fields.get("tools")):
        tool = _obj(raw)
        kind = str(tool.get("type") or "(no type)")
        if not responses:
            if kind in CHAT_AGENT:
                found.agent.append(_name(_obj(tool.get(kind)) or tool))
            else:
                found.unknown.append(kind)
            continue
        if kind in RESPONSES_READ:
            found.hosted_reads.append(kind)
            continue
        if kind == "mcp":
            found.hosted_writes.append(str(tool.get("server_label", "")))
        elif kind == "tool_search":
            if tool.get("execution") == "client":
                found.agent.append(_name(tool))
            else:
                found.hosted_reads.append(kind)
        elif kind == "namespace":
            found.agent += [_name(_obj(inner)) for inner in _list(tool.get("tools"))]
        elif kind in RESPONSES_AGENT:
            found.agent.append(_name(tool))
        else:
            found.unknown.append(kind)
    return found


def _name(tool: dict[str, object]) -> str:
    """A built-in tool such as shell has no name of its own: its type is the name."""
    return str(tool.get("name") or tool.get("type", ""))


def _obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def _list(value: object) -> list[object]:
    return list(cast(list[object], value)) if isinstance(value, list) else []


def _count(numbers: dict[str, object], in_chat: str, in_responses: str) -> int | None:
    """A token count, or None when the reply has none or it is not a sensible number."""
    value = numbers.get(in_chat, numbers.get(in_responses))
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKENS:
        return None
    return value
