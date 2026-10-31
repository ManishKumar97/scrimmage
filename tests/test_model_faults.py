"""Model faults through `faultline run`, seen by real SDK agents and by the lab's IDA."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ida_harness import SCRIPT, fake_llm, ida, ida_env, needs_cluster, run

from faultline.runs import read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")
AGENT = str(Path(__file__).parent / "openai_agent.py")
# Nothing listens here. A fault that answers every attempt sends no call upstream.
NOWHERE = "http://127.0.0.1:9/v1"


def chat_spans(trace: Path) -> list[dict]:
    """The chat spans of the proxy's file, with their attributes as a plain dict."""
    lines = (trace / "model.jsonl").read_text().splitlines()
    spans = [
        span
        for line in lines
        for resource in json.loads(line)["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
        if span["name"].startswith("chat")
    ]
    for span in spans:
        span["attrs"] = {a["key"]: a["value"] for a in span["attributes"]}
    return spans


def sdk_agent(tmp_path: Path, fault: str, upstream: str, *args: str):
    trace = tmp_path / "trace"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
    env["OPENAI_API_KEY"] = "fake"
    result = subprocess.run(
        [FAULTLINE, "run", "--fault", fault, "--trace", str(trace)]
        + ["--upstream", upstream, "--", sys.executable, AGENT, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, trace


def test_a_hung_call_reaches_the_sdk_agent_as_its_own_timeout(tmp_path):
    result, trace = sdk_agent(tmp_path, "model:timeout", NOWHERE, "0.5")

    assert result.stdout == "agent caught openai.APITimeoutError\n", result.stderr
    assert result.returncode == 4
    [chat] = chat_spans(trace)  # one step, every attempt of it hung
    assert chat["attrs"]["faultline.proxy.attempts"] == {"intValue": "3"}
    assert chat["attrs"]["error.type"] == {"stringValue": "timeout"}
    assert chat["attrs"]["faultline.injected"] == {"stringValue": "model:timeout"}
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert [s.error_type for s in read.steps] == ["timeout"]
    assert result.stderr.splitlines()[-1] == (
        "verdict: failed_loudly (exit code 4; injected model:timeout)"
    )


@needs_cluster
def test_the_ida_sees_a_timeout_on_the_chosen_model_call(tmp_path):
    trace = tmp_path / "trace"
    env = ida_env(tmp_path, MODEL_TIMEOUT="1", MODEL_MAX_RETRIES="1")
    with fake_llm(tmp_path, SCRIPT) as upstream:
        result = run(
            [FAULTLINE, "run", "--fault", "model:timeout@2", "--expect", "cart"]
            + ["--base-url-env", "GROQ_BASE_URL", "--upstream", upstream]
            + ["--trace", str(trace), "--", *ida()],
            env,
            tmp_path,
        )

    assert result.returncode == 0, result.stderr
    # Step 1 went through. Step 2 hung, and the IDA's model client gave up on it.
    assert result.stdout == "Model API failed: Request timed out.\n"
    assert "[stop] model_error after 1 steps" in result.stderr
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert [(s.error_type, s.injected) for s in read.steps] == [
        (None, None),
        ("timeout", "model:timeout@2"),
    ]
    [first, second] = chat_spans(trace)
    assert first["attrs"]["faultline.proxy.attempts"] == {"intValue": "1"}
    assert second["attrs"]["faultline.proxy.attempts"] == {"intValue": "2"}
    assert result.stderr.splitlines()[-1] == (
        "verdict: failed_loudly (no answer; injected model:timeout@2)"
    )
