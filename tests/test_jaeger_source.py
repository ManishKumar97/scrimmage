"""Faultline reads from a live Jaeger the runs an agent wrote to a span file.

The Pydantic AI agents in foreign/ and the lab's agent send each span two ways at once:
to a file, and over OTLP/HTTP to a collector that stores it in Jaeger. `find` must see
the same runs, steps and tool calls from both.
Skipped unless FAULTLINE_JAEGER_URL names Jaeger's query URL, base path included,
for example http://localhost:16686/jaeger/ui.
FAULTLINE_OTLP_URL names the collector's OTLP/HTTP traces URL.
"""

import asyncio
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from fake_model import FakeClient, logs_call, text_reply, tool_reply
from fake_tools import logs_registry
from foreign.agents import TASKS
from openai import AsyncOpenAI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings
from test_foreign_pydantic_ai import OtlpJsonLinesExporter

from faultline import cli
from faultline.runs import show_run
from faultline.sources import FileSource, JaegerSource
from faultline_lab import ida, tracing
from faultline_lab.models import OpenAIModel

JAEGER = os.environ.get("FAULTLINE_JAEGER_URL", "")
OTLP = os.environ.get("FAULTLINE_OTLP_URL", "http://localhost:4318/v1/traces")

pytestmark = pytest.mark.skipif(not JAEGER, reason="FAULTLINE_JAEGER_URL is not set")


def run_every_task(service: str, path: Path) -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(SimpleSpanProcessor(OtlpJsonLinesExporter(path)))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=OTLP)))
    Agent.instrument_all(
        InstrumentationSettings(tracer_provider=provider, include_content=True)
    )
    try:
        for make_agent, prompt in TASKS:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                make_agent().run_sync(prompt)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
    finally:
        Agent.instrument_all(False)
        provider.shutdown()


def wait_for(source: JaegerSource, trace_ids: set[str]) -> list:
    """The source's runs of these traces, once Jaeger holds them all."""
    deadline = time.monotonic() + 30
    while True:
        found = [run for run in source.runs() if run.trace_id in trace_ids]
        if len(found) == len(trace_ids):
            return found
        assert time.monotonic() < deadline, f"Jaeger holds {len(found)} traces"
        time.sleep(1)  # the collector stores spans a moment after it accepts them


def find_output(args: list[str], capsys) -> list[str]:
    """What `find --runs` prints, without the line that says where it read from."""
    assert cli.main(["find", "--runs", *args]) == 0
    lines = capsys.readouterr().out.splitlines()
    return [line for line in lines if not line.startswith("read ")]


def test_jaeger_gives_the_runs_steps_and_tool_calls_the_span_file_holds(
    tmp_path: Path, capsys
):
    service = f"faultline-e2e-{uuid.uuid4().hex[:8]}"  # only this test's traces
    path = tmp_path / "spans.jsonl"
    run_every_task(service, path)
    from_file = list(FileSource(path).runs())

    found = wait_for(
        JaegerSource(JAEGER, service, timedelta(minutes=10)),
        {run.trace_id for run in from_file},
    )

    assert len(from_file) == len(TASKS)
    assert [show_run(run) for run in found] == [show_run(run) for run in from_file]
    assert sum(len(run.tool_calls) for run in found) == 4
    jaeger = ["--source", f"jaeger:{JAEGER}", "--service", service, "--since", "10m"]
    assert find_output(jaeger, capsys) == find_output([str(path)], capsys)


def logs(service: str) -> str:
    if service == "checkout":
        raise RuntimeError("pod not found")
    return f"logs of {service}"


def test_the_lab_agent_exports_to_jaeger_when_the_otlp_variable_is_set(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", OTLP)
    path = tmp_path / "spans.jsonl"
    provider = tracing.file_provider(path)
    client = FakeClient(
        tool_reply(logs_call("cart"), logs_call("checkout")),
        text_reply("Cart is down."),
    )
    model = OpenAIModel(cast(AsyncOpenAI, client), "fake-model")
    tracer = tracing.Tracing(provider.get_tracer("e2e"))
    asyncio.run(ida.run("why?", model, tools=logs_registry(logs), tracing=tracer))
    provider.shutdown()
    [from_file] = FileSource(path).runs()

    # The lab's service name is shared with other runs; keep only this one.
    window = timedelta(minutes=10)
    [found] = wait_for(
        JaegerSource(JAEGER, tracing.AGENT_NAME, window), {from_file.trace_id}
    )

    assert show_run(found) == show_run(from_file)
    # A tool error and a finish reason, which Jaeger stores differently from a file.
    assert [call.error_type for call in found.tool_calls] == [None, "tool_error"]
    assert found.steps[0].finish_reason == "tool_calls"
