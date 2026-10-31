"""Build OTLP JSON span files from plain dicts, so a detector test says what the run did.

    run_from(tmp_path, {"calls": [{"name": "get_logs", "error": "ConnectionError"}]})

A step is {"finish": "stop", "error": None, "text": "", "calls": [...]}.
A call is {"name": "t", "id": "c1", "result": "ok", "error": None, "seconds": 1.0}.
Every key is optional. Steps run one after another, 100 seconds apart.
"""

import json
from pathlib import Path

from faultline.model import Run
from faultline.runs import read_runs

SECOND = 1_000_000_000
BASE = 1_700_000_000 * SECOND
STEP_GAP = 100 * SECOND
STATUS_ERROR = 2


def string(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def span(trace: str, span_id: str, parent: str | None, start: int, end: int) -> dict:
    span = {
        "traceId": trace,
        "spanId": span_id,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": [],
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def spans_of(steps: list[dict], trace: str = "t1") -> list[dict]:
    root = span(trace, "root", None, BASE, BASE + (len(steps) + 1) * STEP_GAP)
    root["attributes"].append(string("gen_ai.operation.name", "invoke_agent"))
    spans = [root]
    for number, step in enumerate(steps, start=1):
        start = BASE + number * STEP_GAP
        calls = step.get("calls", [])
        chat = span(trace, f"chat{number}", "root", start, start + SECOND)
        parts = [{"type": "text", "content": step.get("text", "")}]
        parts += [
            {"type": "tool_call", "id": call.get("id", f"c{number}.{i}"), "name": "t"}
            for i, call in enumerate(calls)
        ]
        message = [{"role": "assistant", "parts": parts}]
        chat["attributes"] += [
            string("gen_ai.operation.name", "chat"),
            string("gen_ai.output.messages", json.dumps(message)),
            {
                "key": "gen_ai.response.finish_reasons",
                "value": {
                    "arrayValue": {
                        "values": [{"stringValue": step.get("finish", "stop")}]
                    }
                },
            },
        ]
        if step.get("error"):
            chat["status"] = {"code": STATUS_ERROR}
            chat["attributes"].append(string("error.type", step["error"]))
        spans.append(chat)
        for i, call in enumerate(calls):
            begin = start + SECOND * (i + 1)
            end = begin + round(call.get("seconds", 1.0) * SECOND)
            tool = span(trace, f"tool{number}.{i}", f"chat{number}", begin, end)
            tool["attributes"] += [
                string("gen_ai.operation.name", "execute_tool"),
                string("gen_ai.tool.name", call.get("name", "t")),
                string("gen_ai.tool.call.id", call.get("id", f"c{number}.{i}")),
                string("gen_ai.tool.call.result", call.get("result", "ok")),
            ]
            if call.get("error"):
                tool["status"] = {"code": STATUS_ERROR}
                tool["attributes"].append(string("error.type", call["error"]))
            spans.append(tool)
    return spans


def write_trace(path: Path, *steps: dict, trace: str = "t1") -> Path:
    request = {
        "resourceSpans": [{"scopeSpans": [{"spans": spans_of(list(steps), trace)}]}]
    }
    with path.open("a") as file:
        file.write(json.dumps(request) + "\n")
    return path


def run_from(tmp_path: Path, *steps: dict) -> Run:
    """The one run in a span file made from `steps`."""
    path = write_trace(tmp_path / "spans.jsonl", *steps)
    [run] = read_runs(path).runs
    return run
