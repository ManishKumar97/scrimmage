"""Streamed replies from the real openai and anthropic SDKs, recorded by the proxy."""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from faultline.runs import read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")

AGENT = """
import anthropic, openai
with anthropic.Anthropic().messages.stream(
    model="claude", max_tokens=50, messages=[{"role": "user", "content": "first?"}]
) as stream:
    print("".join(stream.text_stream))
chunks = openai.OpenAI().chat.completions.create(
    model="gpt",
    messages=[{"role": "user", "content": "then?"}],
    stream=True,
    stream_options={"include_usage": True},
)
print("".join(c.choices[0].delta.content or "" for c in chunks if c.choices))
"""


def sse(events: list[tuple[str | None, dict]]) -> bytes:
    lines = []
    for name, data in events:
        lines.append(
            (f"event: {name}\n" if name else "") + f"data: {json.dumps(data)}\n\n"
        )
    return ("".join(lines) + "data: [DONE]\n\n").encode()


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
        (
            None,
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt",
                "choices": [],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            },
        ),
    ]
)
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude",
    "content": [],
    "stop_reason": None,
    "stop_sequence": None,
    "usage": {"input_tokens": 6, "output_tokens": 1},
}
ANTHROPIC_STREAM = sse(
    [
        ("message_start", {"type": "message_start", "message": MESSAGE}),
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


class Streaming(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = ANTHROPIC_STREAM if self.path.endswith("/messages") else OPENAI_STREAM
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # In pieces, as a model streams.
        for start in range(0, len(body), 64):
            self.wfile.write(body[start : start + 64])
            self.wfile.flush()

    def log_message(self, format, *args) -> None:
        pass


@contextmanager
def streaming() -> Iterator[Streaming]:
    server = Streaming()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_streamed_replies_from_both_sdks_read_back_as_steps(tmp_path):
    trace = tmp_path / "trace"
    env = {k: v for k, v in os.environ.items() if "_BASE_URL" not in k}
    env.update(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="o")
    with streaming() as server:
        env.update(OPENAI_BASE_URL=server.url + "/v1", ANTHROPIC_BASE_URL=server.url)
        result = subprocess.run(
            [FAULTLINE, "run", "--expect", "cart", "--trace", str(trace)]
            + ["--", sys.executable, "-c", AGENT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "looking at logs\nthe cart is down\n"
    [run] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert [(s.text, s.finish_reason) for s in run.steps] == [
        ("looking at logs", "stop"),
        ("the cart is down", "stop"),
    ]
    assert [(s.input_tokens, s.output_tokens) for s in run.steps] == [(6, 3), (8, 4)]
    assert run.answer == "the cart is down"
    assert result.stderr.splitlines()[-1] == 'verdict: passed (answer has "cart")'
