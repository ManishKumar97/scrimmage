"""A copy of the service Scrimmage started with [agent] start always stops with its test: on
Ctrl-C or SIGTERM, when it dies mid-test, with two workers, and when the runner itself is
killed with SIGKILL. A probe that something other than the copy answered names the copy's
exit, and waits no longer than the copy takes to exit.

Each test runs the real CLI against a real uvicorn copy of the example app. /chat-held tells
the test which worker took a request and holds that worker until the test lets it go, so a
test knows a run is in flight, and which process serves it.
"""

import contextlib
import http.server
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scrimmage import managed, runner, service_client
from test_managed_start import (
    UVICORN,
    NotFound,
    Shop,
    group,
    no_proc,
    shop,
    time_limit,
    wait_until,
)

pytestmark = pytest.mark.slow
__all__ = ["shop", "time_limit"]  # the fixtures, used by name; time_limit keeps 120 s a test
# An app's load, longer than the 3 s that a copy without /proc was once given to exit: CI's
# 3.12 runner, measuring coverage in every process, took longer than that to load uvicorn.
SLOW_LOAD_S = 4.0
AT_ONCE_S = 3.0  # how soon a copy that answers without the gate is named, with /proc
# An app that, as it loads, starts a helper from a thread that then ends. The helper adds a
# byte to BEAT_FILE every 0.1 s, and writes its pid beside it.
THREADED = """import os
import subprocess
import sys
import threading
import time


def started():
    subprocess.Popen([sys.executable, "beat.py", os.environ["BEAT_FILE"]])
    time.sleep(0.5)  # the helper is up before this thread ends


helper = threading.Thread(target=started)
helper.start()
helper.join()
from acme.service import app  # noqa: E402
"""
BEAT = """import os
import sys
import time

with open(sys.argv[1] + ".pid", "w") as out:
    out.write(str(os.getpid()))
while True:
    with open(sys.argv[1], "a") as out:
        out.write("x")
    time.sleep(0.1)
"""
SECOND_TEST = """
[[test]]
name     = "order-status-again"
question = "Where is order 1234 now?"
expect   = ["shipped"]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"
returns = {sku = "SKU-42", count = 12}
"""


class Holder:
    """The test's end of /chat-held: each request names its worker's pid and waits for a go."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A short folder of its own: a socket's path is at most 107 bytes.
        self.folder = Path(tempfile.mkdtemp(prefix="scrimmage-hold-"))
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.folder / "hold.sock"))
        self.sock.listen(8)
        self.sock.settimeout(60)
        self.held: list[socket.socket] = []
        monkeypatch.setenv("HOLD_SOCKET", str(self.folder / "hold.sock"))

    def take(self) -> int:
        """The next request: it is held, and its worker's pid is returned."""
        conn, _ = self.sock.accept()
        self.held.append(conn)
        return int(conn.makefile().readline())

    def go(self) -> None:
        for conn in self.held:
            with contextlib.suppress(OSError):
                conn.sendall(b"g")

    def close(self) -> None:
        for conn in self.held:
            conn.close()
        self.sock.close()
        shutil.rmtree(self.folder, ignore_errors=True)


@pytest.fixture
def holder(monkeypatch: pytest.MonkeyPatch) -> Iterator[Holder]:
    made = Holder(monkeypatch)
    try:
        yield made
    finally:
        made.close()


def held_file(shop: Shop, start: str, *, extra: str = "", second: bool = False) -> Path:
    """The test file, with each run sent to /chat-held; second: a second test."""
    path = shop.write(start, extra=extra)
    text = path.read_text().replace('/chat"', '/chat-held"')
    path.write_text(text + (SECOND_TEST if second else ""))
    return path


