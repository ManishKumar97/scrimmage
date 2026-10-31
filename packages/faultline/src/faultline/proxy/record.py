"""The proxy's own trace: OTLP JSON lines in the GenAI shape `faultline.runs` reads.

One `faultline run` is one trace. Its root is an `invoke_agent` span. Each model step
is a `chat` span and each MCP tool call an `execute_tool` span, all children of the root.
`faultline run` passes the trace and root ids to everything it starts, in environment
variables, so spans written by different processes join into one run.

Each process writes its own file in the trace directory, so no two processes ever
append to the same file: the proxy writes model.jsonl, each MCP wrapper mcp-<pid>.jsonl.
"""

import json
import secrets
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from faultline.model import ToolCall
from faultline.proxy.context import RunContext
from faultline.proxy.streams import is_stream, reply_from_stream
from faultline.semconv import (
    CHAT,
    ERROR_TYPE,
    EXECUTE_TOOL,
    GEN_AI_INPUT_MESSAGES,
    GEN_AI_OPERATION_NAME,
    GEN_AI_OUTPUT_MESSAGES,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_TOOL_CALL_ARGUMENTS,
    GEN_AI_TOOL_CALL_RESULT,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INVOKE_AGENT,
)

# Faultline's own attribute, on the span of a call it changed. Its value is the fault spec.
# The run reader reads it: Faultline writes it, so no agent's names are involved.
FAULTLINE_INJECTED = "faultline.injected"
# On a call a fault targeted but could not change: the spec and why, such as
# "model:truncated: the upstream answered 500". The call passed through as it was.
INJECTED_SKIPPED = "faultline.injected.skipped"
# The value a fault put in, such as the service tool:NAME:wrong=VALUE named.
INJECTED_VALUE = "faultline.injected.value"
# The argument of the call whose value the fault replaced, such as "service".
INJECTED_ARGUMENT = "faultline.injected.argument"
# The most characters of content a span records, so one huge prompt or tool
# result cannot fill the disk. Over it, content is cut and the span marked.
DEFAULT_MAX_CONTENT = 64 * 1024
CONTENT_CUT = "faultline.proxy.content_cut"
# Message part fields that hold content, in the GenAI message shape.
CONTENT_FIELDS = ("content", "response", "arguments")
ATTEMPTS = "faultline.proxy.attempts"
# On a streamed reply: "parsed", or "unparsed" when no known stream shape fit.
STREAM = "faultline.proxy.stream"
STATUS_ERROR = 2  # OTLP status code
SPAN_KIND_INTERNAL, SPAN_KIND_CLIENT = 1, 3

# Finish reasons in the GenAI convention's values, from each provider's.
FINISH_REASONS = {
    "tool_calls": "tool_call",
    "function_call": "tool_call",
    "tool_use": "tool_call",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
}

type Attributes = dict[str, str | int | bool | list[str]]


@dataclass(frozen=True)
class Exchange:
    """One HTTP request through the model proxy and the reply the agent got."""

    step: int
    attempt: int
    path: str
    request: bytes
    status: int
    response: bytes
    started: int  # unix nanoseconds
    ended: int
    encoding: str | None = None  # the reply's Content-Encoding
    injected: str | None = None  # the fault spec that made the reply
    error: str | None = None  # an error with no HTTP status, such as "timeout"
    skipped: str | None = None  # a targeting fault that could not apply, and why


