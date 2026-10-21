import asyncio
import json
import re
from pathlib import Path
from typing import cast

import pytest
from fake_model import FakeClient, logs_call, rejected, text_reply, tool_reply
from fake_tools import logs_registry
from openai import AsyncOpenAI
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanContext, SpanKind, StatusCode

from faultline import ida, tracing
from faultline.models import OpenAIModel
from faultline.tools import Registry
from faultline.tracing import Tracing


def logs(service: str) -> str:
    if service == "checkout":
        raise RuntimeError("pod not found")
    return f"logs of {service}"


TOOLS = logs_registry(logs)


def traced(
    client: FakeClient,
    budget: ida.Budget = ida.DEFAULT_BUDGET,
    content: bool = False,
) -> tuple[ida.RunResult | None, dict[str, list[ReadableSpan]]]:
    """Run the loop with an in-memory exporter. Spans come back grouped by name prefix."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    model = OpenAIModel(cast(AsyncOpenAI, client), "fake-model", provider="groq")
    config = Tracing(provider.get_tracer("test"), content)
    result = None
    try:
        result = asyncio.run(
            ida.run("why?", model, budget, tools=TOOLS, tracing=config)
        )
    except RuntimeError:
        pass  # the test checks the span of the run that blew up
    grouped: dict[str, list[ReadableSpan]] = {}
    for span in exporter.get_finished_spans():
        grouped.setdefault(span.name.split()[0], []).append(span)
    return result, grouped


def context(span: ReadableSpan) -> SpanContext:
    assert span.context
    return span.context


def attrs(span: ReadableSpan) -> dict:
    return dict(span.attributes or {})


def two_tool_run() -> FakeClient:
    return FakeClient(
        tool_reply(logs_call("cart"), logs_call("checkout")),
        text_reply("Cart is down."),
    )


def test_a_run_writes_a_root_span_and_one_span_per_model_and_tool_call():
    result, spans = traced(two_tool_run())

    assert result and result.stop_reason == "done"
    assert {name: len(found) for name, found in spans.items()} == {
        "invoke_agent": 1,
        "chat": 2,
        "execute_tool": 2,
    }
    (root,) = spans["invoke_agent"]
    assert root.parent is None
    # Model calls and tool calls are all children of the run span, so they share a trace.
    for child in spans["chat"] + spans["execute_tool"]:
        assert child.parent and child.parent.span_id == context(root).span_id
        assert context(child).trace_id == context(root).trace_id


def test_model_spans_carry_the_genai_attributes_and_token_counts():
    _, spans = traced(two_tool_run())

    first, second = sorted(spans["chat"], key=lambda s: s.start_time or 0)
    assert spans["chat"][0].name == "chat fake-model"
    assert first.kind == SpanKind.CLIENT
    assert {t.kind for t in spans["execute_tool"]} == {SpanKind.INTERNAL}
    assert attrs(first) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "groq",
        "gen_ai.request.model": "fake-model",
        "gen_ai.response.model": "fake-model",
        "gen_ai.response.id": "fake",
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 20,
        "gen_ai.response.finish_reasons": ("tool_calls",),
        "faultline.step": 1,
    }
    assert attrs(second)["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs(second)["faultline.step"] == 2


def test_a_failing_tool_span_is_an_error_with_the_error_text():
    _, spans = traced(two_tool_run())

    by_tool = {attrs(s)["gen_ai.tool.call.id"]: s for s in spans["execute_tool"]}
    ok, failed = by_tool["call_0"], by_tool["call_1"]
    assert ok.name == "execute_tool get_logs"
    assert attrs(ok)["gen_ai.tool.name"] == "get_logs"
    assert attrs(ok)["gen_ai.tool.type"] == "function"
    assert attrs(ok)["faultline.step"] == 1
    assert ok.status.status_code == StatusCode.UNSET
    assert failed.status.status_code == StatusCode.ERROR
    assert failed.status.description == "tool_error"  # the text needs content on
    assert attrs(failed)["error.type"] == "tool_error"


def test_the_root_span_has_the_stop_reason_steps_tokens_and_budget():
    result, spans = traced(two_tool_run(), ida.Budget(max_steps=7))

    (root,) = spans["invoke_agent"]
    assert result
    assert attrs(root) == {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "faultline-ida",
        "gen_ai.provider.name": "groq",
        "gen_ai.request.model": "fake-model",
        "faultline.budget.max_steps": 7,
        "faultline.budget.max_tokens": 60_000,
        "faultline.budget.max_seconds": 120.0,
        "faultline.stop_reason": "done",
        "faultline.steps": 2,
        "faultline.tokens": 240,
        "faultline.seconds": result.seconds,
    }
    assert root.status.status_code == StatusCode.UNSET


def test_a_run_that_stops_for_a_limit_is_an_error_on_the_root_span():
    client = FakeClient(tool_reply(logs_call("cart")), tool_reply(logs_call("cart")))

    _, spans = traced(client, ida.Budget(max_steps=2))

    (root,) = spans["invoke_agent"]
    assert attrs(root)["faultline.stop_reason"] == "step_limit"
    assert root.status.status_code == StatusCode.ERROR
    assert root.status.description == "step_limit"


def test_a_model_api_error_is_recorded_on_the_model_span_and_the_root():
    client = FakeClient(rejected(500, "upstream is down"))

    _, spans = traced(client)

    (chat,) = spans["chat"]
    assert chat.status.status_code == StatusCode.ERROR
    assert chat.status.description == "ModelError"
    assert attrs(chat)["error.type"] == "ModelError"
    (root,) = spans["invoke_agent"]
    assert attrs(root)["faultline.stop_reason"] == "model_error"


class SlowClient(FakeClient):
    async def create(self, **kwargs):
        await asyncio.sleep(5)  # cancelled by the deadline long before this ends
        return text_reply("too late")


def test_the_deadline_ends_and_exports_every_open_span():
    result, spans = traced(SlowClient(), ida.Budget(max_seconds=0.1))

    assert result and result.stop_reason == "time_limit"
    (root,) = spans["invoke_agent"]
    (chat,) = spans["chat"]
    assert attrs(root)["faultline.stop_reason"] == "time_limit"
    assert root.status.status_code == StatusCode.ERROR
    assert chat.status.status_code == StatusCode.ERROR
    assert attrs(chat)["error.type"] == "CancelledError"
    assert chat.end_time and chat.end_time <= (root.end_time or 0)


def test_a_crash_in_the_loop_still_ends_the_root_span():
    client = FakeClient(RuntimeError("bug in the adapter"))

    result, spans = traced(client)

    assert result is None
    (root,) = spans["invoke_agent"]
    assert root.status.status_code == StatusCode.ERROR
    assert attrs(root)["error.type"] == "RuntimeError"
    assert root.status.description == "RuntimeError"


def test_spans_hold_no_prompts_or_tool_output_by_default():
    _, spans = traced(two_tool_run())

    everything = json.dumps([attrs(s) for found in spans.values() for s in found])
    assert "logs of cart" not in everything
    assert "why?" not in everything
    assert "gen_ai.input.messages" not in everything
    assert "gen_ai.tool.call.arguments" not in everything


def test_with_content_on_the_error_text_is_kept_in_the_status():
    _, spans = traced(two_tool_run(), content=True)

    failed = next(
        s for s in spans["execute_tool"] if attrs(s)["gen_ai.tool.call.id"] == "call_1"
    )
    assert "pod not found" in (failed.status.description or "")


def test_with_content_on_spans_hold_what_a_replay_needs():
    _, spans = traced(two_tool_run(), content=True)

    (root,) = spans["invoke_agent"]
    assert attrs(root)["faultline.question"] == "why?"
    assert attrs(root)["faultline.answer"] == "Cart is down."
    first, second = sorted(spans["chat"], key=lambda s: s.start_time or 0)
    assert attrs(first)["gen_ai.system_instructions"] == ida.SYSTEM_PROMPT
    assert json.loads(attrs(first)["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "why?"}]}
    ]
    names = [
        t["function"]["name"]
        for t in json.loads(attrs(first)["gen_ai.tool.definitions"])
    ]
    assert names == ["get_logs"]
    # The second call sees the first reply and both tool results.
    sent = json.loads(attrs(second)["gen_ai.input.messages"])
    assert [m["role"] for m in sent] == ["user", "assistant", "tool"]
    assert sent[2]["parts"][0]["response"] == "logs of cart"
    assert sent[2]["parts"][1]["is_error"] is True
    output = json.loads(attrs(second)["gen_ai.output.messages"])
    assert output[0]["parts"] == [{"type": "text", "content": "Cart is down."}]
    assert output[0]["finish_reason"] == "stop"
    assert [m["finish_reason"] for m in sent if m["role"] == "assistant"] == [
        "tool_call"
    ]
    by_id = {attrs(s)["gen_ai.tool.call.id"]: attrs(s) for s in spans["execute_tool"]}
    assert by_id["call_0"]["gen_ai.tool.call.result"] == "logs of cart"
    assert json.loads(by_id["call_0"]["gen_ai.tool.call.arguments"]) == {
        "service": "cart"
    }


def test_the_file_exporter_writes_otlp_json_lines_that_parse_back(tmp_path: Path):
    path = tmp_path / "nested" / "traces.jsonl"
    provider = tracing.file_provider(path)
    model = OpenAIModel(cast(AsyncOpenAI, two_tool_run()), "fake-model")

    asyncio.run(
        ida.run("why?", model, tools=TOOLS, tracing=Tracing(provider.get_tracer("t")))
    )
    provider.shutdown()

    lines = path.read_text().splitlines()
    exported = [
        span
        for line in lines
        for resource in json.loads(line)["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert sorted(s["name"] for s in exported) == [
        "chat fake-model",
        "chat fake-model",
        "execute_tool get_logs",
        "execute_tool get_logs",
        "invoke_agent faultline-ida",
    ]
    assert len({s["traceId"] for s in exported}) == 1
    root = next(s for s in exported if s["name"].startswith("invoke_agent"))
    stop = next(a for a in root["attributes"] if a["key"] == "faultline.stop_reason")
    assert stop["value"] == {"stringValue": "done"}
    resource = json.loads(lines[0])["resourceSpans"][0]["resource"]["attributes"]
    assert {
        "key": "service.name",
        "value": {"stringValue": "faultline-ida"},
    } in resource


def test_a_second_run_appends_a_new_trace_to_the_same_file(tmp_path: Path):
    path = tmp_path / "traces.jsonl"
    for _ in range(2):
        provider = tracing.file_provider(path)
        model = OpenAIModel(cast(AsyncOpenAI, FakeClient(text_reply("ok"))), "m")
        asyncio.run(
            ida.run("q", model, tools=TOOLS, tracing=Tracing(provider.get_tracer("t")))
        )
        provider.shutdown()

    trace_ids = {
        span["traceId"]
        for line in path.read_text().splitlines()
        for resource in json.loads(line)["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    }
    assert len(trace_ids) == 2


def test_a_file_that_cannot_be_written_does_not_crash_the_run(tmp_path: Path):
    blocked = tmp_path / "is-a-dir.jsonl"
    blocked.mkdir()
    provider = tracing.file_provider(blocked)
    model = OpenAIModel(cast(AsyncOpenAI, FakeClient(text_reply("ok"))), "m")

    result = asyncio.run(
        ida.run("q", model, tools=TOOLS, tracing=Tracing(provider.get_tracer("t")))
    )

    assert result.stop_reason == "done"


@pytest.mark.parametrize("flag", ["true", "false"])
def test_the_content_setting_reads_from_the_environment(flag, monkeypatch):
    from faultline.settings import Settings

    monkeypatch.setenv("PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("TRACE_CONTENT", flag)
    monkeypatch.setenv("TRACE_FILE", "elsewhere.jsonl")

    settings = Settings()

    assert settings.trace_content is (flag == "true")
    assert settings.trace_file == Path("elsewhere.jsonl")


SECRET = "sk-planted-marker-123"


def leaky_tools() -> Registry:
    registry = Registry()

    @registry.tool
    def get_logs(service: str) -> str:
        """Get logs."""
        raise RuntimeError(f"password={SECRET}")

    return registry


def everything_in(spans: dict[str, list[ReadableSpan]]) -> str:
    """Every attribute, status and event of every span, as one string."""
    return json.dumps(
        [
            [
                attrs(s),
                s.status.description,
                [(e.name, dict(e.attributes or {})) for e in s.events],
            ]
            for found in spans.values()
            for s in found
        ],
        default=str,
    )


def run_leaky(client: FakeClient, budget: ida.Budget, content: bool = False):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    model = OpenAIModel(cast(AsyncOpenAI, client), "fake-model")
    config = Tracing(provider.get_tracer("test"), content)
    try:
        asyncio.run(ida.run("why?", model, budget, tools=leaky_tools(), tracing=config))
    except RuntimeError:
        pass
    return {"all": list(exporter.get_finished_spans())}


@pytest.mark.parametrize(
    ("client", "budget", "reason"),
    [
        (
            FakeClient(tool_reply(logs_call("a")), text_reply(f"found {SECRET}")),
            ida.DEFAULT_BUDGET,
            "done",
        ),
        (
            FakeClient(*[tool_reply(logs_call("a")) for _ in range(3)]),
            ida.DEFAULT_BUDGET,
            "tool_errors",
        ),
        (
            FakeClient(text_reply(f"cut {SECRET}", "length")),
            ida.DEFAULT_BUDGET,
            "truncated",
        ),
        (
            FakeClient(tool_reply(logs_call("a")), tool_reply(logs_call("a"))),
            ida.Budget(max_steps=1),
            "step_limit",
        ),
        (
            FakeClient(rejected(500, f"body {SECRET}")),
            ida.DEFAULT_BUDGET,
            "model_error",
        ),
        (FakeClient(RuntimeError(f"bug {SECRET}")), ida.DEFAULT_BUDGET, "crash"),
    ],
)
def test_nothing_from_a_prompt_answer_or_tool_output_leaks_with_content_off(
    client, budget, reason
):
    spans = run_leaky(client, budget)

    assert spans["all"]
    assert SECRET not in everything_in(spans), reason
    on = run_leaky(
        FakeClient(tool_reply(logs_call("a")), text_reply(f"found {SECRET}")),
        ida.DEFAULT_BUDGET,
        content=True,
    )
    assert SECRET in everything_in(
        on
    )  # the marker is detectable, so the check can fail


def test_the_file_uses_hex_ids_and_integer_enums_as_otlp_json_requires(tmp_path: Path):
    path = tmp_path / "traces.jsonl"
    provider = tracing.file_provider(path)
    model = OpenAIModel(cast(AsyncOpenAI, two_tool_run()), "fake-model")
    asyncio.run(
        ida.run("why?", model, tools=TOOLS, tracing=Tracing(provider.get_tracer("t")))
    )
    provider.shutdown()

    exported = [
        span
        for line in path.read_text().splitlines()
        for resource in json.loads(line)["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    for span in exported:
        assert re.fullmatch("[0-9a-f]{32}", span["traceId"])
        assert re.fullmatch("[0-9a-f]{16}", span["spanId"])
        assert isinstance(span["kind"], int)
        assert isinstance(span["status"].get("code", 0), int)
        # int64 fields stay strings, field names stay camelCase.
        assert re.fullmatch("[0-9]+", span["startTimeUnixNano"])
    root = next(s for s in exported if s["name"].startswith("invoke_agent"))
    assert "parentSpanId" not in root or root["parentSpanId"] == ""
    child = next(s for s in exported if s["name"].startswith("chat"))
    assert child["parentSpanId"] == root["spanId"]
    assert child["kind"] == 3  # CLIENT
    failed = next(
        s for s in exported if s["name"].startswith("execute_tool") and s["status"]
    )
    assert failed["status"]["code"] == 2  # ERROR


def test_token_attributes_are_left_out_when_the_provider_reports_no_usage():
    no_usage = text_reply("hi").model_copy(update={"usage": None})

    _, spans = traced(FakeClient(no_usage))

    (chat,) = spans["chat"]
    assert not any(key.startswith("gen_ai.usage") for key in attrs(chat))
