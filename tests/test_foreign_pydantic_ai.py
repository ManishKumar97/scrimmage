"""Faultline reads the traces of an agent it did not write.

The agents in foreign/ use Pydantic AI and its own OpenTelemetry instrumentation.
The reader must find the same steps and tool calls as the agent's own message log,
with message content recorded and without it.
"""

import asyncio
import base64
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from foreign.agents import TASKS
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from pydantic_ai import Agent, ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.instrumented import InstrumentationSettings

from faultline.runs import read_runs, show_run


class OtlpJsonLinesExporter(SpanExporter):
    """Appends spans as OTLP JSON, one request per line, ids in hex as OTLP/JSON wants."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        request = MessageToDict(encode_spans(spans), use_integers_for_enums=True)
        for resource in request["resourceSpans"]:
            for scope in resource["scopeSpans"]:
                for span in scope["spans"]:
                    for key in ("traceId", "spanId", "parentSpanId"):
                        if key in span:
                            span[key] = base64.b64decode(span[key]).hex()
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(request, separators=(",", ":")) + "\n")
        return SpanExportResult.SUCCESS


def run_traced(task: int, path: Path, include_content: bool) -> list[ModelMessage]:
    """Run one task with Pydantic AI's instrumentation on, spans to `path`."""
    make_agent, prompt = TASKS[task]
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OtlpJsonLinesExporter(path)))
    settings = InstrumentationSettings(
        tracer_provider=provider, include_content=include_content
    )
    Agent.instrument_all(settings)
    # run_sync looks for the current event loop. Give it its own, not a deprecated default.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        messages = make_agent().run_sync(prompt).all_messages()
    finally:
        asyncio.set_event_loop(None)
        loop.close()
        Agent.instrument_all(False)  # a global switch: leave it off for other tests
        provider.shutdown()
    return messages


def calls_per_step(messages: list[ModelMessage]) -> list[int]:
    """The agent's own log: one ModelResponse per step, its ToolCallParts as calls."""
    return [
        sum(isinstance(part, ToolCallPart) for part in message.parts)
        for message in messages
        if isinstance(message, ModelResponse)
    ]


@pytest.mark.parametrize("include_content", [True, False], ids=["content", "none"])
@pytest.mark.parametrize("task", range(len(TASKS)))
def test_the_reader_finds_the_agents_own_steps_and_tool_calls(
    tmp_path: Path, task: int, include_content: bool
):
    path = tmp_path / "spans.jsonl"
    agent_calls = calls_per_step(run_traced(task, path, include_content))

    reading = read_runs(path)

    [run] = reading.runs
    assert (reading.unreadable, reading.no_spans) == (0, 0)
    assert len(run.steps) == len(agent_calls)
    assert len(run.tool_calls) == sum(agent_calls)
    # Every call sits under the step that asked for it, none before the first step.
    assert [len(step.tool_calls) for step in run.steps] == agent_calls
    assert run.unplaced == ()
    assert run.content_recorded is include_content
    shown = show_run(run).splitlines()
    assert f"steps={len(agent_calls)}" in shown[0]
    assert sum(line.startswith("    tool ") for line in shown) == sum(agent_calls)
    assert ("  content: not recorded" in shown) is not include_content


def test_the_three_tasks_make_different_numbers_of_tool_calls(tmp_path: Path):
    counts = [
        calls_per_step(run_traced(task, tmp_path / f"{task}.jsonl", True))
        for task in range(len(TASKS))
    ]

    assert counts == [[0], [1, 0], [2, 1, 0]]


def test_without_content_the_question_and_tool_results_are_not_shown(tmp_path: Path):
    path = tmp_path / "spans.jsonl"
    run_traced(2, path, include_content=False)

    [run] = read_runs(path).runs

    assert run.question is None
    assert all(call.result == "" for call in run.tool_calls)
    assert "Rome" not in show_run(run)