@dataclass
class Recorder:
    """Writes spans of one trace to one file, one OTLP export request per line."""

    path: Path
    trace_id: str
    root_span_id: str
    open: list[Exchange] = field(default_factory=list)  # the attempts of the last step
    # True when the last step written was a streamed reply no known shape fits, so
    # its answer is not in the trace.
    last_unparsed: bool = False
    max_content: int = DEFAULT_MAX_CONTENT

    @classmethod
    def for_run(cls, run: RunContext, file_name: str) -> "Recorder":
        """A recorder for this process's own file in the run's trace directory."""
        return cls(
            Path(run.trace_dir) / file_name,
            run.trace_id,
            run.root_span_id,
            max_content=run.max_content or DEFAULT_MAX_CONTENT,
        )

    def cap(self, attributes: Attributes) -> None:
        """Cut the content in a span's attributes to max_content, and mark the span."""
        cut = False
        for key in (GEN_AI_INPUT_MESSAGES, GEN_AI_OUTPUT_MESSAGES):
            value = attributes.get(key)
            if isinstance(value, str) and len(value) > self.max_content:
                messages = json.loads(value)
                cut_messages(messages, self.max_content)
                attributes[key] = json.dumps(messages)
                cut = True
        for key in (GEN_AI_TOOL_CALL_ARGUMENTS, GEN_AI_TOOL_CALL_RESULT):
            value = attributes.get(key)
            if isinstance(value, str) and len(value) > self.max_content:
                attributes[key] = cut_text(value, self.max_content)
                cut = True
        if cut:
            attributes[CONTENT_CUT] = True

    def write(self, span: dict) -> None:
        line = json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": encode({"service.name": "faultline"})
                        },
                        "scopeSpans": [
                            {"scope": {"name": "faultline"}, "spans": [span]}
                        ],
                    }
                ]
            },
            separators=(",", ":"),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    def span(
        self,
        name: str,
        started: int,
        ended: int,
        attributes: Attributes,
        *,
        span_id: str | None = None,
        parent: str | None = None,
        kind: int = SPAN_KIND_INTERNAL,
        error: str | None = None,
        events: Sequence[dict] = (),
    ) -> dict:
        span: dict = {
            "traceId": self.trace_id,
            "spanId": span_id or secrets.token_hex(8),
            "name": name,
            "kind": kind,
            "startTimeUnixNano": str(started),
            "endTimeUnixNano": str(ended),
            "attributes": encode(attributes),
        }
        if parent:
            span["parentSpanId"] = parent
        if events:
            span["events"] = list(events)
        if error:
            span["attributes"] += encode({ERROR_TYPE: error})
            span["status"] = {"code": STATUS_ERROR, "message": error}
        return span

    def root(self, started: int, ended: int, command: Sequence[str], code: int) -> None:
        """The run's root span. Written last, when the agent has exited."""
        attributes: Attributes = {
            GEN_AI_OPERATION_NAME: INVOKE_AGENT,
            # The program only: arguments can hold a prompt or a secret.
            "faultline.command": command[0] if command else "",
            "faultline.command.args": len(command) - 1,
            "faultline.exit_code": code,
        }
        self.write(
            self.span(
                "invoke_agent",
                started,
                ended,
                attributes,
                span_id=self.root_span_id,
                error=f"exit {code}" if code else None,
            )
        )

    def exchange(self, exchange: Exchange) -> None:
        """Add one attempt. A step's span is written when the next step begins."""
        if self.open and self.open[0].step != exchange.step:
            self.flush()
        self.open.append(exchange)

    def flush(self) -> None:
        """Write the open step's chat span, if there is one."""
        if self.open:
            span = self.chat(self.open)
            self.last_unparsed = any(
                a["key"] == STREAM and a["value"] == {"stringValue": "unparsed"}
                for a in span["attributes"]
            )
            self.write(span)
            self.open = []

    def chat(self, attempts: Sequence[Exchange]) -> dict:
        first, last = attempts[0], attempts[-1]
        request = loads(first.request)
        reply, stream = reply_of(last)
        anthropic = first.path.rstrip("/").endswith("/messages")
        attributes: Attributes = {GEN_AI_OPERATION_NAME: CHAT, ATTEMPTS: len(attempts)}
        if stream:
            attributes[STREAM] = stream
        model = request.get("model")
        if isinstance(model, str):
            attributes[GEN_AI_REQUEST_MODEL] = model
        try:
            attributes[GEN_AI_INPUT_MESSAGES] = json.dumps(
                anthropic_input(request) if anthropic else openai_input(request)
            )
        except (AttributeError, TypeError, KeyError):
            pass  # a request shape this recorder does not know
        if 200 <= last.status < 300 and reply:
            try:
                attributes.update(reply_attributes(reply))
            except (AttributeError, TypeError, KeyError, IndexError):
                pass
        self.cap(attributes)
        injected = next((a.injected for a in attempts if a.injected), None)
        if injected:
            attributes[FAULTLINE_INJECTED] = injected
        elif skipped := next((a.skipped for a in attempts if a.skipped), None):
            attributes[INJECTED_SKIPPED] = skipped
        events = [
            {
                "timeUnixNano": str(a.started),
                "name": "faultline.proxy.attempt",
                "attributes": encode(
                    {"faultline.proxy.attempt": a.attempt}
                    | ({"http.response.status_code": a.status} if a.status else {})
                    | ({ERROR_TYPE: a.error} if a.error else {})
                ),
            }
            for a in attempts
        ]
        return self.span(
            f"chat {model}" if isinstance(model, str) else "chat",
            first.started,
            last.ended,
            attributes,
            parent=self.root_span_id,
            kind=SPAN_KIND_CLIENT,
            error=last.error or (str(last.status) if last.status >= 400 else None),
            events=events,
        )

    def tool_call(self, call: ToolCall, extra: Attributes | None = None) -> None:
        """One execute_tool span. `extra` adds Faultline's own attributes."""
        attributes: Attributes = {
            GEN_AI_OPERATION_NAME: EXECUTE_TOOL,
            GEN_AI_TOOL_NAME: call.name,
            GEN_AI_TOOL_CALL_ARGUMENTS: call.arguments,
            GEN_AI_TOOL_CALL_RESULT: call.result,
        }
        self.cap(attributes)
        if call.injected:
            attributes[FAULTLINE_INJECTED] = call.injected
        attributes.update(extra or {})
        ended = call.started + int(call.seconds * 1_000_000_000)
        self.write(
            self.span(
                f"execute_tool {call.name}",
                call.started,
                ended,
                attributes,
                parent=self.root_span_id,
                error=call.error_type,
            )
        )


