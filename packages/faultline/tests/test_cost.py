"""`faultline test` estimates the cost before the rest of its runs, and stops at a cap."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sse import openai_chunk, sse
from upstream import ANSWERS, ASKS, openai_reply, upstream_server

from faultline.config import CostConfig
from faultline.cost import Prices, dollars
from faultline.reproduce import plan_line, prices_of
from faultline.runs import read_runs_many

HERE = Path(__file__).parent
FAULTLINE = str(Path(sys.executable).parent / "faultline")
# One model call with the task as its question. Each reply reports 50 + 7 tokens.
ASKER = """
import json, os, sys, urllib.request
body = json.dumps({"model": "m", "messages": [{"role": "user", "content": sys.argv[1]}]})
request = urllib.request.Request(
    os.environ["OPENAI_BASE_URL"] + "/chat/completions", body.encode(),
    {"Content-Type": "application/json"})
with urllib.request.urlopen(request) as reply:
    print(json.load(reply)["choices"][0]["message"]["content"])
"""
TASKS = """
[[task]]
id = "a"
input = "Why does the cart fail?"

[[task]]
id = "b"
input = "Why are ads slow?"
"""
# $1,000 per million tokens: one run of 57 tokens costs $0.057.
PRICES = ["--price-in", "1000", "--price-out", "1000"]


def test_the_plan_multiplies_the_first_runs_tokens_by_every_run():
    prices = Prices(3.0, 15.0)

    line = plan_line(2, 1, 3, 3, (3_000, 1_000), prices)

    # 12 runs of 4,000 tokens; each costs 3,000 x $3 + 1,000 x $15 per million.
    assert line == (
        "plan: 2 tasks x (3 clean + 3 faulted) = 12 runs, about 48,000 tokens, "
        "about $0.29"
    )
    assert plan_line(1, 2, 1, 2, (10, 0), None) == (
        "plan: 1 task x (1 clean + 2 faulted x 2 faults) = 5 runs, about 50 tokens, "
        "cost unknown (no price: set [cost] input_per_mtok and output_per_mtok, "
        "or pass --price-in and --price-out)"
    )
    assert plan_line(1, 1, 1, 1, (0, 0), prices).endswith(
        "tokens unknown (the first run recorded no token usage)"
    )
    assert dollars(0.001) == "under $0.01"
    assert dollars(0) == "$0.00"


def test_flag_prices_win_over_the_config_and_both_are_needed():
    def args(price_in=None, price_out=None):
        return argparse.Namespace(price_in=price_in, price_out=price_out)

    assert prices_of(args(), CostConfig()) is None
    assert prices_of(args(1.0), CostConfig(5.0, 6.0)) == Prices(1.0, 6.0)
    with pytest.raises(ValueError, match="give both prices"):
        prices_of(args(1.0), CostConfig())


def faultline_test(tmp_path: Path, upstream: str, *flags: str, agent=None):
    tasks = tmp_path / "tasks.toml"
    tasks.write_text(TASKS)
    return subprocess.run(
        [
            FAULTLINE,
            "test",
            "--allow-live-tools",
            "--tasks",
            str(tasks),
            "--upstream",
            upstream,
        ]
        + ["--out", str(tmp_path / "out"), *flags]
        + ["--", *(agent or [sys.executable, "-c", ASKER])],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=tmp_path,
    )


def test_the_estimate_comes_after_the_first_run_and_the_cap_stops_the_rest(tmp_path):
    with upstream_server() as upstream:
        upstream.chunks = [openai_reply({"content": "the cart"}, "stop")]
        result = faultline_test(
            tmp_path,
            upstream.url,
            "--fault",
            "model:rate_limit",
            "--clean-runs",
            "2",
            "--runs",
            "1",
            *PRICES,
            "--max-cost",
            "0.1",
        )
        calls = len(upstream.seen)

    assert result.returncode == 3, result.stderr
    lines = result.stderr.splitlines()
    plan = lines.index(
        "plan: 2 tasks x (2 clean + 1 faulted) = 6 runs, about 342 tokens, about $0.34"
    )
    assert lines[plan - 1].startswith("[1/6] a clean #1: passed")
    assert lines[plan + 1].startswith("[2/6] a clean #2: passed")
    # Two runs cost $0.114, past the $0.10 cap, so the third never starts.
    assert lines[plan + 2] == (
        "faultline test: stopped after 2 of 6 runs: they cost about $0.11, "
        "and the --max-cost cap is $0.10"
    )
    assert calls == 2
    assert sorted(p.name for p in (tmp_path / "out" / "a").iterdir()) == ["clean"]
    assert (
        json.loads((tmp_path / "out" / "matrix.json").read_text())["rows"][0]["runs"]
        == 2
    )


def test_the_proxy_refuses_model_calls_once_the_cap_is_passed_mid_run(tmp_path):
    agent = [sys.executable, str(HERE / "tiny_agent.py"), str(tmp_path / "log")]
    with upstream_server() as upstream:
        upstream.replies = [ASKS, ANSWERS]
        # Step 1 costs $0.057 and passes the $0.05 cap. Step 2 is refused.
        result = faultline_test(
            tmp_path,
            upstream.url,
            "--fault",
            "model:rate_limit",
            "--clean-runs",
            "1",
            "--runs",
            "1",
            *PRICES,
            "--max-cost",
            "0.05",
            agent=agent,
        )
        calls = len(upstream.seen)

    assert result.returncode == 3, result.stderr
    assert calls == 1
    run = tmp_path / "out" / "a" / "clean" / "1"
    [read] = read_runs_many(sorted((run / "trace").glob("*.jsonl"))).runs
    assert [step.error_type for step in read.steps] == [None, "cost_cap"]
    spans = [
        span
        for line in (run / "trace" / "model.jsonl").read_text().splitlines()
        for span in json.loads(line)["resourceSpans"][0]["scopeSpans"][0]["spans"]
        if span["name"].startswith("chat")
    ]
    [event] = spans[1]["events"]
    assert {"key": "http.response.status_code", "value": {"intValue": "402"}} in event[
        "attributes"
    ]
    assert "HTTP Error 402" in (run / "stderr.txt").read_text()
    saved = json.loads((run / "result.json").read_text())
    assert saved["verdict"] == "failed_loudly"


def test_a_cap_without_prices_is_refused_before_any_run(tmp_path):
    result = faultline_test(
        tmp_path, "http://127.0.0.1:9", "--fault", "model:timeout", "--max-cost", "1"
    )

    assert result.returncode == 2
    assert "--max-cost needs the prices" in result.stderr
    assert not (tmp_path / "out").exists()


# Two model calls; asks for a stream when given a second argument.
TWO_CALLS = """
import json, os, sys, urllib.request
for _ in range(2):  # the same request twice, as the reviewer's two_call.py probe
    body = json.dumps({"model": "m", "stream": len(sys.argv) > 2,
                       "messages": [{"role": "user", "content": sys.argv[1]}]})
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"] + "/chat/completions", body.encode(),
        {"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as reply:
        text = reply.read().decode()
print(text[:40])
"""
NO_USAGE_REPLY = json.dumps(
    {
        "id": "r",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "the cart"},
                "finish_reason": "stop",
            }
        ],
    }
).encode()
# An OpenAI stream as the SDK asks for it by default: no include_usage, no usage.
NO_USAGE_STREAM = sse(
    [
        openai_chunk({"role": "assistant", "content": "the cart"}),
        openai_chunk({}, "stop"),
        (None, "[DONE]"),
    ]
)


@pytest.mark.parametrize(
    ("chunk", "kind", "stream"),
    [
        (NO_USAGE_REPLY, "application/json", []),
        (NO_USAGE_STREAM, "text/event-stream", ["stream"]),
    ],
    ids=["plain reply", "stream without include_usage"],
)
def test_a_cap_fails_closed_when_the_replies_report_no_usage(
    tmp_path, chunk, kind, stream
):
    with upstream_server() as upstream:
        upstream.chunks = [chunk]
        upstream.reply_headers = [("Content-Type", kind)]
        result = faultline_test(
            tmp_path,
            upstream.url,
            "--fault",
            "model:rate_limit",
            "--clean-runs",
            "2",
            "--runs",
            "1",
            *PRICES,
            "--max-cost",
            "100",
            agent=[sys.executable, "-c", TWO_CALLS, "{input}", *stream],
        )
        calls = len(upstream.seen)

    assert result.returncode == 3, result.stderr
    assert (
        "faultline test: cannot enforce --max-cost: the replies report no token "
        "usage; stopped after 1 of 6 runs"
    ) in result.stderr
    # The first reply had no usage, so the proxy refused the run's second call.
    assert calls == 1
    run = tmp_path / "out" / "a" / "clean" / "1"
    [read] = read_runs_many(sorted((run / "trace").glob("*.jsonl"))).runs
    # One step: the refused call repeats the first, so it is attempt 2 of step 1.
    assert [step.error_type for step in read.steps] == ["cost_unknown"]
    assert "HTTP Error 402" in (run / "stderr.txt").read_text()
