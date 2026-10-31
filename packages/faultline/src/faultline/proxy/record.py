"""The proxy's own trace: OTLP JSON lines in the GenAI shape `faultline.runs` reads.

One `faultline run` is one trace. Its root is an `invoke_agent` span. Each model step
is a `chat` span and each MCP tool call an `execute_tool` span, all children of the root.
`faultline run` passes the trace and root ids to everything it starts, in environment
variables, so spans written by different processes join into one run.

Each process writes its own file in the trace directory, so no two processes ever
append to the same file: the proxy writes model.jsonl, each MCP wrapper mcp-<pid>.jsonl.
"""

import json
import os
import secrets
import shlex
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from faultline.model import ToolCall
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

TRACE_DIR = "FAULTLINE_TRACE_DIR"
# Faultline's own attribute, on the span of a call it changed. Its value is the fault spec.
# The run reader reads it: Faultline writes it, so no agent's names are involved.
FAULTLINE_INJECTED = "faultline.injected"
TRACE_ID = "FAULTLINE_TRACE_ID"
ROOT_SPAN_ID = "FAULTLINE_ROOT_SPAN_ID"
ATTEMPTS = "faultline.proxy.attempts"
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

type Attributes = dict[str, str | int | list[str]]


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


@dataclass
class Recorder:
    """Writes spans of one trace to one file, one OTLP export request per line."""

    path: Path
    trace_id: str
    root_span_id: str
    open: list[Exchange] = field(default_factory=list)  # the attempts of the last step

    @classmethod
    def from_env(
        cls, file_name: str, env: Mapping[str, str] = os.environ
    ) -> "Recorder | None":
        """A recorder for this process, or None when not started by `faultline run`."""
        if not all(env.get(name) for name in (TRACE_DIR, TRACE_ID, ROOT_SPAN_ID)):
            return None
        path = Path(env[TRACE_DIR]) / file_name
        return cls(path, env[TRACE_ID], env[ROOT_SPAN_ID])

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
            "faultline.command": shlex.join(command),
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
            self.write(self.chat(self.open))
            self.open = []

    def chat(self, attempts: Sequence[Exchange]) -> dict:
        first, last = attempts[0], attempts[-1]
        request = loads(first.request)
        reply = loads(decoded(last.response, last.encoding))
        anthropic = first.path.rstrip("/").endswith("/messages")
        attributes: Attributes = {GEN_AI_OPERATION_NAME: CHAT, ATTEMPTS: len(attempts)}
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
        injected = next((a.injected for a in attempts if a.injected), None)
        if injected:
            attributes[FAULTLINE_INJECTED] = injected
        events = [
            {
                "timeUnixNano": str(a.started),
                "name": "faultline.proxy.attempt",
                "attributes": encode(
                    {
                        "faultline.proxy.attempt": a.attempt,
                        "http.response.status_code": a.status,
                    }
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
            error=str(last.status) if last.status >= 400 else None,
            events=events,
        )

    def tool_call(self, call: ToolCall) -> None:
        attributes: Attributes = {
            GEN_AI_OPERATION_NAME: EXECUTE_TOOL,
            GEN_AI_TOOL_NAME: call.name,
            GEN_AI_TOOL_CALL_ARGUMENTS: call.arguments,
            GEN_AI_TOOL_CALL_RESULT: call.result,
        }
        if call.injected:
            attributes[FAULTLINE_INJECTED] = call.injected
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
