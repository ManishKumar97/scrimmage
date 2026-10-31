"""How a child started with a cleaned environment finds the run it belongs to."""

import json
import os
import stat
import subprocess
import sys

from faultline.proxy.context import (
    RunContext,
    context_path,
    from_env,
    publish,
    runtime_dir,
    withdraw,
)

RUN = RunContext("/tmp/trace", "ab" * 16, "cd" * 8, ("tool:get_logs:empty",), 1000)

# A grandchild of the test, with no FAULTLINE_* variables, as an MCP server gets.
FINDS = (
    "import subprocess, sys; subprocess.run([sys.executable, '-c', "
    "'from faultline.proxy.context import find; print(find({}))'])"
)


def in_bare_child() -> str:
    bare = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    return subprocess.run(
        [sys.executable, "-c", FINDS],
        env=bare,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()


def test_the_variables_round_trip():
    assert from_env(RUN.env()) == RUN
    assert from_env({}) is None


def test_a_child_with_no_variables_finds_the_run_through_its_parents():
    path = publish(RUN)
    try:
        found = in_bare_child()
    finally:
        withdraw(path)

    assert found == repr(RUN)
    assert not path.exists()


def test_the_context_file_is_private():
    path = publish(RUN)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(runtime_dir().stat().st_mode) == 0o700
    finally:
        withdraw(path)


def test_a_stale_file_from_a_reused_process_id_is_ignored():
    path = publish(RUN)
    try:
        record = json.loads(path.read_text())
        record["started"] = "1"  # a process that started at another time
        path.write_text(json.dumps(record))
        found = in_bare_child()
    finally:
        withdraw(path)

    assert found == "None"
    assert context_path(os.getpid()) == path
