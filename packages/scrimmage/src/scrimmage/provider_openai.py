"""OpenAI's address table, error bodies, token counts, hosted tools and prices.

Pure functions of a request's host, path and body, or of a reply's body. The gate uses
them to label a call, build a fault the openai library reads as a real error, count
tokens and sort the tools the model is offered. A built reply, for refuse and unknown tool,
also takes the time and new ids, as a real one has.
"""

import dataclasses
import json
import math
import re
import string
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from scrimmage import prices
from scrimmage.judge import normal
from scrimmage.prices import Rates as Rates
from scrimmage.providers.base import Fields as Fields
from scrimmage.providers.base import Given as Given
from scrimmage.providers.base import Offered as Offered
from scrimmage.providers.base import Reply as Reply
from scrimmage.providers.base import Usage as Usage
from scrimmage.providers.impl.marks import among, digest, normalised, running
from scrimmage.providers.impl.wire import fields_dict, reply_value, wrap_fields

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
MAX_TOKENS = 1_000_000_000  # a larger count is not a count
MAX_CHOICES = 128  # the most choices, n, a Chat request may ask for
WRITE_30M = "cache_write_30m"  # the Rates fields that price a cache write, by its lifetime
WRITE_12H = "cache_write_12h"


@dataclass
class OpenAIUsage(Usage):
    """Usage, with OpenAI's two cache lifetimes named, which its tests and docs speak of."""

    @property
    def written_30m(self) -> int:
        return self.writes.get(WRITE_30M, 0)

    @property
    def written_12h(self) -> int:
        return self.writes.get(WRITE_12H, 0)


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


def is_chat_completions(path: str) -> bool:
    """A Chat Completions call, which is billed for each choice it asks for."""
    return path.split("?", 1)[0].rstrip("/").endswith("/chat/completions")


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


def error_content_type(status: int) -> str:
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
ID_LETTERS = string.ascii_letters + string.digits
CHAT_ID_LENGTH = 29  # after chatcmpl-, as in real ids
CALL_ID_LENGTH = 24  # after call_


def made_up_id(prefix: str, length: int) -> str:
    """An id in OpenAI's form. secrets, so the agent's own random sequence is left alone."""
    import secrets  # not at import: only a made-up reply needs it, and it pulls in random

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
        model=name_in(fields) or "",
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


