"""Streamed model replies, rebuilt into the reply a non-streamed call would have given.

With stream=True a model API answers with server-sent events: many small JSON
chunks instead of one reply. The proxy passes them to the agent as they come and
keeps the bytes. Rebuilding the whole reply from them lets the recorder read a
streamed reply exactly as it reads a plain one.

Two shapes are known:

- OpenAI chat completions: `chat.completion.chunk` objects, each with a delta of
  text or tool call parts, then a finish_reason, and usage when the agent asked for it.
- Anthropic messages: message_start, content_block_start, content_block_delta,
  content_block_stop, message_delta and message_stop events.
"""

import json


def is_stream(body: bytes) -> bool:
    """True when the body is server-sent events rather than one JSON value."""
    return body.lstrip().startswith((b"data:", b"event:", b":"))


def events(body: bytes) -> list[dict]:
    """The JSON data of each event, in order. Comments, [DONE] and non-JSON are skipped."""
    found = []
    text = body.decode("utf-8", errors="replace").replace("\r\n", "\n")
    for block in text.split("\n\n"):
        data = "\n".join(
            line[5:].removeprefix(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        )
        if not data or data == "[DONE]":
            continue
        try:
            value = json.loads(data)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def reply_from_stream(body: bytes) -> dict | None:
    """The whole reply, in the non-streamed shape, or None when no known shape fits."""
    chunks = events(body)
    try:
        if any(c.get("object") == "chat.completion.chunk" for c in chunks):
            return openai_reply(chunks)
        if any(c.get("type") == "message_start" for c in chunks):
            return anthropic_reply(chunks)
    except (AttributeError, TypeError, KeyError, IndexError, ValueError):
        return None
    return None


def openai_reply(chunks: list[dict]) -> dict:
    content: list[str] = []
    calls: dict[int, dict] = {}  # by the index each delta names
    finish = None
    reply: dict = {"choices": []}
    for chunk in chunks:
        reply.setdefault("model", chunk.get("model"))
        if chunk.get("usage"):
            reply["usage"] = chunk["usage"]  # the last chunk, with include_usage
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue  # the recorder reads the first choice only
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for part in delta.get("tool_calls") or []:
                call = calls.setdefault(
                    part.get("index", 0),
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                call["id"] = part.get("id") or call["id"]
                function = part.get("function") or {}
                call["function"]["name"] += function.get("name") or ""
                call["function"]["arguments"] += function.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    message: dict = {"role": "assistant", "content": "".join(content) or None}
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    reply["choices"] = [{"index": 0, "message": message, "finish_reason": finish}]
    return reply


def anthropic_reply(chunks: list[dict]) -> dict:
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}  # tool input JSON, sent in pieces
    reply: dict = {"type": "message", "content": []}
    usage: dict = {}
    for event in chunks:
        match event.get("type"):
            case "message_start":
                message = event.get("message") or {}
                reply["model"] = message.get("model")
                usage.update(message.get("usage") or {})
            case "content_block_start":
                blocks[event["index"]] = dict(event["content_block"])
            case "content_block_delta":
                block = blocks.setdefault(event["index"], {"type": "text", "text": ""})
                delta = event.get("delta") or {}
                match delta.get("type"):
                    case "text_delta":
                        block["text"] = block.get("text", "") + delta.get("text", "")
                    case "input_json_delta":
                        piece = delta.get("partial_json", "")
                        partial[event["index"]] = (
                            partial.get(event["index"], "") + piece
                        )
                    case "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + delta.get(
                            "thinking", ""
                        )
            case "message_delta":
                reply["stop_reason"] = (event.get("delta") or {}).get("stop_reason")
                usage.update(event.get("usage") or {})
    for index, text in partial.items():
        blocks[index]["input"] = json.loads(text) if text else {}
    reply["content"] = [blocks[index] for index in sorted(blocks)]
    reply["usage"] = usage
    return reply
