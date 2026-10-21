"""OpenTelemetry spans for one run: a root span, a span per model call and per tool call.

Attribute names follow the OpenTelemetry GenAI semantic conventions.
Anything Faultline adds sits under the `faultline.` prefix.
"""

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from google.protobuf.json_format import MessageToJson
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
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from faultline.models import Message, Reply, ToolResult

AGENT_NAME = "faultline-ida"

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
        line = MessageToJson(encode_spans(spans), indent=None)
        try:
            with self.lock, self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS


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
    # True puts full prompts and tool output in the spans. They can be large and private.
    content: bool = False

    @contextmanager
    def span(
        self,
        name: str,
        attributes: Attributes,
        parent: Context | None = None,
    ) -> Iterator[Span]:
        """A span that always ends, and is marked ERROR if the block raises or is cancelled."""
        span = self.tracer.start_span(name, context=parent, attributes=attributes)
        try:
            yield span
        except BaseException as exc:
            # BaseException, so a deadline's CancelledError is recorded too.
            fail(span, type(exc).__name__, str(exc) or type(exc).__name__)
            raise
        finally:
            span.end()


def context_of(span: Span) -> Context:
    return trace.set_span_in_context(span)


def fail(span: Span, error_type: str, description: str) -> None:
    span.set_attribute(ERROR_TYPE, error_type)
    span.set_status(Status(StatusCode.ERROR, description))


def model_attributes(provider: str, model: str, step: int) -> Attributes:
    return {
        gen_ai.GEN_AI_OPERATION_NAME: gen_ai.GenAiOperationNameValues.CHAT.value,
        gen_ai.GEN_AI_PROVIDER_NAME: provider,
        gen_ai.GEN_AI_REQUEST_MODEL: model,
        "faultline.step": step,
    }


def record_reply(span: Span, reply: Reply) -> None:
    span.set_attribute(gen_ai.GEN_AI_USAGE_INPUT_TOKENS, reply.input_tokens)
    span.set_attribute(gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS, reply.output_tokens)
    span.set_attribute(gen_ai.GEN_AI_RESPONSE_FINISH_REASONS, [reply.finish_reason])


def tool_attributes(name: str, call_id: str, step: int) -> Attributes:
    return {
        gen_ai.GEN_AI_OPERATION_NAME: gen_ai.GenAiOperationNameValues.EXECUTE_TOOL.value,
        gen_ai.GEN_AI_TOOL_NAME: name,
        gen_ai.GEN_AI_TOOL_CALL_ID: call_id,
        gen_ai.GEN_AI_TOOL_TYPE: "function",
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
    return {"role": "assistant", "parts": parts, "finish_reason": reply.finish_reason}


def result_part(result: ToolResult) -> dict:
    return {
        "type": "tool_call_response",
        "id": result.call_id,
        "response": result.content,
        "is_error": result.is_error,
    }


def output_json(reply: Reply) -> str:
    return json.dumps([reply_json(reply)])
