"""Anthropic's reply shapes: the change each carried fault makes to a Messages reply, as pure
functions.

A model's reply is changed as its provider lays it out, so these live with the Anthropic
provider. Only `providers/impl/anthropic.py` calls them: every other module reaches a change
through `ModelProvider.change`. The helpers that do not depend on a shape, `change_text`,
`rewritten`, `garbled` and `cut`, stay in `changes`.

A reply is a list of content blocks. A change touches the `text` blocks, and a cut touches the
first `tool_use` when there is no text. The `thinking` and `redacted_thinking` blocks are signed
by Anthropic, so they are never touched: the agent sends them back as they came.
"""

import json
from collections.abc import Callable, Sequence
from typing import cast

from scrimmage.changes import (
    Unchangeable,
    WebChanged,
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


def only_on_messages(word: str, op: str) -> Unchangeable:
    return Unchangeable(
        f"{word} on a model call works on a Messages reply only, and this call is {op}; "
        "fault it another way",
        "unsupported",
    )


def reply_kind(
    status: int, op: str, headers: Sequence[tuple[str, str]], word: str = "replace"
) -> Unchangeable | None:
    """None when a model reply's headers allow a change to its content, else why not. Decided
    by the headers alone, so a stream is never read to find out."""
    if op != "chat":
        return only_on_messages(word, op)
    kind = media_type(header(headers, "content-type") or "")
    if kind == "text/event-stream":
        return Unchangeable(streamed(word), "unsupported")
    if status != 200:
        return Unchangeable(f"the reply status is {status}, which is not changed", "data")
    packed = packed_why(header(headers, "content-encoding") or "")
    if packed is not None:
        return Unchangeable(packed)
    if kind != "application/json":
        return Unchangeable(f"the reply is {kind or 'of no type'}, not a message")
    return None


def message_reply(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    word: str,
    *,
    decoded: bool,
) -> tuple[dict[str, object], list[object]] | Unchangeable:
    """A Messages reply, parsed, and its content blocks; or why it cannot be changed."""
    said = reply_kind(status, op, headers, word)
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
    content = cast("dict[str, object]", value).get("content") if isinstance(value, dict) else None
    if not isinstance(content, list):
        return Unchangeable("the reply has no content")
    return cast("dict[str, object]", value), cast("list[object]", content)


def as_dict(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def text_blocks(content: list[object]) -> list[tuple[int, dict[str, object]]]:
    """Each text block with text, in order, with its place."""
    found: list[tuple[int, dict[str, object]]] = []
    for i, raw in enumerate(content):
        block = as_dict(raw)
        if block is not None and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                found.append((i, block))
    return found


def tool_blocks(content: list[object]) -> list[tuple[int, dict[str, object]]]:
    """Each tool_use block, in order, with its place."""
    found: list[tuple[int, dict[str, object]]] = []
    for i, raw in enumerate(content):
        block = as_dict(raw)
        if block is not None and block.get("type") == "tool_use":
            found.append((i, block))
    return found


def no_text(value: dict[str, object], content: list[object]) -> str:
    """Why a Messages reply had no text to change."""
    blocks = [as_dict(raw) or {} for raw in content]
    if value.get("stop_reason") == "refusal":
        return "the reply is a refusal with no text"
    if any(block.get("type") == "tool_use" for block in blocks):
        return "the reply has only tool calls and no text"
    return "the reply has no text"


def change_messages(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Replace the text of each text block of a reply. A reply with no text block, such as a
    tool call alone or a refusal, is not changed. Every other block and every other field stay
    as sent. A model's text is changed as text, even when it looks like JSON."""
    read = message_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, content = read
    count, parts, missed = 0, list[str](), list[Unchangeable]()
    for i, block in text_blocks(content):
        done = change_text(cast(str, block["text"]), fault, f"content[{i}].text", sniff=False)
        if isinstance(done, Unchangeable):
            missed.append(done)
            continue
        block["text"] = done.value
        count += done.count
        parts += done.parts
    if not count:
        if missed:
            return next((m for m in missed if m.mistake), missed[0])
        return Unchangeable(no_text(value, content), "data")
    return rewritten(value, headers, count, parts)


def garble_messages(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Put the stray token in each text block. Every other field stays as it was sent."""
    read = message_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, content = read
    count, parts = 0, list[str]()
    for i, block in text_blocks(content):
        new, words = garbled(cast(str, block["text"]))
        if words:
            block["text"] = new
            count += words
            parts.append(f"content[{i}].text")
    if not count:
        return Unchangeable(no_text(value, content), "data")
    return rewritten(value, headers, count, parts)


def truncate_messages(
    status: int,
    op: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    fault: Fault,
    *,
    decoded: bool = False,
) -> WebChanged | Unchangeable:
    """Stop the reply as max_tokens does: the first text block is cut at its middle, and what
    came after it was never made, so it goes. With no text, the first tool_use is cut as a real
    cut leaves it: the block is there with an empty input, as in
    tests/fixtures/anthropic/real_tool_use_cut.json, and what came after it goes. stop_reason is
    max_tokens, and usage stays as it was billed."""
    read = message_reply(status, op, headers, body, fault.do, decoded=decoded)
    if isinstance(read, Unchangeable):
        return read
    value, content = read
    texts = text_blocks(content)
    calls = tool_blocks(content)
    if texts:
        i, block = texts[0]
        block["text"] = cut(cast(str, block["text"]))
        where = f"content[{i}].text"
    elif calls:
        i, block = calls[0]
        block["input"] = dict[str, object]()
        where = f"content[{i}].input"
    else:
        why = no_text(value, content)
        if why == "the reply has no text":
            why = "the reply has no text and no tool call"
        return Unchangeable(why, "data")
    del content[i + 1 :]
    value["stop_reason"] = "max_tokens"
    return rewritten(value, headers, 1, [where])


# The change each carried fault makes to a Messages reply, by its word. bad tool args is not
# here: no Anthropic reply can take it, and the row is refused before any reply is read.
MESSAGES_CHANGES = {
    "replace": change_messages,
    "garble": garble_messages,
    "truncate": truncate_messages,
}


def model_change(word: str) -> Callable[..., WebChanged | Unchangeable]:
    """The change a carried fault makes to a Messages reply."""
    return MESSAGES_CHANGES[word]
