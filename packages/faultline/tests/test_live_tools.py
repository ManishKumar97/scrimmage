"""No tool runs for real unless the team says so: block mode and the tool preview."""

import json
import os
import subprocess
import sys
from pathlib import Path

from upstream import ASKS, upstream_server

from faultline.proxy.context import RunContext, from_env
from faultline.proxy.listing import ServerTools, read_all

HERE = Path(__file__).parent
FAULTLINE = str(Path(sys.executable).parent / "faultline")
# A stdio MCP server with two tools. It logs each tools/call it gets, and answers it.
SERVER = """
import json, sys
log = open(sys.argv[1], "a")
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "tools/list":
        result = {"tools": [{"name": "get_logs"}, {"name": "delete_pod"}]}
    elif message["method"] == "tools/call":
        log.write(line)
        log.flush()
        result = {"content": [{"type": "text", "text": "done"}], "isError": False}
    else:
        result = {"serverInfo": {"name": "kube", "version": "1"}}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
          flush=True)
"""


def request(request_id: int, method: str, **params) -> bytes:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    return (json.dumps(message) + "\n").encode()


def test_the_run_context_carries_block_mode():
    run = RunContext("/tmp/t", "ab" * 16, "cd" * 8, block_tools=True)

    assert run.env()["FAULTLINE_BLOCK_TOOLS"] == "1"
    assert from_env(run.env()) == run
    assert from_env(RunContext("/tmp/t", "ab" * 16, "cd" * 8).env()) == RunContext(
        "/tmp/t", "ab" * 16, "cd" * 8
    )


def test_in_block_mode_only_a_fault_answers_and_the_server_runs_no_tool(tmp_path):
    trace, log = tmp_path / "trace", tmp_path / "server.log"
    run = RunContext(
        str(trace), "ab" * 16, "cd" * 8, ("tool:get_logs:error",), block_tools=True
    )
    sent = [
        request(0, "initialize"),
        request(1, "tools/list"),
        request(2, "tools/call", name="get_logs", arguments={"service": "cart"}),
        request(3, "tools/call", name="delete_pod", arguments={"pod": "cart-1"}),
    ]
    with subprocess.Popen(
        [FAULTLINE, "mcp-wrap", "--", sys.executable, "-c", SERVER, str(log)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **run.env()},
    ) as wrapper:
        out, err = wrapper.communicate(b"".join(sent), timeout=60)

    answers = {m["id"]: m for m in map(json.loads, out.splitlines())}
    assert answers[2]["result"]["isError"] is True  # the fault's answer
    assert answers[3]["error"] == {
        "code": -32000,
        "message": "blocked by faultline: live tool",
    }
    assert not log.exists() or log.read_text() == ""  # no tools/call reached it
    assert b"faultline mcp-wrap: blocked delete_pod" in err
    [listing] = read_all(trace)
    assert listing == ServerTools(
        [sys.executable, "-c", SERVER, str(log)], "kube", ["get_logs", "delete_pod"]
    )
    spans = [
        {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}
        for path in trace.glob("mcp-*.jsonl")
        for text in path.read_text().splitlines()
        for span in json.loads(text)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    ]
    blocked = [s for s in spans if s["gen_ai.tool.name"] == "delete_pod"]
    assert [s["error.type"] for s in blocked] == ["blocked"]


def test_without_the_flag_the_run_is_a_preview_and_no_model_call_leaves(tmp_path):
    # The tiny agent calls the model first and starts its MCP server only after.
    agent = [sys.executable, str(HERE / "tiny_agent.py"), str(tmp_path / "log")]
    with upstream_server() as upstream:
        upstream.replies = [ASKS]
        result = subprocess.run(
            [FAULTLINE, "run", "--upstream", upstream.url]
            + ["--trace", str(tmp_path / "trace"), "--", *agent],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        seen = upstream.seen

    assert result.returncode == 2, result.stderr
    assert seen == []  # the proxy held the model call
    assert not (tmp_path / "log").exists()
    assert result.stderr.splitlines()[-4:] == [
        "faultline run: a preview. No tool ran: every tool call was blocked.",
        "No MCP server ran under faultline mcp-wrap.",
        (
            "Tools in the agent's own code (function tools) never pass through "
            "faultline, so they are not listed. Check them yourself."
        ),
        "Pass --allow-live-tools to run with these tools.",
    ]


def test_faultline_test_makes_no_run_without_the_flag(tmp_path):
    tasks = tmp_path / "tasks.toml"
    tasks.write_text('[[task]]\nid = "a"\ninput = "q"\n')
    agent = [sys.executable, str(HERE / "tiny_agent.py"), str(tmp_path / "log")]
    with upstream_server() as upstream:
        result = subprocess.run(
            [FAULTLINE, "test", "--tasks", str(tasks), "--fault", "model:timeout"]
            + ["--upstream", upstream.url, "--out", str(tmp_path / "out")]
            + ["--", *agent],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        seen = upstream.seen

    assert result.returncode == 2, result.stderr
    assert seen == []
    assert "faultline test: a preview. No tool ran" in result.stderr
    assert not (tmp_path / "out").exists()
    assert result.stdout == ""
