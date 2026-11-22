"""A service Scrimmage starts with [agent] start: the copy is started on a free port with
the token in a private file and the settings in its environment, it is ready once a gated
process answers the probe, and it is stopped, every process of its group, when the test ends.

Each test runs the real CLI against a real uvicorn copy of the example app, with no
SCRIMMAGE_ variable and no token file of the test's own.
"""

import contextlib
import http.server
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from conftest import ACME, EXAMPLE
from scrimmage import config, managed, runner
from scrimmage.cli import main
from scrimmage.managed import Managed

pytestmark = pytest.mark.slow
TOOLS = ["acme.tools:get_order", "acme.tools:check_inventory", "acme.tools:refund_order"]
TOOLS += ["acme.tools:get_stock"]
UVICORN = f"{sys.executable} -m uvicorn"
FILE = """[agent]
start   = 'START'
service = "http://127.0.0.1:{port}/chat"
body    = {message = "{question}", session = "{session}"}
answer  = "reply"
tools   = TOOL_LIST
real    = ["tool:check_inventory", "process:uname"]
model_hosts = [{host = "127.0.0.1", provider = "openai"}]
runs = RUNS
record_grace_s = 0.2
record_wait_s = 10
EXTRA
[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped"]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"
returns = {sku = "SKU-42", count = 12}
"""
# Each fails or misbehaves in one way, beside the example's app.
BROKEN = 'raise RuntimeError("the shop database is down")\n'
SLEEPY = "import time\n\ntime.sleep(600)\n"
STUBBORN = '''import signal

from acme.service import app


async def stubborn(scope, receive, send):
    """Each worker ignores SIGTERM once uvicorn has set its own handlers."""
    if scope["type"] == "lifespan":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    await app(scope, receive, send)
'''
PRINTS = """import os
import sys

print("the shop key is", os.environ["SHOP_KEY"], file=sys.stderr, flush=True)
from acme.service import app  # noqa: E402
"""
MARKING = """import os
import signal
from pathlib import Path

from acme.service import app


def noted(signum, frame):
    Path(os.environ["STOP_MARK"]).touch()  # the stop has begun; the worker stays up


async def marking(scope, receive, send):
    if scope["type"] == "lifespan":  # after uvicorn has set its own handlers
        signal.signal(signal.SIGTERM, noted)
    await app(scope, receive, send)
"""
# Prints a line that names its port and says "in use", then fails, with no bind at all.
CHATTY = """import sys

port, which = sys.argv[1], sys.argv[2]
said = {
    "unused": f"DEBUG: port {port} not in use by a previous run, continuing",
    "session": f"INFO: config listen_port={port}; session store in use: redis",
}
print(said[which], flush=True)
raise SystemExit(1)
"""
# Prints the line uvicorn prints when its bind fails, in one of its wordings, then exits 3.
BINDS = """import sys

port, which = sys.argv[1], sys.argv[2]
said = {
    "ipv6": f"ERROR:    [Errno 98] error while attempting to bind on address ('::1', {port}, 0, 0):"
    " address already in use",
    "ipv4": f"ERROR:    [Errno 98] error while attempting to bind on address ('127.0.0.1', {port}):"
    " address already in use",
    "time": "2026-11-22 18:37:33,068 ERROR [Errno 98] Address already in use",
}
print(said[which], flush=True)
raise SystemExit(3)
"""
# Binds its port, then serves no request: a client that sends "hold" crashes it, and that
# client keeps its end open, so the dead copy's end of that connection waits in FIN_WAIT2.
HOLDS = """import socket
import sys

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", int(sys.argv[1])))
server.listen()
print("bound", flush=True)
kept = []
while True:
    conn, _ = server.accept()
    kept.append(conn)
    conn.settimeout(1.0)
    try:
        if conn.recv(4) == b"hold":
            raise RuntimeError("the shop crashed while it held a client")
    except TimeoutError:
        continue
"""
# Forks a daemon that leaves the copy's session, binds the port and writes its pid to $1;
# the command itself exits 0.
DAEMON = """import os
import socket
import sys
import time

if os.fork() == 0:
    os.setsid()
    server = socket.socket()
    server.bind(("127.0.0.1", int(sys.argv[2])))
    server.listen()
    with open(sys.argv[1], "a") as out:
        out.write(f"{os.getpid()}\\n")
    time.sleep(120)
    os._exit(0)
"""
# Leaves a child in the copy's group, notes its pid in $1, then becomes uvicorn on $2.
LINGERS = """sleep 600 &
echo $! >> "$1"
exec PYTHON -m uvicorn acme.service:app --port "$2" --loop asyncio
"""
SHOP_KEY = "shop-key-5a1d9c0e7b"
SCRIMMAGE = Path(sys.executable).parent / "scrimmage"
TEST_LIMIT_S = 120  # a test that hangs fails here instead
GROUPS: list[int] = []  # every copy's process group a test started, for the time limit


