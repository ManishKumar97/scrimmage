"""model:truncated through `faultline run`: the real SDKs read the cut reply as their own.

One agent makes four calls, plain and streamed, on the openai and anthropic SDKs.
A plain reply asks for a tool, so the test also shows the tool call is dropped.
"""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ida_harness import SCRIPT, fake_llm, ida, ida_env, needs_cluster, run

from faultline.runs import read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")

AGENT = """
import anthropic, openai
claude, gpt = anthropic.Anthropic(), openai.OpenAI()
said = claude.messages.create(
    model="claude", max_tokens=50, messages=[{"role": "user", "content": "a"}]
)
print([b.type for b in said.content], said.content[0].text, said.stop_reason)
with claude.messages.stream(
    model="claude", max_tokens=50, messages=[{"role": "user", "content": "b"}]
) as stream:
    print(repr("".join(stream.text_stream)), stream.get_final_message().stop_reason)
done = gpt.chat.completions.create(model="gpt", messages=[{"role": "user", "content": "c"}])
choice = done.choices[0]
print(choice.message.content, choice.message.tool_calls, choice.finish_reason)
chunks = list(gpt.chat.completions.create(
    model="gpt", messages=[{"role": "user", "content": "d"}], stream=True
))
text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
print(repr(text), [c.choices[0].finish_reason for c in chunks if c.choices][-1])
"""

ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude",
    "content": [
        {"type": "text", "text": "Reading the logs"},
        {"type": "tool_use", "id": "toolu_1", "name": "get_logs", "input": {}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 2},
}
OPENAI_REPLY = {
    "id": "c1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Reading the logs",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_logs", "arguments": "{}"},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def sse(events: list[tuple[str | None, dict | str]]) -> bytes:
    out = []
    for name, data in events:
        text = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {name}\n" if name else "") + f"data: {text}\n\n")
    return "".join(out).encode()


def chunk(delta: dict, finish: str | None = None) -> tuple[None, dict]:
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "gpt"}
    return None, {**base, "choices": [choice]}


OPENAI_STREAM = sse(
    [
        chunk({"role": "assistant", "content": ""}),
        chunk({"content": "the cart "}),
        chunk({"content": "is down"}),
        chunk({}, "stop"),
        (None, "[DONE]"),
    ]
)
ANTHROPIC_STREAM = sse(
    [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {**ANTHROPIC_REPLY, "content": [], "stop_reason": None},
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "looking at logs"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 3},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        anthropic = self.path.endswith("/messages")
        if request.get("stream"):
            body, kind = (ANTHROPIC_STREAM if anthropic else OPENAI_STREAM), "event"
        else:
            reply = ANTHROPIC_REPLY if anthropic else OPENAI_REPLY
            body, kind = json.dumps(reply).encode(), "json"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/event-stream" if kind == "event" else "application/json",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass


@contextmanager
def serving() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_each_sdk_reads_a_cut_reply_as_stopped_at_the_token_limit(tmp_path):
    trace = tmp_path / "trace"
    env = {k: v for k, v in os.environ.items() if "_BASE_URL" not in k}
    env.update(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="o")
    faults = [f"--fault=model:truncated@{step}" for step in (1, 2, 3, 4)]
    with serving() as url:
        env.update(OPENAI_BASE_URL=url + "/v1", ANTHROPIC_BASE_URL=url)
        result = subprocess.run(
            [FAULTLINE, "run", *faults, "--trace", str(trace)]
            + ["--", sys.executable, "-c", AGENT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "['text'] Reading  max_tokens",  # 16 characters cut to 8, tool_use dropped
        "'looking' max_tokens",
        "Reading  None length",
        "'the cart' length",
    ]
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert [(s.finish_reason, s.injected) for s in read.steps] == [
        ("length", f"model:truncated@{step}") for step in (1, 2, 3, 4)
    ]
    assert all(not s.tool_calls for s in read.steps)


def test_a_reply_that_cannot_be_cut_passes_through_and_says_why(tmp_path):
    trace = tmp_path / "trace"
    env = {k: v for k, v in os.environ.items() if "_BASE_URL" not in k}
    env["OPENAI_API_KEY"] = "o"
    agent = (
        "import openai\n"
        "try:\n"
        "    openai.OpenAI(max_retries=0).chat.completions.create(model='m', messages=[])\n"
        "except openai.APIStatusError as error:\n"
        "    print(error.status_code)\n"
    )
    with fake_llm(tmp_path, []) as upstream:  # an empty script answers 500
        result = subprocess.run(
            [FAULTLINE, "run", "--fault", "model:truncated", "--trace", str(trace)]
            + ["--upstream", upstream, "--", sys.executable, "-c", agent],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert result.stdout == "500\n"
    assert (
        "faultline proxy: model:truncated not applied to step 1: "
        "the upstream answered 500"
    ) in result.stderr
    assert result.returncode == 78  # the run tested nothing that was asked
    line = (trace / "model.jsonl").read_text()
    assert '"faultline.injected.skipped"' in line
    assert "model:truncated: the upstream answered 500" in line


@needs_cluster
def test_the_ida_gets_a_reply_cut_at_the_token_limit(tmp_path):
    trace = tmp_path / "trace"
    with fake_llm(tmp_path, SCRIPT) as upstream:
        result = run(
            [FAULTLINE, "run", "--fault", "model:truncated@2"]
            + ["--expect", "ROOT CAUSE SERVICE: cart"]
            + ["--base-url-env", "GROQ_BASE_URL", "--upstream", upstream]
            + ["--trace", str(trace), "--", *ida()],
            ida_env(tmp_path),
            tmp_path,
        )

    assert result.returncode == 0, result.stderr
    # SCRIPT's answer is 64 characters. The IDA got the first 32 and stopped.
    assert result.stdout == "The cart service logs show the f\n"
    assert "[stop] truncated after 2 steps" in result.stderr
    [read] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert [(s.finish_reason, s.injected) for s in read.steps] == [
        ("tool_call", None),
        ("length", "model:truncated@2"),
    ]
    assert result.stderr.splitlines()[-1] == (
        'verdict: failed_silently (answer lacks "ROOT CAUSE SERVICE: cart"; '
        "injected model:truncated@2)"
    )
