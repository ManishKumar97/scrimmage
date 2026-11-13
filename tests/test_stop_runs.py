"""Stopping the runs: all at once, and with the runner when it is killed."""

import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import Project
from scrimmage_core import config, runner
from scrimmage_core.runner import Runner

GRACE_S = 1.0
BOUND_S = 20  # a ceiling for an event that should come at once; it only ends a failing test
LOADED_BOUND_S = 120  # the same, for a wait on another process while the machine is busy

IGNORES_TERM = (
    "import signal, sys\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


def gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] == "Z"  # a zombie is ended
    except (FileNotFoundError, ProcessLookupError):  # reaped before the open, or the read
        return True


@pytest.mark.slow
def test_cancel_stops_every_run_in_one_grace_period_not_one_each(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "KILL_GRACE_S", GRACE_S)
    run = Runner(config.load(project.toml))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", IGNORES_TERM],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        for _ in range(4)
    ]
    try:
        for n, proc in enumerate(procs):
            assert proc.stdout is not None and proc.stdout.readline() == b"ready\n"  # TERM ignored
            run.procs[f"run-{n}"] = proc
        started = time.monotonic()
        run.cancel()
        took = time.monotonic() - started
        assert all(proc.returncode == -signal.SIGKILL for proc in procs)
        assert all(gone(proc.pid) for proc in procs)
        assert GRACE_S <= took < 2 * GRACE_S, f"{took:.2f}s: one grace period, not four"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.stdout is not None:
                proc.stdout.close()
        run.server.server.server_close()


def state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return "gone"


def running_program(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as cmdline:
            return cmdline.read().startswith(b"sh\0")
    except FileNotFoundError:
        return False


MINI_RUNNER = """
SH = "trap '' TERM; while :; do sleep 1; done"
import os, subprocess, sys, time
env = {**os.environ, "SCRIMMAGE_RUNNER_PID": str(os.getpid())}
proc = subprocess.Popen(
    [sys.executable, "-m", "scrimmage_core.run_command", "sh", "-c", SH],
    env=env, start_new_session=True,
)
print(proc.pid, flush=True)
time.sleep(600)
"""


@pytest.mark.slow
def test_a_run_ends_when_its_runner_is_killed() -> None:
    mini = subprocess.Popen([sys.executable, "-c", MINI_RUNNER], stdout=subprocess.PIPE)
    pid = 0
    try:
        assert mini.stdout is not None
        pid = int(mini.stdout.readline())  # the run is started
        ready = time.monotonic() + LOADED_BOUND_S
        while not running_program(
            pid
        ):  # the event: run_command has become the program, which ignores SIGTERM
            assert time.monotonic() < ready and not gone(pid), (
                f"the run never started (state {state(pid)})"
            )
            time.sleep(0.01)
        mini.kill()  # SIGKILL: nothing in the runner gets to stop its runs
        mini.wait()
        deadline = time.monotonic() + LOADED_BOUND_S  # SIGKILL is sent at once; the exit is not
        while not gone(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gone(pid), f"the run outlived its runner (state {state(pid)})"
    finally:
        if pid and not gone(pid):
            os.kill(pid, signal.SIGKILL)
        if mini.poll() is None:
            mini.kill()
            mini.wait()
        if mini.stdout is not None:
            mini.stdout.close()
