"""A model reply cut short, as a provider sends it when the output hits its token limit.

The reply keeps the first half of its text and loses its tool calls, since a call cut
mid-way cannot run. It is marked as stopped at the limit: OpenAI's finish_reason
"length", Anthropic's stop_reason "max_tokens". This works on a plain JSON reply and
on a streamed one, OpenAI chunks or Anthropic events. A streamed reply keeps the first
half of its text characters, so a reply streamed in one piece is cut too.
"""

import json

from faultline.proxy.streams import is_stream

OPENAI_LENGTH = "length"
ANTHROPIC_LENGTH = "max_tokens"


class Unchanged(ValueError):
    """The reply cannot be cut. The message says why."""


def truncated(body: bytes, anthropic: bool) -> bytes:
    """The reply cut to half its text. Raises Unchanged for a shape it does not know."""
    if is_stream(body):
        return truncated_stream(body)
    try:
        reply = json.loads(body)
    except ValueError:
        raise Unchanged("the reply is not JSON") from None
    if not isinstance(reply, dict):
        raise Unchanged("the reply is not a JSON object")
    try:
        if anthropic:
            cut_anthropic(reply)
        else:
            cut_openai(reply)
    except (AttributeError, TypeError, KeyError) as exc:
        raise Unchanged(f"the reply has an unknown shape ({exc})") from None
    return json.dumps(reply).encode()


