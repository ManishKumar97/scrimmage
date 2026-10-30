"""OpenTelemetry spans for one run: a root span, a span per model call and per tool call.

Attribute names follow the OpenTelemetry GenAI semantic conventions.
Anything Faultline adds sits under the `faultline.` prefix.

The names the run reader reads come from `faultline.genai`, so the writer and the reader
share one spelling. The names below are the ones only this agent writes, spelled out the
same way and from the same convention version.
"""

import base64
import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

from faultline.genai import (
    CHAT,
    ERROR_TYPE,
    EXECUTE_TOOL,
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
)
from faultline_lab.models import Message, Reply, ToolResult

AGENT_NAME = "faultline-ida"

GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_ID = "gen_ai.response.id"
GEN_AI_USAGE_CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
GEN_AI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
GEN_AI_TOOL_DEFINITIONS = "gen_ai.tool.definitions"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"

type Attributes = dict[str, str | int | float]


class OtlpJsonFileExporter(SpanExporter):
    """Appends spans to a file as OTLP JSON, one ExportTraceServiceRequest per line.

    Each line is what an OpenTelemetry Collector's file receiver reads.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        request = MessageToDict(encode_spans(spans), use_integers_for_enums=True)
        line = json.dumps(hex_ids(request), separators=(",", ":"))
        try:
            with self.lock, self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS


# The protobuf JSON mapping writes bytes as base64. OTLP/JSON wants ids as hex.
ID_KEYS = {"traceId", "spanId", "parentSpanId"}


def hex_ids(node):
    """Rewrite every trace, span and parent id in an OTLP JSON tree from base64 to hex."""
    if isinstance(node, dict):
        return {
            key: base64.b64decode(value).hex()
            if key in ID_KEYS and isinstance(value, str)
            else hex_ids(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [hex_ids(item) for item in node]
    return node


def file_provider(path: Path) -> TracerProvider:
    """A provider that writes every finished span to `path` at once, with no network."""
    provider = TracerProvider(resource=Resource.create({"service.name": AGENT_NAME}))
    # Simple, not batch: a span is on disk when it ends, even if the process dies next.
    provider.add_span_processor(SimpleSpanProcessor(OtlpJsonFileExporter(path)))
    return provider


@dataclass
class Tracing:
    """What the loop needs to emit spans. The default emits nothing (no provider set)."""

    tracer: Tracer = field(default_factory=lambda: trace.get_tracer("faultline"))

    @contextmanager
    def span(
        self,
        name: str,
        attributes: Attributes,
        parent: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
    ) -> Iterator[Span]:
        """A span that always ends, and is marked ERROR if the block raises or is cancelled."""
        span = self.tracer.start_span(
            name, context=parent, kind=kind, attributes=attributes
        )
        try:
            yield span
        except BaseException as exc:
            # BaseException, so a deadline's CancelledError is recorded too.
            self.fail(span, type(exc).__name__, str(exc))
            raise
        finally:
            span.end()

    def fail(self, span: Span, error_type: str, text: str) -> None:
        """Mark a span ERROR, keeping the error text (an error body, a tool output)."""
        span.set_attribute(ERROR_TYPE, error_type)
        span.set_status(Status(StatusCode.ERROR, text or error_type))


def context_of(span: Span) -> Context:
    return trace.set_span_in_context(span)


def model_attributes(provider: str, model: str, step: int) -> Attributes:
    return {
        GEN_AI_OPERATION_NAME: CHAT,
        GEN_AI_PROVIDER_NAME: provider,
        GEN_AI_REQUEST_MODEL: model,
        "faultline.step": step,
    }


def record_reply(span: Span, reply: Reply) -> None:
    # A token count the provider did not report is left out, not written as zero.
    optional = {
        GEN_AI_USAGE_INPUT_TOKENS: reply.input_tokens,
        GEN_AI_USAGE_OUTPUT_TOKENS: reply.output_tokens,
        GEN_AI_USAGE_CACHE_READ_TOKENS: reply.cache_read_tokens,
        GEN_AI_USAGE_CACHE_CREATION_TOKENS: reply.cache_creation_tokens,
        GEN_AI_RESPONSE_MODEL: reply.response_model,
        GEN_AI_RESPONSE_ID: reply.response_id,
    }
    for key, value in optional.items():
        if value is not None:
            span.set_attribute(key, value)
    span.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, [reply.finish_reason])


def tool_attributes(name: str, call_id: str, step: int) -> Attributes:
    return {
        GEN_AI_OPERATION_NAME: EXECUTE_TOOL,
        GEN_AI_TOOL_NAME: name,
        GEN_AI_TOOL_CALL_ID: call_id,
        GEN_AI_TOOL_TYPE: "function",
        "faultline.step": step,
    }


def messages_json(messages: list[Message]) -> str:
    """The conversation in the GenAI input-messages shape, for replay."""
    wire: list[dict] = []
    for message in messages:
        if isinstance(message, str):
            wire.append(
                {"role": "user", "parts": [{"type": "text", "content": message}]}
            )
        elif isinstance(message, Reply):
            wire.append(reply_json(message))
        else:
            wire.append({"role": "tool", "parts": [result_part(r) for r in message]})
    return json.dumps(wire)


def reply_json(reply: Reply) -> dict:
    parts: list[dict] = [{"type": "text", "content": reply.text}] if reply.text else []
    parts += [
        {
            "type": "tool_call",
            "id": call.id,
            "name": call.name,
            "arguments": call.arguments,
        }
        for call in reply.tool_calls
    ]
    # The convention's value for a turn that ends in tool calls is "tool_call".
    finish = "tool_call" if reply.finish_reason == "tool_calls" else reply.finish_reason
    return {"role": "assistant", "parts": parts, "finish_reason": finish}


def result_part(result: ToolResult) -> dict:
    return {
        "type": "tool_call_response",
        "id": result.call_id,
        "response": result.content,
        "is_error": result.is_error,
    }


def output_json(reply: Reply) -> str:
    return json.dumps([reply_json(reply)])
