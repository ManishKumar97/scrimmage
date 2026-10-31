"""Each provider's calls, and its API key, reach only that provider's upstream.

The agent uses both real SDKs, as an agent with a main model and a cheaper one would.
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
from typing import cast

FAULTLINE = str(Path(sys.executable).parent / "faultline")
ANTHROPIC_KEY = "sk-ant-only-for-anthropic"
OPENAI_KEY = "sk-only-for-openai"

AGENT = """
import anthropic, openai
said = anthropic.Anthropic().messages.create(
    model="claude", max_tokens=10, messages=[{"role": "user", "content": "hi"}]
)
print(said.content[0].text)
done = openai.OpenAI().chat.completions.create(
    model="gpt", messages=[{"role": "user", "content": "hi"}]
)
print(done.choices[0].message.content)
"""

ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude",
    "content": [{"type": "text", "text": "from anthropic"}],
    "stop_reason": "end_turn",
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
            "message": {"role": "assistant", "content": "from openai"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


class Recording(ThreadingHTTPServer):
    def __init__(self, reply: dict) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.reply = json.dumps(reply).encode()
        self.seen: list[tuple[str, str]] = []  # (path, every header and the body)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(Recording, self.server)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        server.seen.append((self.path, str(self.headers) + body.decode()))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(server.reply)))
        self.end_headers()
        self.wfile.write(server.reply)

    def log_message(self, format, *args) -> None:
        pass


@contextmanager
def recording(reply: dict) -> Iterator[Recording]:
    server = Recording(reply)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_an_anthropic_key_never_reaches_the_openai_upstream(tmp_path):
    env = {k: v for k, v in os.environ.items() if "_BASE_URL" not in k}
    env.update(ANTHROPIC_API_KEY=ANTHROPIC_KEY, OPENAI_API_KEY=OPENAI_KEY)
    with (
        recording(OPENAI_REPLY) as openai_api,
        recording(ANTHROPIC_REPLY) as claude_api,
    ):
        env.update(
            OPENAI_BASE_URL=openai_api.url + "/v1", ANTHROPIC_BASE_URL=claude_api.url
        )
        result = subprocess.run(
            [FAULTLINE, "run", "--trace", str(tmp_path / "trace")]
            + ["--", sys.executable, "-c", AGENT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "from anthropic\nfrom openai\n"
    assert [path for path, _ in claude_api.seen] == ["/v1/messages"]
    assert [path for path, _ in openai_api.seen] == ["/v1/chat/completions"]
    assert ANTHROPIC_KEY in claude_api.seen[0][1]
    assert all(ANTHROPIC_KEY not in seen for _, seen in openai_api.seen)
    assert all(OPENAI_KEY not in seen for _, seen in claude_api.seen)
