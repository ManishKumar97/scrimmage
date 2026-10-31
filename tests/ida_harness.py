"""Run the lab's IDA as a customer would: a separate process, configured by environment.

The model is the lab's scripted fake_llm, so no run calls a real model API.
The tools are real, so these runs need the kind test bed.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

QUESTION = "Customers report the cart fails. Why?"
# One tool call, then an answer. get_logs is a tool from the logs MCP server.
SCRIPT = [
    {"tool_calls": [{"name": "get_logs", "arguments": {"service": "cart"}}]},
    {"text": "The cart service logs show the failure.\nROOT CAUSE SERVICE: cart"},
]
# Settings the IDA reads that a test must not inherit from the shell.
OWN_SETTINGS = (
    "PROVIDER",
    "GROQ_",
    "ANTHROPIC_",
    "OPENAI_",
    "MCP_",
    "MODEL_",
    "TRACE_",
)


def cluster_up() -> bool:
    if not shutil.which("kubectl"):
        return False
    try:
        probe = subprocess.run(
            ["kubectl", "get", "nodes"], capture_output=True, timeout=15, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return probe.returncode == 0


needs_cluster = pytest.mark.skipif(
    not cluster_up(),
    reason="the kind test bed is not reachable (kubectl get nodes failed); "
    "the IDA's tools are real and need it",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def fake_llm(tmp_path: Path, script: list[dict] = SCRIPT) -> Iterator[str]:
    """Serve the script and yield its base URL, the form GROQ_BASE_URL takes."""
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script))
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "faultline_lab.fake_llm", "--script", str(path)]
        + ["--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline or server.poll() is not None:
                    raise RuntimeError("fake_llm did not start") from None
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.terminate()
        server.wait(10)


def ida_env(tmp_path: Path, **settings: str) -> dict[str, str]:
    """The environment of an IDA run on the fake model, with its spans in tmp_path."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(OWN_SETTINGS)}
    env.update(
        PROVIDER="groq",
        GROQ_API_KEY="fake",
        GROQ_MODEL="fake-model",
        TRACE_FILE=str(tmp_path / "ida.jsonl"),
    )
    env.update(settings)
    return env


def run(
    command: list[str], env: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    # The working directory is tmp_path, so no .env file is read.
    return subprocess.run(
        command,
        check=False,  # the caller reads the exit code
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
    )


def ida(question: str = QUESTION) -> list[str]:
    return [sys.executable, "-m", "faultline_lab.ida", question]