def cut_openai(reply: dict) -> None:
    choices = reply["choices"]
    if not choices:
        raise Unchanged("the reply has no choices")
    for choice in choices:
        message = choice["message"]
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = content[: len(content) // 2]
        message.pop("tool_calls", None)
        message.pop("function_call", None)  # the legacy form of a tool call
        choice["finish_reason"] = OPENAI_LENGTH


def cut_anthropic(reply: dict) -> None:
    blocks = reply["content"]
    left = sum(len(b.get("text", "")) for b in blocks if b["type"] == "text") // 2
    kept = []
    for block in blocks:
        if block["type"] == "tool_use":
            continue
        if block["type"] == "text":
            block["text"], left = (
                block["text"][:left],
                max(left - len(block["text"]), 0),
            )
        kept.append(block)
    reply["content"] = kept
    reply["stop_reason"] = ANTHROPIC_LENGTH
    reply["stop_sequence"] = None


def truncated_stream(body: bytes) -> bytes:
    """A streamed reply cut to half its text, ended with the matching finish event."""
    text = body.decode("utf-8", errors="strict").replace("\r\n", "\n")
    events = [event_of(block) for block in text.split("\n\n") if block.strip()]
    datas = [data for _, data, _ in events if isinstance(data, dict)]
    if any(d.get("object") == "chat.completion.chunk" for d in datas):
        kept = cut_openai_stream(events)
    elif any(d.get("type") == "message_start" for d in datas):
        kept = cut_anthropic_stream(events)
    else:
        raise Unchanged("the stream has no known shape")
    return "".join(block + "\n\n" for block in kept).encode()


type Event = tuple[str | None, object, str]  # event name, data, the raw block


def event_of(block: str) -> Event:
    name = None
    lines = []
    for line in block.split("\n"):
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
    data: object = "\n".join(lines)
    try:
        data = json.loads(str(data))
    except ValueError:
        pass  # [DONE], a comment, or text: kept as it is
    return name, data, block


def written(name: str | None, data: dict) -> str:
    return (f"event: {name}\n" if name else "") + f"data: {json.dumps(data)}"


def cut_openai_stream(events: list[Event]) -> list[str]:
    total: dict[int, int] = {}  # text characters of each choice
    for _, data, _ in events:
        for choice in choices_of(data):
            content = (choice.get("delta") or {}).get("content") or ""
            total[choice.get("index", 0)] = total.get(choice.get("index", 0), 0) + len(
                content
            )
    left = {index: count // 2 for index, count in total.items()}
    finished: set[int] = set()
    kept: list[str] = []
    for name, data, raw in events:
        if data == "[DONE]":
            kept += finish_chunks(events, total.keys() - finished)
            finished |= total.keys()
        if not isinstance(data, dict) or not choices_of(data):
            kept.append(raw)  # usage, [DONE], a comment
            continue
        choices = []
        for choice in choices_of(data):
            index = choice.get("index", 0)
            delta = dict(choice.get("delta") or {})
            delta.pop("tool_calls", None)
            delta.pop("function_call", None)  # the legacy form of a tool call
            content = delta.get("content")
            if isinstance(content, str) and content:
                cut = content[: left.get(index, 0)]
                left[index] = left.get(index, 0) - len(cut)
                if cut:
                    delta["content"] = cut
                else:
                    del delta["content"]
            if choice.get("finish_reason") is not None:
                finished.add(index)
                choices.append(
                    {**choice, "delta": delta, "finish_reason": OPENAI_LENGTH}
                )
            elif delta:
                choices.append({**choice, "delta": delta})
            # else nothing is left of this choice in this chunk
        if choices == choices_of(data):
            kept.append(raw)
        elif choices:
            kept.append(written(name, {**data, "choices": choices}))
    # A stream cut before its finish chunk and [DONE] still ends at the limit.
    kept += finish_chunks(events, total.keys() - finished)
    return kept


def choices_of(data: object) -> list[dict]:
    if not isinstance(data, dict) or data.get("object") != "chat.completion.chunk":
        return []
    choices = data.get("choices")
    return (
        [c for c in choices if isinstance(c, dict)] if isinstance(choices, list) else []
    )


def finish_chunks(events: list[Event], indexes: set[int]) -> list[str]:
    """Finish chunks for choices whose stream ended without one."""
    if not indexes:
        return []
    first = next(data for _, data, _ in events if choices_of(data))
    assert isinstance(first, dict)
    choices = [
        {"index": i, "delta": {}, "finish_reason": OPENAI_LENGTH}
        for i in sorted(indexes)
    ]
    return [written(None, {**first, "choices": choices})]


def cut_anthropic_stream(events: list[Event]) -> list[str]:
    text = 0
    for _, data, _ in events:
        if isinstance(data, dict) and data.get("type") == "content_block_delta":
            text += len((data.get("delta") or {}).get("text") or "")
    left = text // 2
    dropped: set[int] = set()  # tool_use blocks, by their index in the stream
    renumbered: dict[int, int] = {}  # the index each kept block gets
    kept: list[str] = []
    stopped = False
    for name, data, raw in events:
        kind = data.get("type") if isinstance(data, dict) else None
        if not isinstance(data, dict) or kind not in (
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ):
            kept.append(raw)
            continue
        if kind == "message_stop" and not stopped:
            kept.append(written("message_delta", stop_event()))
            stopped = True
        if kind == "message_delta":
            delta = {
                **(data.get("delta") or {}),
                "stop_reason": ANTHROPIC_LENGTH,
                "stop_sequence": None,
            }
            kept.append(written(name, {**data, "delta": delta}))
            stopped = True
            continue
        if kind == "message_stop":
            kept.append(raw)
            continue
        index = data["index"]
        if kind == "content_block_start":
            if (data.get("content_block") or {}).get("type") == "tool_use":
                dropped.add(index)
                continue
            renumbered[index] = len(renumbered)
        if index in dropped:
            continue
        event = {**data, "index": renumbered.get(index, index)}
        delta = data.get("delta") or {}
        if kind == "content_block_delta" and delta.get("type") == "text_delta":
            piece = str(delta.get("text") or "")
            cut = piece[:left]
            left -= len(cut)
            if not cut:
                continue
            event["delta"] = {**delta, "text": cut}
        kept.append(raw if event == data else written(name, event))
    if not stopped:
        kept.append(written("message_delta", stop_event()))
    return kept


def stop_event() -> dict:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": ANTHROPIC_LENGTH, "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }
