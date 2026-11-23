"""OpenAI's reply shapes: the change each carried fault makes to a Chat Completions reply and
to a Responses reply, as pure functions.

A model's reply is changed as its provider lays it out, so these live with the OpenAI provider.
Only `providers/impl/openai.py` calls them: every other module reaches a change through
`ModelProvider.change`. The helpers that do not depend on a shape, the web and text changes,
`rewritten`, `garbled`, `cut` and `broken_args`, stay in `changes`.
"""

import json
from collections.abc import Callable, Sequence
from typing import Protocol, cast

from scrimmage.changes import (
    Unchangeable,
    WebChanged,
    broken_args,
    change_text,
    cut,
    garbled,
    header,
    media_type,
    packed_why,
    rewritten,
    unpacked,
)
from scrimmage.config import Fault


def streamed(word: str) -> str:
    return (
        f"{word} on a model call needs a reply that is not streamed; turn streaming off for "
        "this model, or fault it another way"
    )


# The carried faults that change a Responses reply too. replace stays on Chat Completions.
ON_RESPONSES = ("garble", "truncate", "bad tool args")


def only_on_chat(word: str, op: str) -> Unchangeable:
    return Unchangeable(
        f"{word} on a model call works on Chat Completions only, and this call is {op}; "
        "fault it another way",
        "unsupported",
    )


def chat_kind(
    status: int, op: str, headers: Sequence[tuple[str, str]], word: str = "replace"
) -> Unchangeable | None:
    """None when a model reply's headers allow a change to its content, else why not. Decided
    by the headers alone, so a stream is never read to find out."""
    if op != "chat" and not (op == "responses" and word in ON_RESPONSES):
        return only_on_chat(word, op)
    kind = media_type(header(headers, "content-type") or "")
    if kind == "text/event-stream":
        return Unchangeable(streamed(word), "unsupported")
    if status != 200:
        return Unchangeable(f"the reply status is {status}, which is not changed", "data")
    packed = packed_why(header(headers, "content-encoding") or "")
    if packed is not None:
        return Unchangeable(packed)
    if kind != "application/json":
        return Unchangeable(f"the reply is {kind or 'of no type'}, not a chat completion")
    return None


def chat_reply(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    word: str,
    *,
    decoded: bool,
) -> tuple[dict[str, object], list[object]] | Unchangeable:
    """A chat completion, parsed, and its choices; or why it cannot be changed."""
    said = chat_kind(status, op, headers, word) if op == "chat" else only_on_chat(word, op)
    if said is not None:
        return said
    encoding = "" if decoded else (header(headers, "content-encoding") or "")
    plain_body = unpacked(body, encoding)
    if plain_body is None:
        return Unchangeable("the body will not unpack")
    try:
        value = json.loads(plain_body)
    except (ValueError, RecursionError):
        return Unchangeable("the body is not JSON that parses")
    choices = cast("dict[str, object]", value).get("choices") if isinstance(value, dict) else None
    if not isinstance(choices, list):
        return Unchangeable("the reply has no choices")
    return cast("dict[str, object]", value), cast("list[object]", choices)


def message_of(choice: object) -> dict[str, object] | None:
    held = cast("dict[str, object]", choice).get("message") if isinstance(choice, dict) else None
    return cast("dict[str, object]", held) if isinstance(held, dict) else None


def change_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Replace the text of each choice of a chat completion. A choice with no text, such as
    tool calls only or a refusal, keeps its place; a reply with no text anywhere is not
    changed. A model's text is changed as text, even when it looks like JSON."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    count, parts, missed = 0, list[str](), list[Unchangeable]()
    for i, choice in enumerate(choices):
        message = message_of(choice)
        text = None if message is None else message.get("content")
        if message is None or not isinstance(text, str) or not text:  # tool calls: often ""
            continue
        done = change_text(text, fault, f"choices[{i}].message.content", sniff=False)
        if isinstance(done, Unchangeable):
            missed.append(done)
            continue
        message["content"] = done.value
        count += done.count
        parts += done.parts
    if not count:
        if missed:
            return next((m for m in missed if m.mistake), missed[0])
        return Unchangeable(no_text(choices), "data")
    return rewritten(value, headers, count, parts)


def garble_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Put the stray token in each choice's text. Every other field stays as it was sent."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    count, parts = 0, list[str]()
    for i, choice in enumerate(choices):
        message = message_of(choice)
        text = None if message is None else message.get("content")
        if message is None or not isinstance(text, str):
            continue
        new, words = garbled(text)
        if words:
            message["content"] = new
            count += words
            parts.append(f"choices[{i}].message.content")
    if not count:
        return Unchangeable(no_text(choices), "data")
    return rewritten(value, headers, count, parts)


# OpenAI's reply, with status 400, when the token limit lands inside a tool call, saved on
# 18 November 2026: a cut tool call is never sent as one.
CUT_IN_A_TOOL_CALL: dict[str, object] = {
    "error": {
        "message": (
            "Could not finish the message because max_tokens or model output limit was "
            "reached. Please try again with higher max_tokens."
        ),
        "type": "invalid_request_error",
        "param": None,
        "code": None,
    }
}


def has_text(message: dict[str, object]) -> bool:
    text = message.get("content")
    return isinstance(text, str) and bool(text)  # tool calls often come with ""


def truncate_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Cut each choice's text as the token limit does, with finish_reason length. A choice cut
    in its text never reached its tool calls, so they go. When any choice holds only tool
    calls, the whole reply becomes the 400 OpenAI sends when the limit lands inside one, with
    tool_choice left to the model or required, as captured. usage stays as it was billed."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    messages = [(i, choice, message_of(choice)) for i, choice in enumerate(choices)]
    if any(m is not None and not has_text(m) and m.get("tool_calls") for _, _, m in messages):
        return rewritten(CUT_IN_A_TOOL_CALL, headers, 1, ["error"], 400)
    count, parts = 0, list[str]()
    for i, choice, message in messages:
        if message is None or not has_text(message):
            continue
        message["content"] = cut(cast(str, message["content"]))
        message.pop("tool_calls", None)
        cast("dict[str, object]", choice)["finish_reason"] = "length"
        count += 1
        parts.append(f"choices[{i}].message.content")
    if count:
        return rewritten(value, headers, count, parts)
    why = no_text(choices)
    if why == "the reply has no text":
        why = "the reply has no text and no tool call"
    return Unchangeable(why, "data")