def refusal_reply(wire: Fields, tokens_in: int) -> bytes:
    """refuse: the refusal field under Structured Outputs, else plain text. Either way the
    model stopped, so finish_reason is stop."""
    fields = fields_dict(wire)
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
    import secrets

    return prefix + secrets.token_hex(RESPONSE_ID_LENGTH // 2)


def structured_response(fields: dict[str, object]) -> bool:
    """A Responses request that asks for Structured Outputs: a text.format of type json_schema."""
    return _obj(_obj(fields.get("text")).get("format")).get("type") == "json_schema"


def response_refusal_reply(wire: Fields, tokens_in: int) -> bytes:
    """refuse on Responses: a refusal content part under Structured Outputs, else plain text,
    in one completed assistant message."""
    fields = fields_dict(wire)
    if structured_response(fields):
        part: dict[str, object] = {"type": "refusal", "refusal": STRUCTURED_REFUSAL}
    else:
        part = {"type": "output_text", "annotations": [], "logprobs": [], "text": PLAIN_REFUSAL}

    def message() -> dict[str, object]:
        return {
            "id": made_up_hex_id("msg_"),
            "type": "message",
            "status": "completed",
            "content": [part],
            "role": "assistant",
        }

    return response_reply(fields, tokens_in, message)


def response_reply(
    fields: dict[str, object], tokens_in: int, make_item: Callable[[], dict[str, object]]
) -> bytes:
    """A whole completed Responses reply whose output is the one item make_item makes. The reply's
    id is made before the item's."""
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
        model=name_in(fields) or "",
        output=[make_item()],
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
    value = _obj(fields_dict(request_fields(reply)))
    found = [value.get("id"), *(_obj(item).get("id") for item in _list(value.get("output")))]
    return [i for i in found if isinstance(i, str) and i]


def refusal_request_problem(form: str | None, path: str, wire: Fields) -> str | None:
    """OpenAI's builder makes a refusal for every request, so none is a mistake here."""
    del form, path, wire
    return None


def refusal_form_problem(form: str) -> str | None:
    """OpenAI's refuse has one form, so a form is a mistake in the test whatever it says."""
    del form
    return (
        "is an OpenAI call, and refuse has one form here, so form does not apply; remove form "
        "from this row"
    )


def never_carried_out(word: str) -> str | None:
    """OpenAI's replies can take every fault word, so none is a mistake whatever the request."""
    del word
    return None


IN_INPUT = "send the conversation in input instead, as the OpenAI Agents SDK does by default"


def not_carried_out(word: str, path: str, wire: Fields) -> str | None:
    """Why a reply fault, or context too long, cannot be carried out on this request: the
    mistake in the test. Only Responses has such cases."""
    fields = fields_dict(wire)
    if not is_responses(path):
        return None
    if word == "context too long":
        return (
            "is a Responses call, and context too long sends Chat Completions' error; "
            "fault a chat call, or use a status on this one"
        )
    if fields.get("conversation") is not None:
        return (
            f"is a Responses call in a conversation OpenAI keeps, and {word} would leave the "
            f"real reply, or none, in it for the next turn; {IN_INPUT}"
        )
    return None


def chain_ids(path: str, reply: bytes) -> list[str]:
    """The ids a later request may chain to, for a reply to this path: Responses keeps its
    replies; Chat Completions keeps none."""
    return response_ids(reply) if is_responses(path) else []


def chain_references(path: str, wire: Fields) -> list[str]:
    """What a request to this path asks OpenAI to look up by id."""
    fields = fields_dict(wire)
    return references(fields) if is_responses(path) else []


def chain_broken(found: str) -> str:
    """Why a request that looks up this faulted id is a mistake in the test, said of the call."""
    return (
        f"asks OpenAI to look up {found}, a reply or an item a fault changed or built, and "
        f"OpenAI holds nothing under that id; {IN_INPUT}"
    )


# A part a change to a Chat Completions reply names: a choice's text, or a tool call's arguments.
CHOICE_PART = re.compile(
    r"choices\[(\d+)\]\.message\.(?:content|tool_calls\[(\d+)\]\.function\.arguments)"
)
FIRST_TEXT = "choices[0].message.content"


def parse_reply(data: bytes) -> Reply:
    """A recorded reply's bytes, parsed. A reply that is not JSON reads as nothing."""
    try:
        return Reply(json.loads(data.decode("utf-8", "replace")))
    except ValueError:
        return Reply(None)


def called_tool(wire: Reply) -> str | None:
    """The name of the first tool a chat completion calls."""
    reply = reply_value(wire)
    for choice in _list(_obj(reply).get("choices")):
        calls = _list(_obj(_obj(choice).get("message")).get("tool_calls"))
        name = _obj(_obj(calls[0] if calls else None).get("function")).get("name")
        if isinstance(name, str):
            return name
    return None


def said(wire: Reply, parts: Sequence[object]) -> str | None:
    """The string at the first of these parts a change named, such as
    choices[0].message.content, or at the first choice's text if it named none: a message's
    content, or a tool call's arguments. None when the reply holds no string there."""
    reply = reply_value(wire)
    part = next((str(p) for p in parts if CHOICE_PART.fullmatch(str(p))), FIRST_TEXT)
    found = CHOICE_PART.fullmatch(part)
    choices = _list(_obj(reply).get("choices"))
    if found is None or not isinstance(_obj(reply).get("choices"), list):
        return None
    index = int(found.group(1))
    message = _obj(_obj(choices[index] if index < len(choices) else None).get("message"))
    if not message:
        return None
    value: object = message.get("content")
    if found.group(2) is not None:
        calls = _list(message.get("tool_calls"))
        call = int(found.group(2))
        function = _obj(_obj(calls[call] if call < len(calls) else None).get("function"))
        value = function.get("arguments")
    return value if isinstance(value, str) else None


def carries_text(wire: Fields, texts: Sequence[str]) -> bool:
    """Whether a chat request holds a text a fault put in a result. Only the strings of the
    parsed body are searched, each message's content as text or as parts, compared as a rule
    compares text; the raw body would hold the text escaped. A heuristic."""
    fields = fields_dict(wire)
    messages = _list(fields.get("messages"))
    if not messages:
        return False
    wanted = [normal(text) for text in texts]
    for message in messages:
        content = _obj(message).get("content")
        parts = _list(content) or [content]
        for part in parts:
            text = part if isinstance(part, str) else _obj(part).get("text")
            if isinstance(text, str) and any(w in normal(text) for w in wanted):
                return True
    return False


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


def conversation_key(path: str, wire: Fields) -> str | None:
    """The system and developer messages, or a Responses request's instructions, and the first
    user message: what every turn of one conversation sends again. None when there is no user
    message, or a Responses request continues by id, with previous_response_id or a
    conversation, so it carries neither."""
    fields = fields_dict(wire)
    if is_responses(path):
        if fields.get("previous_response_id") is not None or fields.get("conversation") is not None:
            return None
        items = fields.get("input")
        messages = [{"role": "user", "content": items}] if isinstance(items, str) else _list(items)
        lead: list[object] = [] if fields.get("instructions") is None else [fields["instructions"]]
    else:
        messages = _list(fields.get("messages"))
        lead = []
    lead += [m for m in messages if _obj(m).get("role") in ("system", "developer")]
    first = next((m for m in messages if _obj(m).get("role") == "user"), None)
    if first is None:
        return None
    return digest([normalised(lead), normalised(first)])[1].hex()


def tool_call_tools(path: str, wire: Fields) -> list[str]:
    """The tools a chat request offers in tools, the field tool_calls answer. The old
    functions field gets a function_call reply instead, never a tool call."""
    fields = fields_dict(wire)
    return offered(path, wrap_fields({k: v for k, v in fields.items() if k != "functions"})).agent


def no_free_tool_call(path: str, wire: Fields) -> str | None:
    """Why the model could not pick a tool of its own on this request, or None when it could:
    unknown tool fires only where a real model chose a tool no one offered."""
    fields = fields_dict(wire)
    if not tool_call_tools(path, wire):
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


def unknown_tool_reply(path: str, wire: Fields, tokens_in: int, tool: str) -> bytes:
    """unknown tool: one call to this tool, which the request does not offer, with no arguments:
    a tool call on Chat Completions (tests/fixtures/openai/real_chat_tool_call.json), a
    function_call item on Responses (real_response_function_call_truncated.json)."""
    fields = fields_dict(wire)
    if is_responses(path):

        def function_call() -> dict[str, object]:
            return {
                "id": made_up_hex_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "arguments": "{}",
                "call_id": made_up_id("call_", CALL_ID_LENGTH),
                "name": tool,
            }

        return response_reply(fields, tokens_in, function_call)
    call = {
        "id": made_up_id("call_", CALL_ID_LENGTH),
        "type": "function",
        "function": {"name": tool, "arguments": "{}"},
    }
    message: dict[str, object] = {"content": None, "tool_calls": [call], "refusal": None}
    return chat_reply(fields, tokens_in, message, "tool_calls")


def given(wire: Fields, reply: bytes, *, ids_only: bool) -> Given:
    """What this reply to this request gave the agent. ids_only: the tool call ids alone, for
    a reply whose message any model may send, such as a call to multi_tool_use.parallel."""
    fields = fields_dict(wire)
    messages = [
        _obj(_obj(choice).get("message"))
        for choice in _list(_obj(fields_dict(request_fields(reply))).get("choices"))
    ]
    ids = frozenset(
        found
        for message in messages
        for call in _list(message.get("tool_calls"))
        if isinstance(found := _obj(call).get("id"), str) and found
    )
    if ids_only:
        return Given(ids)
    asked = [normalised(item) for item in _list(fields.get("messages"))]
    prefixes = frozenset(digest([*asked, normalised(message)]) for message in messages if message)
    return Given(ids, prefixes)


def response_calls(reply: bytes) -> Given:
    """The call ids of the function_call items a Responses reply holds: all that reply gave a
    later request to carry back, as OpenAI keeps the reply itself and names it by id."""
    output = _list(_obj(fields_dict(request_fields(reply))).get("output"))
    found = (
        _obj(item).get("call_id") for item in output if _obj(item).get("type") == "function_call"
    )
    return Given(frozenset(i for i in found if isinstance(i, str) and i))


def carries_response_call(wire: Fields, kept: Sequence[Given]) -> bool:
    """Whether a Responses request's input carries a function_call, or the output of one, with a
    call id a faulted reply gave. An id that is not a string matches nothing."""
    ids = frozenset[str]().union(*(gave.ids for gave in kept))
    items = (_obj(item) for item in _list(fields_dict(wire).get("input")))
    return any(
        item.get("type") in ("function_call", "function_call_output")
        and among(item.get("call_id"), ids)
        for item in items
    )


def carries(wire: Fields, kept: Sequence[Given]) -> bool:
    """Whether a chat request's messages carry what any of these faulted replies gave: an
    assistant tool call or a tool result with one of its tool call ids, or messages that
    start with a faulted request's messages and its reply's message. The request is hashed
    once, message by message, so the check grows with the request, not with the faults. An
    id that is not a string is the agent's own mistake: it matches nothing, and the request
    goes on to the provider, which answers it."""
    fields = fields_dict(wire)
    if not kept:
        return False
    ids = frozenset[str]().union(*(gave.ids for gave in kept))
    messages = [_obj(item) for item in _list(fields.get("messages"))]
    for message in messages:
        calls = [_obj(call).get("id") for call in _list(message.get("tool_calls"))]
        if message.get("role") == "assistant" and any(among(i, ids) for i in calls):
            return True
        if message.get("role") == "tool" and among(message.get("tool_call_id"), ids):
            return True
    wanted = frozenset[tuple[int, bytes]]().union(*(gave.prefixes for gave in kept))
    counts = {count for count, _ in wanted}
    if not counts:
        return False
    sent = (normalised(message) for message in messages[: max(counts)])
    return any(found in wanted for found in running(sent, counts))


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
    return name_in(fields_dict(wire))


def streams(wire: Fields | None) -> bool:
    return fields_dict(wire).get("stream") is True


def name_in(fields: dict[str, object]) -> str | None:
    value = fields.get("model")
    return value if isinstance(value, str) and value else None


def max_output(fields: dict[str, object]) -> int | None:
    for key in ("max_completion_tokens", "max_tokens", "max_output_tokens"):
        value = fields.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_TOKENS:
            return value
    return None


def output_cap(wire: Fields, path: str, default: int) -> int:
    """The most output tokens a call to this path could bill. A Chat request bills each
    choice, n, for its own output and its own limit; Responses has no n."""
    fields = fields_dict(wire)
    cap = max_output(fields) or default
    return cap * choices(fields) if is_chat_completions(path) else cap


def choices(fields: dict[str, object]) -> int:
    """How many choices a Chat request asks for. Each is billed for its own output, and
    each has its own output limit. A value the API refuses is never billed, so it is 1."""
    value = fields.get("n")
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_CHOICES:
        return value
    return 1


def usage(body: bytes, *, output_required: bool = True) -> OpenAIUsage:
    """Token counts from a reply: a JSON body, or the events of a streamed one.

    A stream's lines may start with "event:" or be a ": ping", so only "data:" lines are
    read. Counts are trusted only when they are sensible whole numbers. An embedding has
    no output count; anything else without one has no usable counts. A cached count that
    is not a sensible number up to the input count is read as 0, which prices it high.
    """
    text = body.decode("utf-8", "replace")
    # Split on newlines only: text in a reply can hold other line separators.
    data = [line[5:].strip() for line in text.split("\n") if line.startswith("data:")]
    found = OpenAIUsage()
    tier: str | None = None
    cost: float | None = None
    for doc in data or [text]:
        try:
            parsed = _obj(json.loads(doc))
        except (ValueError, RecursionError):
            continue
        named = parsed.get("service_tier", _obj(parsed.get("response")).get("service_tier"))
        if isinstance(named, str) and named:  # a stream's every chunk may carry it
            tier = named
        numbers = _obj(parsed.get("usage"))
        if not numbers:  # the Responses API puts it in its last event's response
            numbers = _obj(_obj(parsed.get("response")).get("usage"))
        cost = gateway_cost(numbers.get("cost"), cost)
        tokens_in = _count(numbers, "prompt_tokens", "input_tokens")
        tokens_out = _count(numbers, "completion_tokens", "output_tokens")
        if tokens_out is None and not output_required:
            tokens_out = 0
        if tokens_in is not None and tokens_out is not None:
            cached = cached_count(numbers, tokens_in)
            found = OpenAIUsage(tokens_in, tokens_out, True, cached)
            short, long = written(numbers, tokens_in - cached)
            found.writes = {WRITE_30M: short, WRITE_12H: long}
    found.tier, found.cost = tier, cost
    return found


def gateway_cost(value: object, before: float | None = None) -> float | None:
    """A cost in dollars a gateway put in its reply, such as OpenRouter's usage.cost, or the
    LiteLLM proxy's x-litellm-response-cost header, which is text. Anything that is not a
    finite number of at least 0 is no cost, and the call is priced from its tokens."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return before
    if isinstance(value, bool) or not isinstance(value, int | float):
        return before
    return float(value) if math.isfinite(value) and value >= 0 else before


def request_tier(wire: Fields | None) -> str | None:
    """The service tier a request names. auto leaves the choice to the project's setting, so
    it names none."""
    fields = fields_dict(wire)
    value = (fields or {}).get("service_tier")
    return value if isinstance(value, str) and value not in ("", "auto") else None


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


def snapshot_family(model: str) -> str:
    """A dated snapshot's family: OpenAI dates one -YYYY-MM-DD. The provider's one rule, read by
    the price lookup below and by the plan's dedupe of the price rows."""
    return prices.dashed_family(model)


# OpenAI renamed its fast tier priority, so a row that prices one prices both. Rates.at reads this
# off the row, which is the only row it is ever used on: another provider has no such alias.
TIER_ALIASES = (("fast", "priority"), ("priority", "fast"))


def price(model: str, overrides: dict[str, Rates]) -> Rates | None:
    """The team's price first, whole, then the shipped table: see prices.price. The row comes
    with OpenAI's tier aliases."""
    found = prices.priced("openai", model, overrides, family=snapshot_family)
    return None if found is None else dataclasses.replace(found, aliases=TIER_ALIASES)


def cost(
    tokens_in: int,
    tokens_out: int,
    rates: Rates,
    cached: int = 0,
    written_30m: int = 0,
    written_12h: int = 0,
) -> float:
    """What a call costs. cached and the written counts are parts of tokens_in. A call over the
    long-context threshold costs every token at the long rates, not only those past it."""
    rates = rates.sized(tokens_in)
    plain = tokens_in - cached - written_30m - written_12h
    full = plain * rates.input + cached * _or_input(rates.cached_input, rates)
    full += written_30m * _or_input(rates.cache_write_30m, rates)
    full += written_12h * _or_input(rates.cache_write_12h, rates)
    return (full + tokens_out * rates.output) / 1_000_000


def dearest(
    rates: Rates, tokens_in: int, tokens_out: int, cached: int, written_30m: int, written_12h: int
) -> Rates:
    """Of the default rates and every tier the row prices, the ones this call costs most at,
    long rates included at the call's size: for a reply that names a tier the row does not price."""
    every = (rates, *(tier for _, tier in rates.tiers))
    return max(
        every, key=lambda r: cost(tokens_in, tokens_out, r, cached, written_30m, written_12h)
    )


def most_cost(tokens_in: int, tokens_out: int, rates: Rates, *, responses: bool) -> float:
    """What a call could cost at most: every input token at the input or cache-write rate,
    whichever is dearer, since the provider may write any of them to its cache. Only the
    Responses API can write for 12 hours; Chat Completions writes for 30 minutes. An input over
    the long-context threshold is held at the long rates."""
    rates = rates.sized(tokens_in)
    longest = rates.cache_write_12h if responses else None
    rate = max(rates.input, rates.cache_write_30m or 0.0, longest or 0.0)
    return (tokens_in * rate + tokens_out * rates.output) / 1_000_000


def _or_input(rate: float | None, rates: Rates) -> float:
    return rates.input if rate is None else rate


def offered(path: str, wire: Fields) -> Offered:
    fields = fields_dict(wire)
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
    """A dict's items under string keys, or {} for anything else. A Fields or a Reply is no dict:
    handed one, the caller forgot fields_dict or reply_value, and {} would read it as empty."""
    if isinstance(value, Fields | Reply):
        raise TypeError("_obj was handed an opaque wire value; open it with fields_dict first")
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
