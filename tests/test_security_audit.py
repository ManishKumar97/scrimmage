"""A real scrimmage test, watched by Python's audit hooks in every process: the runner, the
program each run starts, and the agent.

Each process loads a sitecustomize from a folder on PYTHONPATH. In the agent's process
Scrimmage's own startup file runs first and then chains to it, so the hook sees everything
after Scrimmage switched on, though not the plan fetch at its start; every later call to the
control server goes through the same client. The hook logs every connection, every
process start and every byte sent with http.client, which is the SDK's only client.
"""

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import ACME, Project
from scrimmage_core import protocol
from fakes.openai_server import FakeOpenAI

HOOK = textwrap.dedent(
    """
    import json, os, sys, sysconfig

    _LOG = os.environ["AUDIT_LOG"]
    _HERE = os.path.realpath(__file__)
    _STDLIB = tuple(
        os.path.realpath(sysconfig.get_paths()[k]) + os.sep for k in ("stdlib", "platstdlib")
    )

    def _origin():
        # The innermost caller outside the standard library: who asked for this.
        frame = sys._getframe(2)
        while frame is not None:
            code = frame.f_code.co_filename
            name = os.path.realpath(code)
            # A frozen module, such as os, is part of the standard library too.
            if not code.startswith("<") and name != _HERE and not name.startswith(_STDLIB):
                return name
            frame = frame.f_back
        return ""

    def _write(record):
        record["pid"] = os.getpid()
        with open(_LOG, "a") as out:
            out.write(json.dumps(record) + "\\n")

    def _text(data):
        if isinstance(data, (bytes, bytearray, memoryview)):
            return bytes(data).decode("latin-1")
        return str(data)

    def _hook(event, args):
        if event == "socket.connect":
            _write({"event": event, "address": [str(a) for a in args[1]]
                    if isinstance(args[1], tuple) else str(args[1])})
        elif event in ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.spawn"):
            _write({"event": event, "program": str(args[0]),
                    "args": [str(a) for a in (args[1] or [])], "origin": _origin()})
        elif event in ("os.system", "pty.spawn"):
            _write({"event": event, "program": str(args[0]), "args": [], "origin": _origin()})
        elif event == "http.client.send":
            _write({"event": event, "data": _text(args[1])})

    sys.addaudithook(_hook)
    _write({"event": "start", "argv": list(sys.orig_argv),
            "token": os.environ.get("SCRIMMAGE_TOKEN"),
            "control": os.environ.get("SCRIMMAGE_CONTROL_URL")})
    """
)
# The runner, started with the hook: it logs the token its Runner made, then runs the test.
RUNNER = textwrap.dedent(
    """
    import json, os, sys
    from scrimmage_core import runner
    from scrimmage_core.cli import main

    made = runner.Runner.__init__

    def init(self, *args, **kwargs):
        made(self, *args, **kwargs)
        with open(os.environ["AUDIT_LOG"], "a") as out:
            out.write(json.dumps({"event": "runner", "token": self.token}) + "\\n")

    runner.Runner.__init__ = init
    sys.exit(main(["test"]))
    """
)


def watched_run(project: Project, fake: FakeOpenAI, tmp_path: Path) -> list[dict[str, Any]]:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(HOOK)
    log = tmp_path / "audit.jsonl"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(hooks), *filter(None, [os.environ.get("PYTHONPATH")])]),
        "AUDIT_LOG": str(log),
        "OPENAI_BASE_URL": fake.base_url,
        "OPENAI_API_KEY": "test",
        "ACME_DATABASE_PASSWORD": "env-only-value-7f3a",
    }
    done = subprocess.run(
        [sys.executable, "-c", RUNNER],
        cwd=project.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.design("security.audited_run")
def test_a_watched_run_talks_only_to_loopback_starts_only_its_program_and_never_sends_the_token(
    project: Project, fake_openai: Callable[..., FakeOpenAI], tmp_path: Path
) -> None:
    fake = fake_openai(ACME)
    events = watched_run(project, fake, tmp_path)

    # Positive control: the token the Runner made is the one every agent process holds, and
    # the hook saw signed requests to the control server, so it was watching the right bytes.
    (token,) = {e["token"] for e in events if e["event"] == "runner"}
    agents = [e for e in events if e["event"] == "start" and e["token"]]
    assert len(token) == 64 and agents and {e["token"] for e in agents} == {token}
    sent = [e["data"] for e in events if e["event"] == "http.client.send"]
    assert sum(protocol.SIGNATURE in data for data in sent) >= len(agents)

    # The token itself never leaves a process, in a header or a body.
    assert not [data for data in sent if token in data]

    # Every connection goes to 127.0.0.1: the control server or the fake model server.
    control_ports = {e["control"].rsplit(":", 1)[1] for e in agents}
    model_port = fake.base_url.split(":")[2].split("/")[0]
    connects = [e["address"] for e in events if e["event"] == "socket.connect"]
    assert connects
    assert all(isinstance(a, list) and a[0] == "127.0.0.1" for a in connects), connects
    assert {a[1] for a in connects} <= control_ports | {model_port}

    # The only processes Scrimmage starts are the runs: the runner starts scrimmage-run, which
    # becomes the agent's own program. What the agent's libraries start, such as the
    # `uname -p` that Python's platform module runs for the openai library, is theirs.
    scrimmage = f"{os.sep}scrimmage_core{os.sep}"
    starts = [e for e in events if "origin" in e and scrimmage in e["origin"]]
    assert {e["event"] for e in starts} == {"subprocess.Popen", "os.exec"}
    for start in starts:
        args = start["args"]
        assert start["program"] == sys.executable, start
        assert args[1:3] in (["-m", "scrimmage_core.run_command"], ["-m", "scrimmage_core.start"]), start

    # Nothing written for the run holds the token or the agent's environment.
    files = [f for f in (project.root / "scrimmage-runs").rglob("*") if f.is_file()]
    assert any(f.suffix == ".jsonl" for f in files) and any(f.suffix == ".log" for f in files)
    for path in files:
        data = path.read_bytes()
        assert token.encode() not in data, path
        assert b"env-only-value-7f3a" not in data, path
        assert b"ACME_DATABASE_PASSWORD" not in data, path