def over_time(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    for pgid in GROUPS:  # only the copies' own groups, by their ids
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    raise TimeoutError(f"the test ran past {TEST_LIMIT_S} s")


@pytest.fixture(autouse=True)
def time_limit() -> Iterator[None]:
    previous = signal.signal(signal.SIGALRM, over_time)
    signal.alarm(TEST_LIMIT_S)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        GROUPS.clear()


def group(pgid: int) -> list[int]:
    """The processes of a process group still running; ended ones (Z, X) left out."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue  # it ended while it was read
        if fields[0] not in ("Z", "X") and int(fields[2]) == pgid:
            found.append(int(entry.name))
    return found


def children(pid: int) -> list[int]:
    found: list[int] = []
    for task in Path(f"/proc/{pid}/task").glob("*/children"):
        with contextlib.suppress(OSError):
            found += [int(x) for x in task.read_text().split()]
    return found


def wait_until(check: Callable[[], object], within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("it did not happen in time")
        time.sleep(0.02)


def environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    pairs = [item.decode().split("=", 1) for item in raw if b"=" in item]
    return {key: value for key, value in pairs}


class Shop:
    """The example app in a folder of its own, the fake model, and every copy started."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        from fakes.openai_server import FakeOpenAI

        self.root = tmp_path / "shop"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        for name, text in {
            "broken.py": BROKEN,
            "sleepy.py": SLEEPY,
            "stubborn.py": STUBBORN,
            "prints.py": PRINTS,
            "marking.py": MARKING,
            "chatty.py": CHATTY,
            "binds.py": BINDS,
            "holds.py": HOLDS,
            "daemon.py": DAEMON,
            "lingers.sh": LINGERS.replace("PYTHON", sys.executable),
        }.items():
            (self.root / name).write_text(text)
        self.fake = FakeOpenAI(ACME)
        self.fake.start()
        for name in list(os.environ):
            if name.startswith("SCRIMMAGE_"):
                monkeypatch.delenv(name)
        monkeypatch.setenv("OPENAI_BASE_URL", self.fake.base_url)
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        self.monkeypatch, self.capsys = monkeypatch, capsys
        self.copies: list[Managed] = []
        self.spawned: list[Spawned] = []
        self.at_stop: list[Callable[[Managed], None]] = []
        start, stop = Managed.start, Managed.stop

        def started(copy: Managed, control_url: str) -> None:
            self.copies.append(copy)
            start(copy, control_url)
            if copy.proc is not None:
                GROUPS.append(copy.proc.pid)

        def stopping(copy: Managed) -> None:
            try:
                for check in self.at_stop:
                    check(copy)
            finally:
                stop(copy)

        monkeypatch.setattr(Managed, "start", started)
        monkeypatch.setattr(Managed, "stop", stopping)

    def write(self, start: str, *, runs: int = 1, extra: str = "") -> Path:
        text = FILE.replace("START", start).replace("TOOL_LIST", json.dumps(TOOLS))
        path = self.root / "scrimmage.toml"
        path.write_text(text.replace("RUNS", str(runs)).replace("EXTRA", extra))
        return path

    def run(self, *args: str) -> tuple[int, str]:
        self.capsys.readouterr()
        code = main(list(args))
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def test(self, start: str, **kwargs: Any) -> tuple[int, str]:
        return self.run("test", str(self.write(start, **kwargs)))

    def spawn(self, *args: str) -> "Spawned":
        """The scrimmage command as its own process, as a user's shell starts it, so a signal
        reaches it and nothing else."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
        out = (self.root / "out.txt").open("w")
        try:
            proc = subprocess.Popen(
                [str(SCRIMMAGE), *args], cwd=self.root, env=env, stdout=out, stderr=out
            )
        finally:
            out.close()
        spawned = Spawned(proc, self.root / "out.txt")
        self.spawned.append(spawned)
        return spawned

    def log(self) -> Path:
        return next((self.root / "scrimmage-runs").glob("*/service.log"))

    def close(self) -> None:
        for spawned in self.spawned:
            spawned.close()
        self.fake.stop()
        # A test that failed part way leaves nothing behind: SIGKILL to the copy's group by
        # hand, so a broken stop in the code under test cannot leave it running.
        for copy in self.copies:
            if copy.proc is not None and group(copy.proc.pid):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(copy.proc.pid, signal.SIGKILL)
                copy.proc.wait()


class Spawned:
    """A scrimmage command in its own process, and the copy of the service it started."""

    def __init__(self, proc: subprocess.Popen[bytes], out: Path) -> None:
        self.proc, self.out = proc, out
        self.copy = 0  # the copy's pid, which leads its process group
        self.token_folder: Path | None = None

    def found_copy(self) -> bool:
        """The copy has started, and its environment names the token file."""
        for pid in children(self.proc.pid):
            with contextlib.suppress(OSError):
                named = environ(pid).get("SCRIMMAGE_TOKEN_FILE")
                if named:
                    self.copy, self.token_folder = pid, Path(named).parent
                    GROUPS.append(pid)
                    return True
        return False

    def text(self) -> str:
        return self.out.read_text()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.copy and group(self.copy):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.copy, signal.SIGKILL)
        if self.token_folder is not None:
            shutil.rmtree(self.token_folder, ignore_errors=True)


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Iterator[Shop]:
    made = Shop(tmp_path, monkeypatch, capsys)
    try:
        yield made
    finally:
        made.close()


def test_a_managed_test_starts_the_copy_answers_its_runs_and_stops_it(shop: Shop) -> None:
    """A team's start script that starts uvicorn with two workers and waits on it: stopping
    the copy's group stops the script, the supervisor and both workers."""
    (shop.root / "serve.sh").write_text(
        f"{UVICORN} acme.service:app --port $1 --loop asyncio --workers 2 &\nwait\n"
    )
    seen: list[list[int]] = []
    shop.at_stop.append(lambda copy: seen.append(group(copy.proc.pid)) if copy.proc else None)
    check_service = runner.Runner.check_service

    def both_active(self: runner.Runner) -> None:
        # Before the runs: on a loaded machine a worker can still be loading the app when
        # runs this fast are done.
        wait_until(lambda: shop.log().read_text().count("active in service mode") == 2, 60)
        check_service(self)

    shop.monkeypatch.setattr(runner.Runner, "check_service", both_active)
    code, out = shop.test("sh serve.sh {port}", runs=2)
    assert code == 0, out
    assert "SCRIMMAGE_TOKEN_FILE" not in os.environ
    assert len(shop.copies) == 1
    pgid = shop.copies[0].proc.pid if shop.copies[0].proc else 0
    # The script, the supervisor and two workers were running while the test ran.
    assert len(seen[0]) >= 4, seen
    assert group(pgid) == [], "a process of the copy's group was left running"
    active = shop.log().read_text().count("scrimmage: active in service mode")
    assert active == 2, shop.log().read_text()


def test_the_child_gets_the_settings_and_no_other_scrimmage_variable(shop: Shop) -> None:
    shop.monkeypatch.setenv("SCRIMMAGE_TOKEN", "f00d" * 16)  # left from another test
    shop.monkeypatch.setenv("SCRIMMAGE_CHILDREN", "1")
    shop.monkeypatch.setenv("SCRIMMAGE_CONTROL_URL", "http://127.0.0.1:1")
    found: list[dict[str, str]] = []

    def read(copy: Managed) -> None:
        if copy.proc is not None and copy.alive():  # a copy that stopped has no environ left
            found.append(environ(copy.proc.pid))

    shop.at_stop.append(read)
    code, out = shop.test(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
    assert code == 0, out
    env = found[0]
    ours = {key: value for key, value in env.items() if key.startswith("SCRIMMAGE_")}
    # SCRIMMAGE_ROOT_PID is scrimmage-run's own, set as it becomes uvicorn.
    assert set(ours) == {
        "SCRIMMAGE_SERVICE",
        "SCRIMMAGE_TOKEN_FILE",
        "SCRIMMAGE_CONTROL_URL",
        "SCRIMMAGE_TOOLS",
        "SCRIMMAGE_ALLOW_IMPORTS",
        "SCRIMMAGE_MANAGED",
        "SCRIMMAGE_ROOT_PID",
    }, sorted(ours)
    copy = shop.copies[0]
    assert ours["SCRIMMAGE_SERVICE"] == "1"
    assert ours["SCRIMMAGE_MANAGED"] == "1"
    assert ours["SCRIMMAGE_TOOLS"] == ",".join(TOOLS)
    assert ours["SCRIMMAGE_ALLOW_IMPORTS"] == "acme."
    assert ours["SCRIMMAGE_CONTROL_URL"].startswith("http://127.0.0.1:")
    assert ours["SCRIMMAGE_CONTROL_URL"] != "http://127.0.0.1:1"
    assert copy.token_folder is not None
    assert ours["SCRIMMAGE_TOKEN_FILE"] == str(copy.token_folder / "token")
    leaked = [value for value in env.values() if copy.token in value]
    assert not leaked, "the token is in the copy's environment"
    assert env.get("OPENAI_BASE_URL") == shop.fake.base_url  # the rest of the environment


def test_the_token_file_is_private_and_gone_after(shop: Shop) -> None:
    modes: list[tuple[int, int, int, int, bool]] = []

    def checked(copy: Managed) -> None:
        folder = copy.token_folder
        assert folder is not None
        token = folder / "token"
        here, there = folder.stat(), token.stat()
        modes.append(
            (
                stat.S_IMODE(here.st_mode),
                stat.S_IMODE(there.st_mode),
                here.st_uid,
                there.st_uid,
                stat.S_ISREG(there.st_mode),
            )
        )

    stopped_with_the_file: list[bool] = []
    real_stop = runner.stop_groups

    def stop_groups(procs: Any) -> None:
        # The file must outlive the copy: a worker uvicorn restarts reads it at its start.
        for copy in shop.copies:
            if copy.proc in procs and copy.token_folder is not None:
                stopped_with_the_file.append((copy.token_folder / "token").is_file())
        real_stop(procs)

    shop.monkeypatch.setattr(runner, "stop_groups", stop_groups)
    shop.at_stop.append(checked)
    code, out = shop.test(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
    assert code == 0, out
    uid = os.getuid()
    assert modes == [(0o700, 0o600, uid, uid, True)]
    assert stopped_with_the_file == [True]
    first = shop.copies[0].token_folder
    assert first is not None and not first.exists()
    assert not (shop.log().parent / "token").exists()  # never in the records folder
    # A copy that never became ready: exit 3, and the folder is gone too.
    code, out = shop.test(f"{UVICORN} broken:app --port {{port}}")
    assert code == 3, out
    second = shop.copies[1].token_folder
    assert second is not None and second != first and not second.exists()
    assert stopped_with_the_file == [True, True]


def test_the_port_is_filled_into_start_and_service(shop: Shop) -> None:
    command: list[list[str]] = []

    def read(copy: Managed) -> None:
        assert copy.proc is not None
        raw = Path(f"/proc/{copy.proc.pid}/cmdline").read_bytes()
        command.append([part.decode() for part in raw.split(b"\0") if part])

    shop.at_stop.append(read)
    path = shop.write(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
    code, out = shop.run("test", str(path), "--json")
    assert code == 0, out
    port = shop.copies[0].port
    assert 0 < port < 65536
    assert command[0][command[0].index("--port") + 1] == str(port)
    log = shop.log().read_text()
    assert f"Uvicorn running on http://127.0.0.1:{port}" in log, log
    # Every run went to that address: the copy served all of them.
    records = list((shop.root / "scrimmage-runs").glob("*/run-*.jsonl"))
    assert records and all(r.read_text() for r in records)
    # The file is unchanged, and the address keeps its written form.
    cfg = config.load(path)
    assert cfg.agent.service == "http://127.0.0.1:{port}/chat"
    assert config.service_url(cfg.agent, port) == f"http://127.0.0.1:{port}/chat"


def test_the_childs_output_is_in_service_log_and_scrubbed(shop: Shop) -> None:
    shop.monkeypatch.setenv("SHOP_KEY", SHOP_KEY)
    extra = 'headers = {"x-shop-key" = "${SHOP_KEY}"}'
    code, out = shop.test(f"{UVICORN} prints:app --port {{port}} --loop asyncio", extra=extra)
    assert code == 0, out
    log = shop.log()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text()
    assert "the shop key is" in text and "Application startup complete" in text, text
    leaked = SHOP_KEY in text
    assert not leaked, "service.log holds a value ${NAME} filled in"


def test_a_child_that_exits_before_ready_is_exit_3_with_its_last_lines(shop: Shop) -> None:
    began = time.monotonic()
    code, out = shop.test(f"{UVICORN} broken:app --port {{port}}")
    took = time.monotonic() - began
    assert code == 3, out
    assert "the service started with [agent] start exited with code 1 before it was ready" in out
    assert "RuntimeError: the shop database is down" in out, out
    assert f"its whole output is in {shop.log()}" in out, out
    assert len(shop.copies[0].ports) == 1 and "was taken before" not in out, out  # once
    # It said so as soon as the copy exited, not after the probe's 30 s.
    assert took < runner.READY_S / 2, took


def test_a_child_that_never_answers_is_stopped_after_the_wait(shop: Shop) -> None:
    shop.monkeypatch.setattr(runner, "READY_S", 2.0)
    code, out = shop.test(f"{UVICORN} sleepy:app --port {{port}}")
    assert code == 3, out
    assert "the service started with [agent] start did not answer a probe in 2 s" in out, out
    copy = shop.copies[0]
    assert copy.proc is not None and copy.proc.returncode is not None
    assert group(copy.proc.pid) == []
    assert copy.token_folder is not None and not copy.token_folder.exists()


def test_a_child_that_ignores_sigterm_is_killed_after_the_deadline(shop: Shop) -> None:
    """Both workers ignore SIGTERM, so the supervisor waits on them: only the SIGKILL to the
    whole group, after the grace, ends them."""
    took: list[float] = []
    real_stop = runner.stop_groups

    def stop_groups(procs: Any) -> None:
        began = time.monotonic()
        real_stop(procs)
        if any(copy.proc in procs for copy in shop.copies):  # not the runs' own stops
            took.append(time.monotonic() - began)

    shop.monkeypatch.setattr(runner, "stop_groups", stop_groups)
    start = f"{UVICORN} stubborn:stubborn --port {{port}} --loop asyncio --workers 2"
    code, out = shop.test(start)
    assert code == 0, out
    copy = shop.copies[0]
    assert copy.proc is not None
    assert group(copy.proc.pid) == [], "a worker that ignores SIGTERM was left running"
    # The lower bound is the grace; the upper leaves room for the SIGKILL and the reap
    # on a loaded machine, and the empty group above proves the kill.
    assert len(took) == 1 and runner.KILL_GRACE_S <= took[0] < runner.KILL_GRACE_S + 5.0, took


def test_check_service_starts_and_stops_the_copy(shop: Shop) -> None:
    path = shop.write(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
    code, out = shop.run("check", str(path), "--service")
    assert code == 0, out
    start = f"{UVICORN} acme.service:app --port {{port}} --loop asyncio"
    assert f"the service starts with: {start}" in out, out
    assert "start the service with these two set" not in out
    copy = shop.copies[0]
    assert f"POST http://127.0.0.1:{copy.port}/chat   gated" in out, out
    assert "(match the file)" in out and "reached, no TLS, on this machine" in out, out
    assert copy.proc is not None and copy.proc.returncode is not None
    assert group(copy.proc.pid) == []
    assert copy.token_folder is not None and not copy.token_folder.exists()
    assert not (shop.root / "scrimmage-runs").exists()  # the scratch folder, log and all
    # A copy that fails: its last lines are printed, and no path to a removed log.
    path = shop.write(f"{UVICORN} broken:app --port {{port}}")
    code, out = shop.run("check", str(path), "--service")
    assert code == 3, out
    assert "RuntimeError: the shop database is down" in out and "whole output" not in out, out


def test_a_ctrl_c_while_the_copy_stops_still_kills_its_workers(shop: Shop) -> None:
    """The runs are done and the copy is stopping: its workers ignore SIGTERM, so it waits
    out the grace. A Ctrl-C then must not cut the stop short: the SIGKILL still comes, the
    token's folder goes only once the group is empty, and the test says it was stopped."""
    mark = shop.root / "stop-began"
    shop.monkeypatch.setenv("STOP_MARK", str(mark))
    path = shop.write(f"{UVICORN} marking:marking --port {{port}} --loop asyncio --workers 2")
    spawned = shop.spawn("test", str(path))
    wait_until(spawned.found_copy)
    folder = spawned.token_folder
    assert folder is not None
    early: list[list[int]] = []  # the group, when the folder was first found gone

    def watch() -> None:
        while spawned.proc.poll() is None:
            if not folder.exists():
                early.append(group(spawned.copy))
                return
            time.sleep(0.005)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    wait_until(mark.exists, within=60)
    time.sleep(0.5)  # well inside the 5 s grace
    began = time.monotonic()
    spawned.proc.send_signal(signal.SIGINT)
    code = spawned.proc.wait(30)
    took = time.monotonic() - began
    watcher.join(10)
    out = spawned.text()
    assert code == 3, out
    # Every run had ended, so none was cut, and their result is kept, marked stopped.
    assert "scrimmage: stopped by SIGINT after every run had ended" in out, out
    (result,) = (shop.root / "scrimmage-runs").glob("*/result.json")
    kept = json.loads(result.read_text())
    assert kept["stopped_by"] == "interrupted" and kept["rows"], kept
    assert "Traceback" not in out, out
    assert group(spawned.copy) == [], "a worker that ignores SIGTERM was left running"
    assert early == [[]], f"the token folder went while {early} still ran"
    assert not folder.exists()
    assert took < runner.KILL_GRACE_S + 5.0, took


def check_service_stopped_by(shop: Shop, sig: signal.Signals) -> None:
    """scrimmage check --service, stopped while it waits for a copy that never answers."""
    path = shop.write(f"{UVICORN} sleepy:app --port {{port}} --workers 2")
    spawned = shop.spawn("check", str(path), "--service")
    wait_until(spawned.found_copy)
    wait_until(lambda: len(group(spawned.copy)) >= 3)  # the supervisor and both workers
    spawned.proc.send_signal(sig)
    code = spawned.proc.wait(30)
    out = spawned.text()
    assert code == 3, out
    assert f"scrimmage: stopped by {sig.name}" in out, out
    assert "Traceback" not in out, out
    assert group(spawned.copy) == [], "a process of the copy was left running"
    assert spawned.token_folder is not None and not spawned.token_folder.exists()


def test_sigterm_during_check_service_stops_the_copy_and_removes_the_token_file(
    shop: Shop,
) -> None:
    check_service_stopped_by(shop, signal.SIGTERM)


def test_ctrl_c_during_check_service_stops_the_copy_with_no_traceback(shop: Shop) -> None:
    check_service_stopped_by(shop, signal.SIGINT)


class NotFound(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@contextlib.contextmanager
def held_port() -> Iterator[int]:
    """A port another process holds, as one that took it between the pick and the bind:
    it answers every request with 404."""
    holder = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
    held = threading.Thread(target=holder.serve_forever, daemon=True)
    held.start()
    try:
        yield int(holder.server_address[1])
    finally:
        holder.shutdown()
        holder.server_close()


def no_proc(shop: Shop) -> None:
    """As on a system without /proc/net/tcp, such as macOS: who holds a port is unknown."""
    shop.monkeypatch.setattr(managed, "sockets_on", lambda port, listen_only=False: None)


@pytest.mark.parametrize("proc", [True, False], ids=["proc", "no-proc"])
def test_a_port_taken_before_the_bind_starts_the_copy_again_on_another(
    shop: Shop, proc: bool
) -> None:
    """The first port the runner picks is taken before the copy binds it: the copy exits
    with uvicorn's "address already in use", and the runner starts it again on a new port,
    which every run then goes to. With /proc, the socket that holds the port says so;
    without it, uvicorn's line does."""
    if not proc:
        no_proc(shop)
    real = managed.free_port
    with held_port() as taken:
        picks = iter([taken])
        shop.monkeypatch.setattr(runner, "free_port", lambda: next(picks, None) or real())
        code, out = shop.test(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
    assert code == 0, out
    copy = shop.copies[0]
    assert len(shop.copies) == 1 and copy.ports == [taken, copy.port] and copy.port != taken
    again = f"started it again on port {copy.port}, start 2 of {managed.START_TRIES}"
    assert f"scrimmage: port {taken} was taken before the service bound it; {again}" in out
    log = shop.log().read_text()
    # uvicorn on 3.14 puts "[errno 98]" between the address and "address already in use".
    assert f"('127.0.0.1', {taken}): " in log and "address already in use" in log, log
    assert f"Uvicorn running on http://127.0.0.1:{copy.port}" in log, log
    records = list((shop.root / "scrimmage-runs").glob("*/run-*.jsonl"))
    assert records and all(r.read_text() for r in records)
    assert copy.proc is not None and group(copy.proc.pid) == []


@pytest.mark.parametrize("which", ["unused", "session"])
@pytest.mark.parametrize("proc", [True, False], ids=["proc", "no-proc"])
def test_a_line_that_says_in_use_with_the_port_is_not_started_again(
    shop: Shop, which: str, proc: bool
) -> None:
    """A copy that prints its port beside the words "in use", then fails with no bind,
    lost no port: it is started once."""
    if not proc:
        no_proc(shop)
    code, out = shop.test(f"{sys.executable} chatty.py {{port}} {which}")
    assert code == 3, out
    assert "exited with code 1 before it was ready" in out and "in use" in out, out
    assert "was taken before" not in out and "starts on ports" not in out, out
    assert len(shop.copies) == 1 and len(shop.copies[0].ports) == 1


def test_a_dead_copys_own_connection_is_not_another_process_on_its_port(shop: Shop) -> None:
    """The copy binds its port, takes a client's connection, and crashes while the client
    keeps its end open. The copy's end then waits in FIN_WAIT2, owned by no process: that
    is not another process on the port, so the copy is started once."""
    real = managed.free_port
    picked: list[int] = []
    shop.monkeypatch.setattr(runner, "free_port", lambda: picked.append(real()) or picked[-1])
    clients: list[socket.socket] = []
    done = threading.Event()

    def hold() -> None:
        while not done.is_set():
            if not picked:
                time.sleep(0.02)
                continue
            client = socket.socket()
            try:
                client.connect(("127.0.0.1", picked[0]))
            except OSError:
                client.close()
                time.sleep(0.02)
                continue
            client.sendall(b"hold")
            clients.append(client)  # kept open until the test has ended
            return

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        code, out = shop.test(f"{sys.executable} holds.py {{port}}")
    finally:
        done.set()
        holder.join(5)
        for client in clients:
            client.close()
    assert clients, "the client never reached the copy"
    assert code == 3, out
    assert "RuntimeError: the shop crashed while it held a client" in out, out
    assert "was taken before" not in out and "starts on ports" not in out, out
    assert len(shop.copies) == 1 and shop.copies[0].ports == picked[:1], out


def test_a_start_that_forks_a_daemon_and_exits_0_is_not_started_again(shop: Shop) -> None:
    """The command forks a daemon into a session of its own, which binds the port, and
    exits 0: a clean exit is no failed bind, so the copy is started once, and one daemon
    is left, the documented limit of setsid."""
    pids = shop.root / "daemons.txt"
    try:
        code, out = shop.test(f"{sys.executable} daemon.py {pids} {{port}}")
        assert code == 3, out
        assert "exited with code 0 before it was ready" in out, out
        assert "was taken before" not in out and "starts on ports" not in out, out
        assert len(shop.copies) == 1 and len(shop.copies[0].ports) == 1
    finally:
        for _ in range(2):  # a late daemon is still found; no file: leave the real failure
            with contextlib.suppress(AssertionError):
                wait_until(pids.exists, 10)
            if pids.exists():
                break
        if pids.exists():
            for pid in [int(pid) for pid in pids.read_text().split()]:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


def test_a_start_again_stops_what_the_last_start_left_in_its_group(shop: Shop) -> None:
    """The first start leaves a child in its group, then fails to bind a port another
    process took: that child is stopped before the copy starts again, not left running."""
    pids = shop.root / "children.txt"
    real = managed.free_port
    try:
        with held_port() as taken:
            picks = iter([taken])
            shop.monkeypatch.setattr(runner, "free_port", lambda: next(picks, None) or real())
            code, out = shop.test(f"sh lingers.sh {pids} {{port}}")
        assert code == 0, out
        assert shop.copies[0].ports[0] == taken and len(shop.copies[0].ports) == 2, out
        left = [int(pid) for pid in pids.read_text().split()]
        assert len(left) == 2, left
        assert [pid for pid in left if alive(pid)] == [], "a start's child was left running"
    finally:
        for pid in [int(pid) for pid in pids.read_text().split()] if pids.exists() else []:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_a_port_the_command_fixes_is_not_started_again(shop: Shop) -> None:
    """The command holds {port}, but binds a port of its own that another process holds:
    a new port would not help, so the copy is started once and its bind error shown."""
    with held_port() as fixed:
        start = f"{UVICORN} acme.service:app --port {fixed} --root-path /{{port}} --loop asyncio"
        code, out = shop.test(start)
    assert code == 3, out
    assert f"('127.0.0.1', {fixed}): " in out and "address already in use" in out, out
    assert "was taken before" not in out and "starts on ports" not in out, out
    assert len(shop.copies) == 1 and len(shop.copies[0].ports) == 1


@pytest.mark.parametrize("proc", [True, False], ids=["proc", "no-proc"])
def test_a_port_held_by_another_is_not_blamed_for_a_bind_that_failed_elsewhere(
    shop: Shop, proc: bool
) -> None:
    """Another process holds the picked port, but the copy fails to bind a port its command
    fixes: its log names that address, so a new picked port would not help. The copy is
    started once and its bind error shown, not blamed on the picked port."""
    if not proc:
        no_proc(shop)
    with held_port() as picked, held_port() as fixed:
        shop.monkeypatch.setattr(runner, "free_port", lambda: picked)
        start = f"{UVICORN} acme.service:app --port {fixed} --root-path /{{port}} --loop asyncio"
        code, out = shop.test(start)
    assert code == 3, out
    assert f"('127.0.0.1', {fixed}): " in out and "address already in use" in out, out
    assert "was taken before" not in out and "starts on ports" not in out, out
    assert len(shop.copies) == 1 and len(shop.copies[0].ports) == 1


@pytest.mark.parametrize(
    ("which", "proc"),
    [("ipv4", True), ("ipv6", True), ("ipv6", False), ("time", True)],
    ids=["ipv4", "ipv6", "ipv6-no-proc", "time-no-address"],
)
def test_a_bind_error_in_any_wording_blames_the_picked_port_it_names(
    shop: Shop, which: str, proc: bool
) -> None:
    """Another process holds the picked port and the copy's log says the bind failed on it,
    as ('::1', PORT, 0, 0) for an IPv6 host, or with no address at all but a time in the
    line: the copy is started again, as many times as are allowed."""
    if not proc:
        no_proc(shop)
    with held_port() as taken:
        shop.monkeypatch.setattr(runner, "free_port", lambda: taken)
        code, out = shop.test(f"{sys.executable} binds.py {{port}} {which}")
    assert code == 3, out
    assert f"scrimmage: port {taken} was taken before the service bound it" in out, out
    assert len(shop.copies[0].ports) == managed.START_TRIES, out


def test_a_port_another_process_holds_names_the_copys_exit(shop: Shop) -> None:
    """Another process takes the port before the copy binds it, and answers the probe with
    404, at every start. The test names the copy's exit and its log's "address already in
    use", not the process that answered, once the starts are used up."""
    with held_port() as port:
        shop.monkeypatch.setattr(runner, "free_port", lambda: port)
        code, out = shop.test(f"{UVICORN} acme.service:app --port {{port}} --loop asyncio")
    assert code == 3, out
    # uvicorn exits with code 3 when it cannot bind its port.
    tries = managed.START_TRIES
    ports = ", ".join([str(port)] * tries)
    exited = "the service started with [agent] start exited with code 3 before it was ready"
    assert f"{exited}, after {tries} starts on ports {ports}" in out, out
    assert out.count("was taken before the service bound it") == tries - 1, out
    assert "address already in use" in out, out
    assert "does not reach Scrimmage" not in out, out


def test_a_scrimmage_py_in_the_service_folder_is_not_run_by_the_copys_start(shop: Shop) -> None:
    """The copy's supervisor is started with python -P, so the folder it starts in is not on
    its path and a team file named scrimmage there neither stands in for Scrimmage nor runs."""
    (shop.root / "scrimmage.py").write_text('open("SIDE_EFFECT", "w").write("ran")\n')
    (shop.root / "serve.sh").write_text(
        f"{UVICORN} acme.service:app --port $1 --loop asyncio &\nwait\n"
    )
    code, out = shop.test("sh serve.sh {port}")
    assert code == 0, out
    assert not (shop.root / "SIDE_EFFECT").exists(), "the team's scrimmage.py ran"