def gone(pid: int) -> bool:
    """The process has exited: no entry, a zombie (Z), or one being reaped now (X)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return True
    return fields[0] in ("Z", "X")


def test_ctrl_c_during_the_ready_wait_stops_the_child(shop: Shop) -> None:
    path = shop.write(f"{UVICORN} sleepy:app --port {{port}} --workers 2")
    spawned = shop.spawn("test", str(path))
    wait_until(spawned.found_copy)
    wait_until(lambda: len(group(spawned.copy)) >= 3)  # the supervisor and both workers
    wait_until(lambda: "scrimmage: waiting for" in spawned.text())  # the probe's wait
    spawned.proc.send_signal(signal.SIGINT)
    code = spawned.proc.wait(30)
    out = spawned.text()
    assert code == 3, out
    assert "scrimmage: stopped by SIGINT" in out, out
    assert "Traceback" not in out, out
    assert group(spawned.copy) == [], "a process of the copy was left running"
    assert spawned.token_folder is not None and not spawned.token_folder.exists()


def test_sigterm_mid_test_stops_the_child_and_removes_the_token_file(
    shop: Shop, holder: Holder
) -> None:
    start = f"{UVICORN} acme.service:app --port {{port}} --loop asyncio --workers 2"
    spawned = shop.spawn("test", str(held_file(shop, start)))
    holder.take()  # the setup request
    holder.go()
    serving = holder.take()  # the first run, held in its worker: the test is mid-run
    wait_until(spawned.found_copy)
    assert serving in group(spawned.copy)
    spawned.proc.send_signal(signal.SIGTERM)
    code = spawned.proc.wait(30)
    out = spawned.text()
    assert code == 3, out
    assert "scrimmage: stopped by SIGTERM" in out, out
    assert "Traceback" not in out, out
    # The held worker cannot run uvicorn's handler, so only the SIGKILL after the grace ends it.
    assert group(spawned.copy) == [], "a process of the copy was left running"
    assert spawned.token_folder is not None and not spawned.token_folder.exists()


def test_a_child_that_dies_mid_test_stops_the_test_with_exit_3_naming_the_log(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy's whole group is killed just before the second run is sent: that run fails,
    the test sees the copy has exited, and the two runs left are never sent."""
    sent: list[str] = []
    served = runner.Runner.served

    def killing(self: runner.Runner, run: str, row: Any) -> Any:
        sent.append(run)
        if len(sent) == 2:
            copy = shop.copies[0]
            assert copy.proc is not None
            os.killpg(copy.proc.pid, signal.SIGKILL)
            wait_until(lambda: group(copy.proc.pid) == [] if copy.proc else True)
        return served(self, run, row)

    monkeypatch.setattr(runner.Runner, "served", killing)
    code, out = shop.test(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio", runs=4)
    assert code == 3, out
    said = "the service started with [agent] start exited with signal SIGKILL during the test"
    assert f"{said}, so no further run was sent" in out, out
    assert f"its whole output is in {shop.log()}" in out, out
    assert len(sent) == 2, sent
    copy = shop.copies[0]
    assert copy.token_folder is not None and not copy.token_folder.exists()


def test_workers_2_both_serve_runs_and_both_stop(
    shop: Shop, holder: Holder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tests' runs at once. The second is sent once the first is held in the worker it
    reached, which then accepts nothing, so the second reaches the other worker: a worker that
    wakes for one connection can accept two. Both workers are in the copy's process group
    while it runs, so its stop reaches them, and neither is left after."""
    start = f"{UVICORN} acme.service:app --port {{port}} --loop asyncio --workers 2"
    path = held_file(shop, start, extra="parallel = 2", second=True)
    took: list[int] = []
    failed: list[BaseException] = []
    first_held = threading.Event()
    sent: list[str] = []
    lock = threading.Lock()
    served = runner.Runner.served

    def in_turn(self: runner.Runner, run: str, row: Any) -> Any:
        with lock:
            sent.append(run)
            second = len(sent) == 2
        if second:
            first_held.wait(60)
        return served(self, run, row)

    def serve() -> None:
        try:
            holder.take()  # the setup request
            holder.go()
            took.append(holder.take())
            first_held.set()
            took.append(holder.take())
            holder.go()
        except BaseException as exc:  # said by the test, not lost in this thread
            failed.append(exc)
            first_held.set()
            holder.go()

    monkeypatch.setattr(runner.Runner, "served", in_turn)
    in_group: list[list[int]] = []
    shop.at_stop.append(lambda copy: in_group.append(group(copy.proc.pid)) if copy.proc else None)
    server = threading.Thread(target=serve, daemon=True)
    server.start()
    code, out = shop.run("test", str(path), "--json")
    server.join(10)
    assert not failed, failed
    assert code == 0, out
    assert len(set(took)) == 2, took
    claimed = {
        line["summary"]["claimed_by"]["pid"]
        for record in (shop.root / "scrimmage-runs").glob("*/run-*.jsonl")
        for line in map(json.loads, record.read_text().splitlines())
        if "summary" in line
    }
    assert claimed == set(took), (claimed, took)
    assert set(took) <= set(in_group[0]), "a worker is outside the copy's process group"
    copy = shop.copies[0]
    assert copy.proc is not None and group(copy.proc.pid) == []
    assert all(gone(pid) for pid in took), "a worker was left running"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux only: macOS has no parent-death signal"
)
def test_a_runner_killed_with_sigkill_takes_the_supervisor_and_its_workers(
    shop: Shop, holder: Holder
) -> None:
    start = f"{UVICORN} acme.service:app --port {{port}} --loop asyncio --workers 2"
    spawned = shop.spawn("test", str(held_file(shop, start)))
    holder.take()  # the setup request
    holder.go()
    holder.take()  # the first run, held: the test is mid-run
    wait_until(spawned.found_copy)
    # One worker holds the run; the other may still be loading the app on a loaded machine.
    wait_until(lambda: shop.log().read_text().count("active in service mode") == 2, within=60)
    workers = [pid for pid in group(spawned.copy) if pid != spawned.copy]
    spawned.proc.kill()
    spawned.proc.wait()
    wait_until(lambda: group(spawned.copy) == [], within=2.0)
    assert all(gone(pid) for pid in workers), workers


@pytest.mark.parametrize("proc", [True, False], ids=["proc", "no-proc"])
def test_a_port_taken_while_the_app_loads_slowly_names_the_copys_exit(
    shop: Shop, proc: bool
) -> None:
    """The app takes SLOW_LOAD_S to load, as an app's imports can on a loaded machine, while
    another process holds its port at every start. With /proc, the copy holds no socket on
    its port meanwhile; without it, nothing says who answered. Either way the wait goes on
    until the copy exits, and the test names its exit, after its starts."""
    if not proc:
        no_proc(shop)
    (shop.root / "slowapp.py").write_text(
        f"import time\n\ntime.sleep({SLOW_LOAD_S:g})\nfrom acme.service import app\n"
    )
    holder = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
    threading.Thread(target=holder.serve_forever, daemon=True).start()
    try:
        port = int(holder.server_address[1])
        shop.monkeypatch.setattr(runner, "free_port", lambda: port)
        code, out = shop.test(f"{UVICORN} slowapp:app --port {{port}} --loop asyncio")
    finally:
        holder.shutdown()
        holder.server_close()
    assert code == 3, out
    assert "the service started with [agent] start exited with code 3 before it was ready" in out
    assert "address already in use" in out, out
    assert f"after {managed.START_TRIES} starts on ports" in out, out


def test_a_copy_that_answers_without_the_gate_is_named_at_once(shop: Shop) -> None:
    """The copy itself listens on its port but has no gate: no wait for it to exit.

    Timed from the probe's reply to the runner's exit 3, and the check for a lost port on
    its own. Starting the copy takes longer on a loaded machine, and no deadline covers it."""
    probe, lost_its_port = service_client.probe, managed.Managed.lost_its_port
    probe_service = runner.Runner.probe_service
    replied: list[float] = []
    checks: list[tuple[bool, float]] = []
    ended: list[float] = []

    def probed(*args: Any) -> service_client.Probed:
        found = probe(*args)
        replied.append(time.monotonic())
        return found

    def timed(copy: managed.Managed, until: float) -> bool:
        began = time.monotonic()
        lost = lost_its_port(copy, until)
        checks.append((lost, time.monotonic() - began))
        return lost

    def raised(self: runner.Runner) -> service_client.Probed:
        try:
            return probe_service(self)
        finally:
            ended.append(time.monotonic())

    shop.monkeypatch.setattr(service_client, "probe", probed)
    shop.monkeypatch.setattr(managed.Managed, "lost_its_port", timed)
    shop.monkeypatch.setattr(runner.Runner, "probe_service", raised)
    code, out = shop.test(f"{sys.executable} -m http.server {{port}} --bind 127.0.0.1")
    assert code == 3, out
    assert "x-scrimmage-run does not reach Scrimmage" in out, out
    [(lost, took)] = checks
    assert not lost, "the copy that answered was taken for one that lost its port"
    assert took < AT_ONCE_S, took
    [end] = ended
    named = end - replied[-1]
    assert named < AT_ONCE_S, named


def test_a_port_held_by_a_process_that_never_answers_ends_the_probe_when_the_copy_exits(
    shop: Shop,
) -> None:
    """The other process takes the probe's connection and never replies: the probe ends as
    soon as the copy exits, not at its own 30 s."""
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(8)
    try:
        port = int(hole.getsockname()[1])
        shop.monkeypatch.setattr(runner, "free_port", lambda: port)
        began = time.monotonic()
        code, out = shop.test(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
        took = time.monotonic() - began
    finally:
        hole.close()
    assert code == 3, out
    assert "the service started with [agent] start exited with code 3 before it was ready" in out
    assert "address already in use" in out, out
    assert took < service_client.PROBE_S / 2, took


def test_a_helper_started_from_a_thread_that_ends_runs_until_the_copy_stops(
    shop: Shop, tmp_path: Path
) -> None:
    """Linux sends the parent-death signal when the thread that started a process ends. A
    helper the app starts from a short-lived thread must not get it: it keeps running after
    that thread has ended, and the copy's stop at the end of the test ends it."""
    (shop.root / "threaded.py").write_text(THREADED)
    (shop.root / "beat.py").write_text(BEAT)
    beats = tmp_path / "beats"
    shop.monkeypatch.setenv("BEAT_FILE", str(beats))
    pid_file = Path(f"{beats}.pid")
    seen: list[tuple[int, int, bool]] = []

    def beating(copy: Any) -> None:
        wait_until(pid_file.exists)
        # Well after the thread that started it has ended.
        wait_until(lambda: time.time() > pid_file.stat().st_mtime + 2.0, within=10)
        before = beats.stat().st_size
        time.sleep(0.5)
        seen.append((before, beats.stat().st_size, gone(int(pid_file.read_text()))))

    shop.at_stop.append(beating)
    code, out = shop.test(f"{UVICORN} threaded:app --port {{port}} --loop asyncio")
    assert code == 0, out
    before, after, ended = seen[0]
    assert not ended and after > before, (before, after, "the helper was killed early")
    helper = int(pid_file.read_text())
    try:
        assert gone(helper), "the copy's stop did not end the helper"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(helper, signal.SIGKILL)


def test_ctrl_c_during_the_setup_request_says_it_was_stopped(shop: Shop) -> None:
    """The model never answers, so the setup request waits on it: a Ctrl-C then stops the
    test as stopped, not as a setup request Scrimmage did not serve."""
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(8)
    hole.settimeout(60)
    shop.monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{hole.getsockname()[1]}/v1")
    try:
        path = shop.write(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
        spawned = shop.spawn("test", str(path))
        call, _ = hole.accept()  # the setup request's model call, which gets no answer
        try:
            wait_until(spawned.found_copy)
            spawned.proc.send_signal(signal.SIGINT)
            code = spawned.proc.wait(30)
        finally:
            call.close()
    finally:
        hole.close()
    out = spawned.text()
    assert code == 3, out
    assert "scrimmage: stopped by SIGINT" in out, out
    assert "not served by Scrimmage" not in out, out
    assert group(spawned.copy) == [], "a process of the copy was left running"
    assert spawned.token_folder is not None and not spawned.token_folder.exists()
