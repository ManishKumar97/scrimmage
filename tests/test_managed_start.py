"""A service Scrimmage starts with [agent] start: the copy is started on a free port with
the token in a private file and the settings in its environment, it is ready once a gated
process answers the probe, and it is stopped, every process of its group, when the test ends.

Each test runs the real CLI against a real uvicorn copy of the example app, with no
SCRIMMAGE_ variable and no token file of the test's own.
"""

import contextlib
import json
import os
import shutil
import signal
import stat
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import ACME, EXAMPLE
from scrimmage_core import config, runner
from scrimmage_core.cli import main
from scrimmage_core.managed import Managed

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
SHOP_KEY = "shop-key-5a1d9c0e7b"


def group(pgid: int) -> list[int]:
    """The processes of a process group that are still running, zombies left out."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue  # it ended while it was read
        if fields[0] != "Z" and int(fields[2]) == pgid:
            found.append(int(entry.name))
    return found


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
        self.at_stop: list[Callable[[Managed], None]] = []
        start, stop = Managed.start, Managed.stop

        def started(copy: Managed, control_url: str) -> None:
            self.copies.append(copy)
            start(copy, control_url)

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

    def log(self) -> Path:
        return next((self.root / "scrimmage-runs").glob("*/service.log"))

    def close(self) -> None:
        self.fake.stop()
        # A test that failed part way leaves nothing behind: SIGKILL to the copy's group by
        # hand, so a broken stop in the code under test cannot leave it running.
        for copy in self.copies:
            if copy.proc is not None and group(copy.proc.pid):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(copy.proc.pid, signal.SIGKILL)
                copy.proc.wait()


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
        "SCRIMMAGE_ROOT_PID",
    }, sorted(ours)
    copy = shop.copies[0]
    assert ours["SCRIMMAGE_SERVICE"] == "1"
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
    assert len(took) == 1 and runner.KILL_GRACE_S <= took[0] < 6.0, took


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