def break_args_chat(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Break the arguments of every function tool call in every choice."""
    read = chat_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, choices = read
    count, parts = 0, list[str]()
    for i, choice in enumerate(choices):
        message = message_of(choice)
        held = None if message is None else message.get("tool_calls")
        for j, call in enumerate(cast("list[object]", held) if isinstance(held, list) else []):
            function = (
                cast("dict[str, object]", call).get("function") if isinstance(call, dict) else None
            )
            if not isinstance(function, dict):
                continue
            function = cast("dict[str, object]", function)
            arguments = function.get("arguments")
            broken = broken_args(arguments) if isinstance(arguments, str) else None
            if broken is None:
                continue
            function["arguments"] = broken
            count += 1
            parts.append(f"choices[{i}].message.tool_calls[{j}].function.arguments")
    if not count:
        return Unchangeable("the reply has no tool call with arguments to break", "data")
    return rewritten(value, headers, count, parts)


# The change each carried fault makes to a chat completion, by its word.
CHAT_CHANGES = {
    "replace": change_chat,
    "garble": garble_chat,
    "truncate": truncate_chat,
    "bad tool args": break_args_chat,
}


def response_reply(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    word: str,
    *,
    decoded: bool,
) -> tuple[dict[str, object], list[object]] | Unchangeable:
    """A Responses reply, parsed, and its output items; or why it cannot be changed."""
    said = chat_kind(status, op, headers, word)
    if said is not None:
        return said
    encoding = "" if decoded else (header(headers, "content-encoding") or "")
    plain_body = unpacked(body, encoding)
    if plain_body is None:
        return Unchangeable("the body will not unpack")
    try:
        value = json.loads(plain_body)
    except (ValueError, RecursionError):
        return Unchangeable("the body is not JSON that parses")
    output = cast("dict[str, object]", value).get("output") if isinstance(value, dict) else None
    if not isinstance(output, list):
        return Unchangeable("the reply has no output")
    return cast("dict[str, object]", value), cast("list[object]", output)


def as_dict(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def text_parts(output: list[object]) -> list[tuple[int, int, dict[str, object]]]:
    """Each output_text part with text, in order: its item, its place, and the part."""
    found: list[tuple[int, int, dict[str, object]]] = []
    for i, raw in enumerate(output):
        item = as_dict(raw)
        held = None if item is None or item.get("type") != "message" else item.get("content")
        for j, raw_part in enumerate(cast("list[object]", held) if isinstance(held, list) else []):
            part = as_dict(raw_part)
            if part is not None and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    found.append((i, j, part))
    return found


def function_calls(output: list[object]) -> list[tuple[int, dict[str, object]]]:
    """Each function_call item whose arguments are text, in order, with its place."""
    items = [(i, as_dict(raw)) for i, raw in enumerate(output)]
    return [
        (i, item)
        for i, item in items
        if item is not None
        and item.get("type") == "function_call"
        and isinstance(item.get("arguments"), str)
    ]


def no_output_text(output: list[object]) -> str:
    """Why a Responses reply had no text to change."""
    items = [as_dict(raw) or {} for raw in output]
    parts = [
        as_dict(part) or {}
        for item in items
        if item.get("type") == "message"
        for part in cast("list[object]", item.get("content") or [])
    ]
    if any(part.get("type") == "refusal" for part in parts):
        return "the reply is a refusal with no text"
    if any(item.get("type") == "function_call" for item in items):
        return "the reply has only tool calls and no text"
    return "the reply has no text"


class FreshIds(Protocol):
    """A provider that keeps the replies it made, so a changed reply must not carry their ids."""

    def fresh_ids(self, value: dict[str, object], output: list[object]) -> dict[str, object]: ...


def with_fresh_ids(
    provider: FreshIds, value: dict[str, object], output: list[object]
) -> dict[str, object]:
    """The changed reply under new ids in the call's provider's forms, for the reply and each
    item. The provider is the one the call went to, passed in."""
    return provider.fresh_ids(value, output)


def garble_responses(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    provider: FreshIds,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Put the stray token in each output_text part. Every other field stays as it was sent."""
    read = response_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, output = read
    count, parts = 0, list[str]()
    for i, j, part in text_parts(output):
        new, words = garbled(cast(str, part["text"]))
        part["text"] = new
        count += words
        parts.append(f"output[{i}].content[{j}].text")
    if not count:
        return Unchangeable(no_output_text(output), "data")
    return rewritten(with_fresh_ids(provider, value, output), headers, count, parts)


def truncate_responses(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    provider: FreshIds,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Stop the reply as max_output_tokens does: the first text is cut, and what came after it
    was never made, so it goes. With no text, the last function call's arguments are cut. The
    item and the reply are incomplete, for max_output_tokens, as captured; usage stays as it
    was billed."""
    read = response_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, output = read
    texts, calls = text_parts(output), function_calls(output)
    if texts:
        i, j, part = texts[0]
        part["text"] = cut(cast(str, part["text"]))
        item = cast("dict[str, object]", output[i])
        item["content"] = cast("list[object]", item["content"])[: j + 1]
        where = f"output[{i}].content[{j}].text"
    elif calls:
        i, item = calls[-1]
        item["arguments"] = cut(cast(str, item["arguments"]))
        where = f"output[{i}].arguments"
    else:
        why = no_output_text(output)
        if why == "the reply has no text":
            why = "the reply has no text and no tool call"
        return Unchangeable(why, "data")
    item["status"] = "incomplete"
    del output[i + 1 :]
    value.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    if "completed_at" in value:  # an incomplete reply never completed
        value["completed_at"] = None
    return rewritten(with_fresh_ids(provider, value, output), headers, 1, [where])


def break_args_responses(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    provider: FreshIds,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Break the arguments of every function_call item."""
    read = response_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, output = read
    count, parts = 0, list[str]()
    for i, item in function_calls(output):
        broken = broken_args(cast(str, item["arguments"]))
        if broken is None:
            continue
        item["arguments"] = broken
        count += 1
        parts.append(f"output[{i}].arguments")
    if not count:
        return Unchangeable("the reply has no tool call with arguments to break", "data")
    return rewritten(with_fresh_ids(provider, value, output), headers, count, parts)


RESPONSES_CHANGES = {
    "garble": garble_responses,
    "truncate": truncate_responses,
    "bad tool args": break_args_responses,
}


def model_change(word: str, op: str) -> Callable[..., WebChanged | Unchangeable]:
    """The change a carried fault makes to a model's reply of this kind. replace on a
    Responses reply keeps the chat change, which says it works on Chat Completions only."""
    if op == "responses" and word in RESPONSES_CHANGES:
        return RESPONSES_CHANGES[word]
    return CHAT_CHANGES[word]


def no_text(choices: list[object]) -> str:
    """Why a chat completion had no text to change, from its first choice."""
    first = (
        cast("dict[str, object]", choices[0]) if choices and isinstance(choices[0], dict) else {}
    )
    message = first.get("message")
    held = cast("dict[str, object]", message) if isinstance(message, dict) else {}
    if held.get("refusal"):
        return "the reply is a refusal with no text"
    if held.get("tool_calls"):
        return "the reply has only tool calls and no text"
    return "the reply has no text"
