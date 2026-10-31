"""A two-step agent on the standard library alone: one tool call over MCP, then an answer.

    python tiny_agent.py SERVER_LOG [bare]

It reads its model base URL from OPENAI_BASE_URL and starts its MCP server, the
stand-in stdio_server.py, under `faultline mcp-wrap`, as a team's MCP config would.
With `bare`, the server gets only PATH and HOME, as many MCP clients give it.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
FAULTLINE = str(Path(sys.executable).parent / "faultline")


def chat(messages: list[dict]) -> dict:
    body = json.dumps({"model": "tiny-model", "messages": messages}).encode()
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"] + "/chat/completions",
        body,
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as reply:
        return json.load(reply)["choices"][0]["message"]


def call_tool(name: str, arguments: dict, log: str) -> str:
    server = [sys.executable, str(HERE / "stdio_server.py"), log, "0"]
    bare = sys.argv[2:] == ["bare"]
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    with subprocess.Popen(
        [FAULTLINE, "mcp-wrap", "--", *server],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env if bare else None,
    ) as process:
        assert process.stdin and process.stdout
        params = {"name": name, "arguments": arguments}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        process.stdin.write(json.dumps(request).encode() + b"\n")
        process.stdin.flush()
        result = json.loads(process.stdout.readline())["result"]
        process.stdin.close()
    return "\n".join(block["text"] for block in result["content"])


def main() -> None:
    messages: list[dict] = [{"role": "user", "content": "Why does the cart fail?"}]
    asked = chat(messages)
    call = asked["tool_calls"][0]
    output = call_tool(
        call["function"]["name"], json.loads(call["function"]["arguments"]), sys.argv[1]
    )
    messages += [asked, {"role": "tool", "tool_call_id": call["id"], "content": output}]
    print(chat(messages)["content"])


if __name__ == "__main__":
    main()