def encode(attributes: Mapping[str, str | int | list[str]]) -> list[dict]:
    """Attributes as OTLP JSON key-values. Integers are strings in OTLP JSON."""
    encoded = []
    for key, value in attributes.items():
        if isinstance(value, bool):
            any_value: dict = {"boolValue": value}
        elif isinstance(value, int):
            any_value = {"intValue": str(value)}
        elif isinstance(value, list):
            any_value = {"arrayValue": {"values": [{"stringValue": v} for v in value]}}
        else:
            any_value = {"stringValue": value}
        encoded.append({"key": key, "value": any_value})
    return encoded


def cut_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]} [cut, {len(text)} chars]"


def cut_messages(messages: list[dict], limit: int) -> None:
    """Cut the content fields of GenAI messages so they share `limit` characters.

    The messages stay valid JSON in the same shape, so the reader still reads them.
    """
    fields = [
        (part, key)
        for message in messages
        for part in message.get("parts", [])
        for key in CONTENT_FIELDS
        if isinstance(part.get(key), str)
    ]
    each = max(limit // max(len(fields), 1), 64)
    for part, key in fields:
        part[key] = cut_text(part[key], each)


def decoded(body: bytes, encoding: str | None) -> bytes:
    """The reply body as the agent's SDK reads it. An unknown encoding gives nothing."""
    match (encoding or "identity").lower():
        case "identity":
            return body
        case "gzip" | "deflate":
            try:
                return zlib.decompress(body, zlib.MAX_WBITS | 32)  # gzip or zlib header
            except zlib.error:
                return b""
        case _:
            return b""  # br and zstd are not in the standard library


def loads(body: bytes) -> dict:
    """A JSON object body, or an empty dict for anything else (a stream, an error page)."""
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def reply_of(exchange: Exchange) -> tuple[dict, str | None]:
    """The reply as a JSON object, read from a stream when it was one, else empty.
    Also "parsed" or "unparsed" for a stream, None for a plain body."""
    body = decoded(exchange.response, exchange.encoding)
    reply = loads(body)
    if reply or not is_stream(body):
        return reply, None
    reply = reply_from_stream(body) or {}
    return reply, "parsed" if reply else "unparsed"


def tokens_of(exchange: Exchange) -> tuple[int, int]:
    """The input and output tokens a successful reply reports. (0, 0) when it
    reports none, or failed."""
    if not 200 <= exchange.status < 300:
        return 0, 0
    reply, _ = reply_of(exchange)
    try:
        attributes = reply_attributes(reply) if reply else {}
    except (AttributeError, TypeError, KeyError, IndexError):
        return 0, 0
    input_tokens = attributes.get(GEN_AI_USAGE_INPUT_TOKENS)
    output_tokens = attributes.get(GEN_AI_USAGE_OUTPUT_TOKENS)
    return (
        input_tokens if isinstance(input_tokens, int) else 0,
        output_tokens if isinstance(output_tokens, int) else 0,
    )


def finish_reason(reason: object) -> str:
    return FINISH_REASONS.get(str(reason), str(reason))


def text_of(content: object) -> str:
    """The text of a message content: a string, or a list of blocks with text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and "text" in b
        )
    return ""


def arguments_of(raw: object) -> object:
    """Tool call arguments as an object when they are JSON, else as written."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def openai_message(message: dict) -> dict:
    """One OpenAI chat message in the GenAI message shape."""
    role = message.get("role", "user")
    parts: list[dict] = []
    if role == "tool":
        parts.append(
            {
                "type": "tool_call_response",
                "id": message.get("tool_call_id", ""),
                "response": text_of(message.get("content")),
            }
        )
    elif text := text_of(message.get("content")):
        parts.append({"type": "text", "content": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        parts.append(
            {
                "type": "tool_call",
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "arguments": arguments_of(function.get("arguments")),
            }
        )
    return {"role": "system" if role == "developer" else role, "parts": parts}


def openai_input(request: dict) -> list[dict]:
    return [openai_message(m) for m in request.get("messages", [])]


def anthropic_parts(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "content": content}]
    parts: list[dict] = []
    for block in content if isinstance(content, list) else []:
        match block.get("type"):
            case "text":
                parts.append({"type": "text", "content": block.get("text", "")})
            case "tool_use":
                parts.append(
                    {
                        "type": "tool_call",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("input"),
                    }
                )
            case "tool_result":
                parts.append(
                    {
                        "type": "tool_call_response",
                        "id": block.get("tool_use_id", ""),
                        "response": text_of(block.get("content")),
                    }
                )
            case "thinking":
                parts.append(
                    {"type": "reasoning", "content": block.get("thinking", "")}
                )
    return parts


def anthropic_input(request: dict) -> list[dict]:
    messages = []
    if system := text_of(request.get("system")):
        messages.append(
            {"role": "system", "parts": [{"type": "text", "content": system}]}
        )
    for message in request.get("messages", []):
        role = message.get("role", "user")
        messages.append(
            {"role": role, "parts": anthropic_parts(message.get("content"))}
        )
    return messages


def reply_attributes(reply: dict) -> Attributes:
    """Output messages, finish reason and token usage from an OpenAI or Anthropic reply."""
    attributes: Attributes = {}
    usage = reply.get("usage") or {}
    if "choices" in reply:  # OpenAI chat completions
        choice = reply["choices"][0]
        message = openai_message(choice.get("message", {}))
        reason = finish_reason(choice.get("finish_reason"))
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
    else:  # Anthropic messages
        message = {"role": "assistant", "parts": anthropic_parts(reply.get("content"))}
        reason = finish_reason(reply.get("stop_reason"))
        input_tokens = usage.get("input_tokens")
        if isinstance(input_tokens, int):
            # The convention counts all input. Anthropic's input_tokens leaves out the cache.
            input_tokens += (usage.get("cache_read_input_tokens") or 0) + (
                usage.get("cache_creation_input_tokens") or 0
            )
        output_tokens = usage.get("output_tokens")
    message["finish_reason"] = reason
    attributes[GEN_AI_OUTPUT_MESSAGES] = json.dumps([message])
    attributes[GEN_AI_RESPONSE_FINISH_REASONS] = [reason]
    if isinstance(input_tokens, int):
        attributes[GEN_AI_USAGE_INPUT_TOKENS] = input_tokens
    if isinstance(output_tokens, int):
        attributes[GEN_AI_USAGE_OUTPUT_TOKENS] = output_tokens
    return attributes
