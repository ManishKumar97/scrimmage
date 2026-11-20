"""Real uvicorn processes under scrimmage-run: each worker is a service process, not a child.

Each test starts its own service, waits for the lines it prints, and stops it; every pid it
started, the supervisor and its workers, is checked gone after.
"""

import contextlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import scrimmage_core
from conftest import EXAMPLE
from ports import free_port
from scrimmage_core import protocol
from scrimmage_core.config import Mock, ModelHost
from scrimmage_core.control import ControlServer
from scrimmage_core.plan import Plan
from scrimmage_core.records import RecordStore
from service_harness import eventually

pytestmark = pytest.mark.slow
BIN = Path(sys.executable).parent
RECORD = json.loads((Path(scrimmage_core.__file__).parent / "schemas/record.v1.json").read_text())
TOOLS = (
    "acme.tools:get_order,acme.tools:check_inventory,acme.tools:refund_order,acme.tools:get_stock"
)
ACTIVE = re.compile(r"scrimmage: active in service mode \(pid (\d+)\)")
READY = "Application startup complete"


class Running:
    """One service: scrimmage-run uvicorn acme.service:app, its stderr read as it comes."""

    def __init__(
        self,
        root: Path,
        env: dict[str, str],
        args: list[str],
        app: str = "acme.service:app",
        bin_dir: Path = BIN,
        bare: bool = False,
    ) -> None:
        """bin_dir: the bin folder of the venv the service runs in, this one unless a test
        wants another SDK. bare: start uvicorn itself, with no scrimmage-run, so the
        environment's PYTHONPATH is all that switches Scrimmage on."""
        self.port = free_port()
        command = [str(bin_dir / "uvicorn"), app]
        if not bare:
            command.insert(0, str(bin_dir / "scrimmage-run"))
        command += ["--port", str(self.port), "--loop", "asyncio", *args]
        self.proc = subprocess.Popen(
            command, cwd=root, env=env, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True
        )
        self.lines: list[str] = []
        self.ready = threading.Event()
        self.ended = threading.Event()
        self.wanted = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.lines.append(line)
            # Each worker's app has started, and the port is bound: one worker logs its app's
            # start before it binds, so both lines are needed.
            bound = any("Uvicorn running on" in x for x in self.lines)
            if bound and sum(READY in x for x in self.lines) >= self.wanted:
                self.ready.set()
        self.ended.set()

    @property
    def text(self) -> str:
        return "".join(self.lines)

    def workers(self) -> list[int]:
        return [int(m.group(1)) for m in ACTIVE.finditer(self.text)]

    def stop(self) -> None:
        pids = [self.proc.pid, *self.workers()]
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.ended.wait(10)
        if self.proc.stderr is not None:
            self.proc.stderr.close()
        for pid in pids:  # every process this test started is gone
            gone = False
            for _ in range(200):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    gone = True
                    break
                threading.Event().wait(0.05)
            assert gone, f"process {pid} was left running"

    def post(
        self, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            data = json.dumps(body if body is not None else {}).encode()
            conn.request(
                "POST",
                path,
                body=data,
                headers={"content-type": "application/json", **(headers or {})},
            )
            reply = conn.getresponse()
            raw = reply.read()
            try:
                return reply.status, json.loads(raw)
            except ValueError:
                return reply.status, raw.decode()
        finally:
            conn.close()


@pytest.fixture
def lab(tmp_path: Path) -> Iterator["Lab"]:
    made = Lab(tmp_path)
    try:
        yield made
    finally:
        made.close()


class Lab:
    """A copy of the example, a token file, and the runner's control server."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "acme-bot"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.token = secrets.token_hex(32)
        self.token_file = tmp_path / "token"
        self.token_file.write_text(self.token)
        self.token_file.chmod(0o600)
        self.store = RecordStore(tmp_path / "records")
        self.server = ControlServer(self.token, self.store)
        self.server.start()
        self.services: list[Running] = []

    def env(self, **more: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
        env.update(
            PATH=f"{BIN}{os.pathsep}{env.get('PATH', '')}",
            SCRIMMAGE_SERVICE="1",
            SCRIMMAGE_TOKEN_FILE=str(self.token_file),
            SCRIMMAGE_CONTROL_URL=self.server.url,
            SCRIMMAGE_TOOLS=TOOLS,
            SCRIMMAGE_ALLOW_IMPORTS="acme.",
        )
        env.update(more)
        return {k: v for k, v in env.items() if v != ""}

    def start(
        self,
        *args: str,
        workers: int = 1,
        wait: bool = True,
        app: str = "acme.service:app",
        bare: bool = False,
        **env: str,
    ) -> Running:
        flags = ["--workers", str(workers)] if workers > 1 else []
        service = Running(self.root, self.env(**env), [*flags, *args], app)
        service.wanted = workers
        self.services.append(service)
        if wait:
            assert service.ready.wait(60) or service.ended.is_set(), service.text
            assert service.ready.is_set(), service.text
        return service

    def ended(self, service: Running) -> str:
        assert service.ended.wait(60), service.text
        assert service.proc.wait(30) != 0
        return service.text

    def open(
        self, mocks: tuple[Mock, ...] = (), real: tuple[str, ...] = ()
    ) -> tuple[str, dict[str, str]]:
        name = f"run-{secrets.token_hex(6)}"
        tools = tuple(TOOLS.split(","))
        plan = Plan(
            name,
            "t",
            "baseline",
            mocks,
            (),
            tools,
            real,
            60.0,
            model_hosts=(ModelHost("127.0.0.1", "openai"),),  # the fake model server
            service=True,
        )
        self.server.open_run(name, "baseline", plan.to_json(), service=True)
        return name, {"x-scrimmage-run": f"{name}.{protocol.run_mac(self.token, name)}"}

    def lines(self, name: str) -> list[dict[str, Any]]:
        from service_harness import eventually

        eventually(lambda: self.server.runs[name].made, within=30)
        return self.store.lines(name)

    def close(self) -> None:
        for service in self.services:
            service.stop()
        self.server.stop()


@pytest.mark.design("service.workers_are_service")
def test_uvicorn_workers_start_in_service_mode_not_as_children(lab: Lab) -> None:
    service = lab.start(workers=2)
    pids = service.workers()
    assert len(pids) == 2 and service.proc.pid not in pids
    name, headers = lab.open(real=("tool:check_inventory",))
    status, reply = service.post("/child", headers=headers)
    assert status == 200 and "service runs refuse every process start" in reply["reply"]
    lines = lab.lines(name)
    assert {x["proc"]["pid"] for x in lines} <= set(pids)
    assert not any(x.get("notice") == "process" for x in lines)  # no worker joined as a child


def trackers_in(folder: Path) -> list[int]:
    """The resource trackers running with this folder as their working folder."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            line = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            here = os.readlink(entry / "cwd")
        except OSError:  # gone, or not ours to read
            continue
        if b"resource_tracker import main" in line and Path(here) == folder:
            found.append(int(entry.name))
    return found


@pytest.mark.design("service.resource_tracker")
@pytest.mark.parametrize("workers", [1, 2])
def test_a_service_starts_one_resource_tracker_and_the_tracker_starts_none(
    lab: Lab, workers: int
) -> None:
    """The service starts the shared tracker as it starts; the tracker inherits the
    service's environment, and Scrimmage stays off in it, so it starts no tracker of its own."""
    service = lab.start(workers=workers)
    first = trackers_in(lab.root)
    # Counting is what this checks: a tracker that started one would show more by now.
    threading.Event().wait(1.0)
    assert len(first) == 1 and trackers_in(lab.root) == first, (first, service.text[-2000:])


@pytest.mark.design("service.resource_tracker")
def test_a_service_mode_python_that_never_serves_starts_no_tracker(lab: Lab) -> None:
    """Only a process that serves the app, or supervises its workers, starts the tracker."""
    code = (
        "from multiprocessing import resource_tracker\n"
        "from scrimmage_core import state\n"
        "print(state.process() is not None, resource_tracker._resource_tracker._pid)\n"
    )
    done = subprocess.run(
        [str(BIN / "scrimmage-run"), sys.executable, "-c", code],
        cwd=lab.root,
        env=lab.env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.stdout.split() == ["True", "None"], done.stdout + done.stderr


TRACKER = "from multiprocessing.resource_tracker import main;main(5)"
HELPER_LINES = {
    "tracker": ["-c", TRACKER],
    "tracker with options": ["-X", "faulthandler", "-Wdefault", "-c", TRACKER],
    "tracker with joined and long options": [
        "-Xdev",
        "-W",
        "default",
        "--check-hash-based-pycs",
        "always",
        "-c",
        TRACKER,
    ],
    "forkserver": [
        "-c",
        "from multiprocessing.forkserver import main; main(3, 4, ['__main__'], **{'a': [1]})",
    ],
    "forkserver reading argv": [
        "-c",
        "import sys; from multiprocessing.forkserver import main; "
        "main(3, 4, ['__main__'], sys_argv=sys.argv[1:], **{'authkey_r': 7})",
        "app.py",
        "--port",
    ],
    "loky's tracker": [
        "-c",
        "from joblib.externals.loky.backend.resource_tracker import main; main(5, False)",
    ],
}
LOOKALIKE_LINES = {
    "more code after": ["-c", TRACKER + "; import os"],
    "a comment": ["-c", "import os  # from multiprocessing.resource_tracker import main"],
    "a script first": ["app.py", "-c", TRACKER],
    "a module first": ["-m", "app", "-c", TRACKER],
    "code joined to a -c first": ["-cimport os", "-c", TRACKER],
    "a module joined to -m first": ["-mapp", "-c", TRACKER],
    "-X taking the -c as its value": ["-X", "-c", TRACKER],
    "a call in the arguments": [
        "-c",
        "from multiprocessing.resource_tracker import main; main(__import__('os').getpid())",
    ],
    "arguments after": ["-c", TRACKER, "more"],
    "a computed keyword": [
        "-c",
        "from multiprocessing.forkserver import main; main(3, 4, [], **dict(a=print()))",
    ],
}


@pytest.mark.design("service.helper_exact")
@pytest.mark.parametrize("name", sorted(HELPER_LINES))
def test_a_helpers_exact_command_line_keeps_scrimmage_off(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from scrimmage_core import bootstrap

    monkeypatch.setattr(sys, "orig_argv", [sys.executable, *HELPER_LINES[name]])
    assert bootstrap.multiprocessing_helper()


@pytest.mark.design("service.helper_exact")
@pytest.mark.parametrize("name", sorted(LOOKALIKE_LINES))
def test_a_command_line_that_only_looks_like_a_helpers_keeps_scrimmage_on(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from scrimmage_core import bootstrap

    monkeypatch.setattr(sys, "orig_argv", [sys.executable, *LOOKALIKE_LINES[name]])
    assert not bootstrap.multiprocessing_helper()


@pytest.mark.design("service.active_line")
def test_the_active_line_prints_only_where_the_entry_gate_is(lab: Lab) -> None:
    service = lab.start(workers=2)
    assert len(service.workers()) == 2  # one per worker; the supervisor loads no app


@pytest.mark.design("service.token_file_popped")
def test_a_token_file_path_is_popped_in_each_worker(lab: Lab) -> None:
    service = lab.start(workers=2)
    seen = {service.post("/env")[1]["pid"]: service.post("/env")[1] for _ in range(8)}
    assert seen and not any(view["token_file"] for view in seen.values())


@pytest.mark.design("service.team_children_untouched")
def test_a_teams_spawn_pool_worker_started_from_a_worker_stays_off_and_runs(lab: Lab) -> None:
    service = lab.start()
    status, view = service.post("/spawn")
    assert status == 200 and view == {"scrimmage": False, "token_file": False}, json.dumps(view)


@pytest.mark.design("service.token_file_private")
def test_the_token_file_must_be_0600_and_owned(lab: Lab) -> None:
    lab.token_file.chmod(0o644)
    service = lab.start(wait=False)
    text = lab.ended(service)
    assert "has mode 644; it must be 600" in text
    assert "the service stops before it serves a request" in text and "agent runs" not in text


@pytest.mark.design("service.loop_check_message")
@pytest.mark.needs_python(max=(3, 11), reason="uvloop is stopped on Python 3.11 only")
def test_uvloop_on_3_11_stops_the_service_with_one_prefix(lab: Lab) -> None:
    pytest.importorskip("uvloop")
    service = lab.start("--loop", "uvloop", wait=False)
    text = lab.ended(service)
    assert "--loop asyncio" in text and "scrimmage: scrimmage:" not in text
    # A structure stop says the service stops, not that it never served: one found inside a
    # request is worded the same.
    assert "; the service stops\n" in text and "before it serves a request" not in text


@pytest.mark.design("service.token_in_env_stops")
def test_scrimmage_token_in_a_service_environment_stops_naming_the_file(lab: Lab) -> None:
    service = lab.start(wait=False, SCRIMMAGE_TOKEN=lab.token)
    text = lab.ended(service)
    assert "SCRIMMAGE_TOKEN is set" in text and "SCRIMMAGE_TOKEN_FILE" in text


@pytest.mark.design("service.zttp_refused")
def test_zttp_is_refused_at_the_structure_check(lab: Lab) -> None:
    service = lab.start("--http", "zttp", wait=False)
    assert "uvicorn --http zttp is not tested with Scrimmage" in lab.ended(service)


@pytest.mark.design("service.mode_needed")
def test_a_service_without_service_mode_stops_with_a_message_naming_it(lab: Lab) -> None:
    service = lab.start(wait=False, SCRIMMAGE_SERVICE="")
    assert "set SCRIMMAGE_SERVICE=1" in lab.ended(service)


@pytest.mark.design("service.structure_before_listening")
def test_a_library_that_cannot_be_gated_stops_the_service_before_it_listens(lab: Lab) -> None:
    service = lab.start(wait=False, SCRIMMAGE_TOOLS="acme.tools:no_such_tool")
    text = lab.ended(service)
    assert "has no function no_such_tool" in text and READY not in text


@pytest.mark.design("service.child_refused")
def test_a_python_child_in_a_service_run_is_refused_and_outside_a_run_is_untouched(
    lab: Lab,
) -> None:
    service = lab.start()
    assert service.post("/child") == (200, {"reply": "child ran"})
    name, headers = lab.open()
    status, reply = service.post("/child", headers=headers)
    assert status == 200 and 'Add "process:python' in reply["reply"]
    refused = [x for x in lab.lines(name) if x.get("reason") == "service_child"]
    assert refused and refused[0]["by_config"] is True


@pytest.mark.design("service.plain_thread_left_running")
def test_a_plain_requests_waited_work_runs_and_work_it_left_running_meets_the_outside_rules(
    lab: Lab,
) -> None:
    service = lab.start(app="acme.service:served")
    name, headers = lab.open()
    with ThreadPoolExecutor(1) as pool:  # the run's request is in flight throughout
        held = pool.submit(service.post, "/hold", None, headers)
        assert service.post("/left-running") == (200, {"reply": "started"})
        status, reply = held.result(60)
    assert status == 200 and reply["seen"] == [
        "waited for: ran",
        "background: ran",
        "left running: ScrimmageRefused",
    ]
    refused = [x for x in lab.lines(name) if x.get("reason") == "orphan"]
    assert [x["call"] for x in refused] == ["tool:check_inventory"]
    assert refused[0]["outside"] is True and "background_entry" in refused[0]
    check = jsonschema.Draft202012Validator(RECORD)
    assert [e.message for x in lab.lines(name) for e in check.iter_errors(x)] == []


@pytest.mark.design("service.sync_endpoint")
def test_a_sync_endpoint_finds_its_plan_through_anyio(lab: Lab) -> None:
    from conftest import ACME
    from fakes.openai_server import FakeOpenAI

    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        service = lab.start(OPENAI_BASE_URL=fake.base_url, OPENAI_API_KEY="test")
        mocks = (
            Mock("tool:get_order", "shipped, Oct 30", None),
            Mock("tool:get_stock", {"sku": "SKU-42", "count": 12}, "acme.models:Stock"),
        )
        # The openai client runs `uname -p` through platform to name its machine: listed.
        real = ("tool:check_inventory", "process:uname")
        name, headers = lab.open(mocks=mocks, real=real)
        status, reply = service.post("/chat-sync", {"message": "Where is order 1234?"}, headers)
    finally:
        fake.stop()
    assert status == 200, (reply, service.text[-3000:])
    calls = [x for x in lab.lines(name) if x.get("call") == "tool:get_order"]
    assert calls and calls[0]["action"] == "mock" and calls[0]["run"] == name


@pytest.mark.design("service.tools_listed_at_startup")
def test_mcp_tools_listed_at_startup_are_seen_by_a_run(lab: Lab, tmp_path: Path) -> None:
    import fakes
    from fakes.openai_server import FakeOpenAI

    fake = FakeOpenAI(["acme_answer"])
    fake.start()
    try:
        service = lab.start(
            app="acme.service_mcp:app",
            OPENAI_BASE_URL=fake.base_url,
            OPENAI_API_KEY="test",
            DOCS_SERVER=str(Path(fakes.__file__).parent / "docs_server.py"),
            DOCS_LOG=str(tmp_path / "docs.log"),
        )
        name, headers = lab.open(real=("process:uname",))
        status, reply = service.post("/chat", {"message": "Where is order 1234?"}, headers)
    finally:
        fake.stop()
    assert status == 200, (reply, service.text[-3000:])
    offered = [t["function"]["name"] for t in fake.requests[0]["tools"]]
    assert "search" in offered  # listed in the lifespan, before any run, and offered here
    lines = lab.lines(name)
    models = [x for x in lines if x.get("call") == "model:openai"]
    assert models and models[0]["action"] == "real", lines
    assert not any(x.get("reason") == "unseen_tool" for x in lines)


# scrimmage test against a service: the probe, the setup request, each run's POST.

FILE = """[agent]
service = "http://127.0.0.1:PORTPATH"
body    = {message = "{question}", session = "{session}"}
answer  = "ANSWER"
tools   = TOOL_LIST
real    = ["tool:check_inventory", "process:uname"]
model_hosts = [{host = "127.0.0.1", provider = "openai"}]
runs = RUNS
run_timeout = TIMEOUT
record_grace_s = GRACE
record_wait_s = WAIT
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


class Forwarder:
    """A standard-library proxy in front of the service. drop: it removes every x-scrimmage-*
    header. runs_to: where it sends each run's request, apart from the probe and setup."""

    def __init__(
        self, service_port: int, *, drop: bool = False, runs_to: int | None = None
    ) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        forwarder = self
        self.seen: list[str] = []
        self.times: list[float] = []  # when each request came, as seen is

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                headers = {
                    k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")
                }
                run = self.headers.get("x-scrimmage-run", "")
                forwarder.seen.append(run)
                forwarder.times.append(time.monotonic())
                if drop:
                    headers = {
                        k: v for k, v in headers.items() if not k.lower().startswith("x-scrimmage-")
                    }
                port = service_port
                if runs_to is not None and run.startswith("run-"):
                    port = runs_to
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                try:
                    conn.request("POST", self.path, body=body, headers=headers)
                    reply = conn.getresponse()
                    data = reply.read()
                    self.send_response(reply.status)
                    for name, value in reply.getheaders():
                        if name.lower() not in (
                            "connection",
                            "transfer-encoding",
                            "date",
                            "server",
                        ):
                            self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    conn.close()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class Ungated:
    """A service that answers without Scrimmage: a worker started another way."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or 0))
                self.send_response(status)
                for name, value in {**headers, "content-length": str(len(body))}.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class EagerPort:
    """A port that accepts before the app listens, as Docker's port proxy does: for the first
    `reset_for` seconds it takes each connection and resets it (SO_LINGER 0), then it pipes
    connections to `behind`. half: it replies with the start of an HTTP reply and resets."""

    def __init__(
        self, behind: int | None, reset_for: float, *, half: bool = False, cut_runs: bool = False
    ) -> None:
        self.behind, self.half, self.cut_runs = behind, half, cut_runs
        self.cut = 0  # run requests it reset (cut_runs: it pipes the probe and setup only)
        self.opens = time.monotonic() + reset_for
        self.accepted = 0
        self.stopped = False
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while not self.stopped:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            if self.cut_runs and self.behind is not None and self.is_run(client):
                self.cut += 1
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                client.close()
                continue
            if self.behind is None or time.monotonic() < self.opens:
                if self.half:
                    client.recv(65536)
                    client.sendall(b"HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\nabc")
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                client.close()
                continue
            threading.Thread(target=self.pipe, args=(client,), daemon=True).start()

    @staticmethod
    def is_run(client: socket.socket) -> bool:
        """Whether the request on this connection is a run's own, read without consuming it."""
        client.settimeout(5)
        try:
            head = client.recv(65536, socket.MSG_PEEK)
        except OSError:
            return False
        finally:
            client.settimeout(None)
        return b"x-scrimmage-run: run-" in head.lower()

    def pipe(self, client: socket.socket) -> None:
        with client, socket.create_connection(("127.0.0.1", self.behind or 0)) as server:

            def forward(source: socket.socket, sink: socket.socket) -> None:
                with contextlib.suppress(OSError):
                    while data := source.recv(65536):
                        sink.sendall(data)
                    sink.shutdown(socket.SHUT_WR)

            back = threading.Thread(target=forward, args=(server, client), daemon=True)
            back.start()
            forward(client, server)
            back.join(30)

    def close(self) -> None:
        self.stopped = True
        self.sock.close()
        self.thread.join(5)


class Bench:
    """A service started for scrimmage test, the runner in this process, the fake model."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        from conftest import ACME
        from fakes.openai_server import FakeOpenAI

        self.root = tmp_path / "acme-bot"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.token_file = tmp_path / "token"
        self.token_file.write_text(secrets.token_hex(32))
        self.token_file.chmod(0o600)
        self.control = f"http://127.0.0.1:{free_port()}"
        self.fake = FakeOpenAI(ACME)
        self.fake.start()
        self.monkeypatch, self.capsys = monkeypatch, capsys
        monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(self.token_file))
        monkeypatch.setenv("SCRIMMAGE_CONTROL_URL", self.control)
        self.services: list[Running] = []
        self.closers: list[Any] = []

    def start(
        self,
        *args: str,
        workers: int = 1,
        wait: bool = True,
        bin_dir: Path = BIN,
        app: str = "acme.service:app",
        bare: bool = False,
        **env: str,
    ) -> Running:
        base = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
        base.update(
            PATH=f"{bin_dir}{os.pathsep}{base.get('PATH', '')}",
            SCRIMMAGE_SERVICE="1",
            SCRIMMAGE_TOKEN_FILE=str(self.token_file),
            SCRIMMAGE_CONTROL_URL=self.control,
            SCRIMMAGE_TOOLS=TOOLS,
            SCRIMMAGE_ALLOW_IMPORTS="acme.",
            OPENAI_BASE_URL=self.fake.base_url,
            OPENAI_API_KEY="test",
        )
        base.update(env)
        flags = ["--workers", str(workers)] if workers > 1 else []
        env_for = {k: v for k, v in base.items() if v != ""}
        service = Running(self.root, env_for, [*flags, *args], app, bin_dir=bin_dir, bare=bare)
        service.wanted = workers
        self.services.append(service)
        if wait:
            assert service.ready.wait(60), service.text
        return service

    def test(
        self,
        port: int,
        path: str = "/chat",
        *,
        runs: int = 1,
        answer: str = "reply",
        grace: float = 0.2,
        wait: float = 10.0,  # ends early once the last batch comes: a slow box needs no more
        tests: str = "",
        json_out: bool = False,
        parallel: int = 1,
        timeout: int = 30,
    ) -> tuple[int, str]:
        from scrimmage_core.cli import main

        text = FILE.replace("PORT", str(port)).replace("PATH", path)
        text = text.replace("EXTRA", f"parallel = {parallel}")
        text = text.replace("TOOL_LIST", json.dumps(TOOLS.split(",")))
        text = text.replace("RUNS", str(runs)).replace("ANSWER", answer)
        text = text.replace("TIMEOUT", str(timeout)).replace("WAIT", str(wait))
        (self.root / "service.toml").write_text(text.replace("GRACE", str(grace)) + tests)
        self.capsys.readouterr()
        args = ["test", str(self.root / "service.toml"), *(["--json"] if json_out else [])]
        code = main(args)
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def summaries(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for path in (self.root / "scrimmage-runs").glob("*/*.jsonl"):
            lines = [json.loads(x) for x in path.read_text().splitlines()]
            found[path.stem] = lines[-1].get("summary") or {}
        return found

    def lines(self, name: str) -> list[dict[str, Any]]:
        path = next((self.root / "scrimmage-runs").glob(f"*/{name}.jsonl"))
        return [json.loads(x) for x in path.read_text().splitlines()]

    def close(self) -> None:
        for closer in self.closers:
            closer.close()
        for service in self.services:
            service.stop()
        self.fake.stop()


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Iterator[Bench]:
    made = Bench(tmp_path, monkeypatch, capsys)
    try:
        yield made
    finally:
        made.close()


@pytest.mark.design("service.runs_answered")
def test_a_service_test_answers_its_runs_and_keeps_the_setup_record(bench: Bench) -> None:
    service = bench.start()
    code, out = bench.test(service.port, runs=2)
    assert code == 0, out
    summaries = bench.summaries()
    setup = [name for name in summaries if name.startswith("setup-")]
    runs = [name for name in summaries if name.startswith("run-")]
    assert len(setup) == 1 and len(runs) == 2
    assert all(summaries[r]["outcome"] == "answered" for r in runs)


@pytest.mark.design("service.runs_answered")
def test_every_line_of_a_service_runs_setup_record_fits_the_record_schema(bench: Bench) -> None:
    import jsonschema

    import scrimmage_core

    body = json.loads((Path(scrimmage_core.__file__).parent / "schemas/record.v1.json").read_text())
    check = jsonschema.Draft202012Validator(body, format_checker=jsonschema.FormatChecker())
    service = bench.start()
    code, out = bench.test(service.port, runs=1)
    assert code == 0, out
    files = list((bench.root / "scrimmage-runs").glob("*/setup-*.jsonl"))
    assert len(files) == 1
    bad = [
        f"{line}: {error.message}"
        for line in files[0].read_text().splitlines()
        for error in check.iter_errors(json.loads(line))
    ]
    assert not bad, "\n".join(bad)


def a_service_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: str = "127.0.0.1:9"
) -> Any:
    """A runner for a service test, with a long grace and wait, and no service."""
    from scrimmage_core import config as settings
    from scrimmage_core import runner

    token = tmp_path / "token"
    token.write_text(secrets.token_hex(32))
    token.chmod(0o600)
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(token))
    monkeypatch.setenv("SCRIMMAGE_CONTROL_URL", f"http://127.0.0.1:{free_port()}")
    path = tmp_path / "service.toml"
    path.write_text(
        f'[agent]\nservice = "http://{address}/chat"\nbody = {{message = "{{question}}"}}\n'
        'answer = "reply"\nrecord_grace_s = 30\nrecord_wait_s = 30\n\n'
        '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n'
    )
    return runner.Runner(settings.load(path))


@pytest.mark.design("service.waits_to_listen")
def test_ctrl_c_ends_the_wait_for_a_service_to_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scrimmage_core import runner

    made = a_service_runner(tmp_path, monkeypatch)  # nothing listens at 127.0.0.1:9
    stopped: list[BaseException] = []
    said: list[str] = []

    def check() -> None:
        try:
            made.check_service()
        except BaseException as exc:  # what ended the wait
            stopped.append(exc)

    def waiting_line() -> bool:
        said.append(capsys.readouterr().err)
        return "waiting for 127.0.0.1:9 to listen" in "".join(said)

    waiting = threading.Thread(target=check)
    began = time.monotonic()
    try:
        waiting.start()
        eventually(waiting_line)
        made.stop_flag.append("SIGINT")
        made.cancel()  # as the runner's watcher does on Ctrl-C
        waiting.join(5)
        assert not waiting.is_alive() and time.monotonic() - began < 10
        assert isinstance(stopped[0], runner.Interrupted) and str(stopped[0]) == "SIGINT"
    finally:
        made.server.server.server_close()


@pytest.mark.design("service.waits_to_listen")
def test_an_address_that_cannot_be_reached_is_named_as_that(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scrimmage_core import service_client

    made = a_service_runner(tmp_path, monkeypatch, "no-such-host.invalid:8765")
    try:
        with pytest.raises(service_client.Setup) as setup:
            made.check_service()
        assert setup.value.code == 3 and "cannot reach the service's address" in str(setup.value)
        assert "took the probe's connection" not in str(setup.value)
    finally:
        made.server.server.server_close()


@pytest.mark.design("service.waits_to_listen")
def test_an_address_whose_connection_times_out_is_asked_again_and_named_as_that(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10.255.255.1 drops the connection, as a firewall would: each try waits 2 s."""
    from scrimmage_core import runner, service_client

    monkeypatch.setattr(runner, "READY_S", 3.0)
    made = a_service_runner(tmp_path, monkeypatch, "10.255.255.1:8765")
    began = time.monotonic()
    try:
        with pytest.raises(service_client.Setup) as setup:
            made.check_service()
    finally:
        made.server.server.server_close()
    assert setup.value.code == 3 and "timed out" in str(setup.value)
    assert "a firewall may drop it" in str(setup.value) and "no such host" not in str(setup.value)
    assert time.monotonic() - began < 10  # tries of 2 s, not one of 30


@pytest.mark.design("service.record_wait_bounds")
def test_a_record_wait_counts_from_the_reply_and_a_cancel_ends_every_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = a_service_runner(tmp_path, monkeypatch)
    try:
        for name in ("run-00000000000a", "run-00000000000b"):
            made.server.open_run(name, "baseline", {"run": name}, service=True)
            made.server.runs[name].claimed_by = "a worker"  # its last batch never comes
        # A reply long ago: its grace and wait are over, so the waiter waits no more.
        began = time.monotonic()
        made.wait_record("run-00000000000a", time.monotonic() - 100)
        assert time.monotonic() - began < 1.0
        # A wait of a minute, which a cancel, as Ctrl-C makes, ends at once.
        waiting = threading.Thread(
            target=made.wait_record, args=("run-00000000000b", time.monotonic())
        )
        waiting.start()
        made.cancel()
        waiting.join(5)
        assert not waiting.is_alive()
    finally:
        made.server.server.server_close()


@pytest.mark.design("service.waits_to_listen")
def test_a_test_started_with_its_service_waits_for_the_service_to_listen(bench: Bench) -> None:
    """The service and the test start together, as a script that backgrounds the service
    does: the probe meets a refused connection first, and asks again."""
    service = bench.start("--loop", "asyncio", workers=2, wait=False)
    code, out = bench.test(service.port)
    assert code == 0, out + service.text


@pytest.mark.design("service.waits_to_listen")
def test_a_service_that_never_listens_stops_the_test_naming_that(bench: Bench) -> None:
    from scrimmage_core import runner

    bench.monkeypatch.setattr(runner, "READY_S", 0.5)
    code, out = bench.test(free_port())
    assert code == 3 and "nothing listens at the service's address" in out, out
    assert "is still starting" in out and "a proxy dropped the header" not in out


@pytest.mark.design("service.waits_to_listen")
def test_a_port_that_resets_connections_until_the_app_listens_is_asked_again(bench: Bench) -> None:
    """Docker's published port accepts before the app inside listens, then resets."""
    service = bench.start()
    eager = EagerPort(service.port, reset_for=1.5)
    bench.closers.append(eager)
    code, out = bench.test(eager.port)
    assert code == 0, out
    assert eager.accepted > 2  # it was asked again, not stopped by the first reset


@pytest.mark.design("service.waits_to_listen")
def test_a_port_that_always_resets_stops_the_test_naming_it_after_the_wait(bench: Bench) -> None:
    from scrimmage_core import runner

    bench.monkeypatch.setattr(runner, "READY_S", 1.0)
    eager = EagerPort(None, reset_for=0)
    bench.closers.append(eager)
    began = time.monotonic()
    code, out = bench.test(eager.port)
    assert code == 3 and "took the probe's connection but gave no reply" in out, out
    assert "published port can accept a connection before the app inside listens" in out
    assert eager.accepted > 2 and time.monotonic() - began < 10


@pytest.mark.design("service.waits_to_listen")
def test_a_reset_after_part_of_a_reply_is_final_at_once(bench: Bench) -> None:
    from scrimmage_core import runner

    bench.monkeypatch.setattr(runner, "READY_S", 30.0)
    eager = EagerPort(None, reset_for=0, half=True)
    bench.closers.append(eager)
    code, out = bench.test(eager.port)
    assert code == 3 and "took the probe's connection but gave no reply" in out, out
    assert "published port" not in out and eager.accepted == 1


@pytest.mark.design("service.waits_to_listen")
def test_a_run_request_that_is_reset_is_not_sent_again(bench: Bench) -> None:
    """Only the probe is asked again. A run's POST may reach a live write tool, so a reset
    on it is one infrastructure failure, never a second request."""
    service = bench.start()
    eager = EagerPort(service.port, reset_for=0, cut_runs=True)
    bench.closers.append(eager)
    code, out = bench.test(eager.port, json_out=True)
    report = json.loads(out)
    assert eager.cut == 1, out  # one run connection, reset, and no retry of it
    assert code == 3 and report["status"] == "infra", out
    details = report["rows"][0]["run_details"]
    assert [d["outcome"] for d in details] == ["infrastructure error"], out


@pytest.mark.design("service.probe_proves_the_gate")
@pytest.mark.parametrize("path", ["/chat", "/echo"])  # /echo answers 200 to anything
def test_a_proxy_that_drops_the_header_stops_the_runner_before_any_test_request(
    bench: Bench, path: str
) -> None:
    service = bench.start()
    proxy = Forwarder(service.port, drop=True)
    bench.closers.append(proxy)
    code, out = bench.test(proxy.port, path=path)
    assert code == 3 and "x-scrimmage-run does not reach Scrimmage" in out
    assert len(proxy.seen) == 1 and proxy.seen[0].startswith("probe-")  # nothing after it


@pytest.mark.design("service.probe_signed")
def test_a_probe_reply_with_a_forged_signature_stops_the_runner(bench: Bench) -> None:
    """A server without the token that answers as a probe would: its report is not taken."""
    report = {"gates": ["asgi"], "tools": sorted(t.rpartition(":")[2] for t in TOOLS.split(","))}
    headers = {
        "content-type": "application/json",
        "x-scrimmage-probe": "report",
        "x-scrimmage-signature": "0" * 64,
    }
    forger = Ungated(200, headers, json.dumps(report).encode())
    bench.closers.append(forger)
    code, out = bench.test(forger.port)
    assert code == 3 and "x-scrimmage-run does not reach Scrimmage" in out


@pytest.mark.design("service.probe_plain_to_strangers")
def test_an_unsigned_probe_reaches_the_app_as_a_plain_request(bench: Bench) -> None:
    service = bench.start()
    status, reply = service.post(
        "/echo", headers={"x-scrimmage-probe": "1", "x-scrimmage-run": "probe-0123456789ab"}
    )
    assert status == 200 and not any(k.startswith("x-scrimmage") for k, _ in reply["headers"])


@pytest.mark.design("service.probe_signed")
def test_a_probe_signed_with_another_token_reaches_the_app_and_the_runner_stops(
    bench: Bench,
) -> None:
    service = bench.start()
    bench.token_file.write_text(secrets.token_hex(32))  # the runner's token is another
    code, out = bench.test(service.port)
    assert code == 3 and "its token differs" in out


def ungated_runs(
    bench: Bench, status: int, headers: dict[str, str], body: bytes
) -> tuple[int, str, Forwarder]:
    service = bench.start()
    other = Ungated(status, headers, body)
    proxy = Forwarder(service.port, runs_to=other.port)
    bench.closers += [proxy, other]
    code, out = bench.test(proxy.port, runs=3)
    return code, out, proxy


@pytest.mark.design("service.unclaimed_stops")
def test_a_reply_whose_plan_was_never_fetched_is_an_infrastructure_error_and_stops_the_test_with_exit_3(  # noqa: E501  # the design's name
    bench: Bench,
) -> None:
    reply = json.dumps({"reply": "shipped"}).encode()
    code, out, proxy = ungated_runs(bench, 200, {"content-type": "application/json"}, reply)
    assert code == 3 and "its plan was never claimed" in out
    assert sum(1 for run in proxy.seen if run.startswith("run-")) == 1  # no run after the first
    runs = [s for name, s in bench.summaries().items() if name.startswith("run-")]
    assert [s["outcome"] for s in runs if "outcome" in s] == ["infrastructure error"]


@pytest.mark.design("service.gate_shape_exact")
def test_a_reply_that_looks_like_the_gate_503_but_differs_stops_the_test(bench: Bench) -> None:
    headers = {"content-type": "application/json", "x-scrimmage-error": "claimed", "x-other": "1"}
    code, out, _ = ungated_runs(bench, 503, headers, b'{"error": "claimed"}')
    assert code == 3 and "its plan was never claimed" in out


@pytest.mark.design("service.claimed_or_not")
def test_an_answer_that_carries_x_scrimmage_error_but_was_never_claimed_stops_the_test(
    bench: Bench,
) -> None:
    """A good answer with the gate's header on it: whether the plan was claimed decides, not
    the header."""
    headers = {"content-type": "application/json", "x-scrimmage-error": "claimed"}
    reply = json.dumps({"reply": "shipped"}).encode()
    code, out, _ = ungated_runs(bench, 200, headers, reply)
    assert code == 3 and "its plan was never claimed" in out


@pytest.mark.design("service.claimed_or_not")
def test_an_app_that_sends_x_scrimmage_error_itself_cannot_make_a_result(bench: Bench) -> None:
    """Even the gate's exact 503 shape, sent by an app without Scrimmage, never makes a
    result: the run was never claimed, so it is an infrastructure error."""
    headers = {"content-type": "application/json", "x-scrimmage-error": "plan-unavailable"}
    code, _, proxy = ungated_runs(bench, 503, headers, b'{"error": "plan-unavailable"}')
    runs = [s for name, s in bench.summaries().items() if name.startswith("run-")]
    assert code == 3 and runs and all(s["outcome"] == "infrastructure error" for s in runs)
    assert sum(1 for run in proxy.seen if run.startswith("run-")) == 3  # the test went on


@pytest.mark.design("service.setup_main_path")
def test_a_setup_request_with_no_model_line_in_its_record_exits_3(bench: Bench) -> None:
    service = bench.start()
    code, out = bench.test(service.port, path="/lost")
    assert code == 3 and "context is lost on the agent's main path" in out
    assert "lost_context notice was seen" in out


@pytest.mark.design("service.setup_rejected")
def test_a_setup_request_the_service_rejects_stops_with_exit_2(bench: Bench) -> None:
    service = bench.start(TEST_TOKEN="tt-0123456789abcdef")  # the file sends no bearer token
    code, out = bench.test(service.port)
    assert code == 2 and "got 401" in out


@pytest.mark.design("service.setup_answer")
def test_a_setup_reply_without_the_answer_field_stops_with_exit_2(bench: Bench) -> None:
    service = bench.start()
    code, out = bench.test(service.port, answer="missing")
    assert code == 2 and "'missing'" in out and "got 200" in out


@pytest.mark.design("service.streamed")
@pytest.mark.parametrize("path", ["/stream", "/stream-json"])  # one event, a whole JSON doc
def test_a_streamed_reply_is_a_setup_error(bench: Bench, path: str) -> None:
    service = bench.start()
    code, out = bench.test(service.port, path=path)
    assert code == 2 and "text/event-stream" in out


@pytest.mark.design("service.tools_compared")
def test_a_tools_list_that_differs_from_the_file_stops_with_exit_2(bench: Bench) -> None:
    service = bench.start(SCRIMMAGE_TOOLS="acme.tools:get_order")
    code, out = bench.test(service.port)
    assert code == 2 and "the service gates the tools ['get_order']" in out
    assert f"SCRIMMAGE_TOOLS={TOOLS}" in out and "SCRIMMAGE_ALLOW_IMPORTS=acme." in out


@pytest.mark.design("service.runner_environment")
def test_the_token_and_control_url_come_from_the_runners_environment(bench: Bench) -> None:
    bench.token_file.write_text("short")
    assert bench.test(9)[0] == 2
    bench.token_file.write_text(secrets.token_hex(32))
    bench.token_file.chmod(0o644)
    code, out = bench.test(9)
    assert code == 2 and "must be 600" in out
    bench.token_file.chmod(0o600)
    bench.monkeypatch.setenv("SCRIMMAGE_CONTROL_URL", "http://0.0.0.0:7711")
    assert bench.test(9)[0] == 2
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        bench.monkeypatch.setenv(
            "SCRIMMAGE_CONTROL_URL", f"http://127.0.0.1:{holder.getsockname()[1]}"
        )
        code, out = bench.test(9)
        assert code == 3 and "cannot be bound" in out
    finally:
        holder.close()


@pytest.mark.design("service.run_timeout")
def test_run_timeout_ends_the_run_and_the_next_gated_call_in_the_service_fails(
    bench: Bench,
) -> None:
    service = bench.start()
    slow = (
        '\n[[test]]\nname = "slow"\nquestion = "slow: where is order 1234?"\nexpect = ["shipped"]\n'
    )
    code, out = bench.test(service.port, path="/slow", timeout=2, tests=slow)
    # A baseline whose only run timed out before any model call shows nothing: exit 2.
    assert code == 2 and "slow baseline: 1 timed out" in out, out
    ended = [
        s
        for name, s in bench.summaries().items()
        if name.startswith("run-") and bench.lines(name)[0]["test"] == "slow"
    ]
    assert [s["outcome"] for s in ended] == ["timed out"], ended
    # The service still holds that request: its next gated call, after the run's time, fails.
    status, reply = service.post("/go-on")
    assert status == 200 and reply["seen"] == ["ScrimmageTimeLimit"], reply


@pytest.mark.design("service.slot_frees_at_reply")
def test_the_parallel_slot_frees_at_the_reply_and_the_record_wait_runs_beside_the_next_run(
    bench: Bench,
) -> None:
    service = bench.start()
    proxy = Forwarder(service.port)
    bench.closers.append(proxy)
    other = (  # a second test, so a second row: its run goes beside the first row's wait
        '\n[[test]]\nname = "again"\nquestion = "Where is order 1234?"\nexpect = ["shipped"]\n'
        '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = "shipped, Oct 30"\n'
        '\n[[test.mock]]\ncall = "tool:get_stock"\nreturns = {sku = "SKU-42", count = 12}\n'
    )
    code, out = bench.test(proxy.port, runs=2, grace=2.0, tests=other)  # parallel = 1
    assert code == 0, out
    sent = [
        (bench.lines(header.partition(".")[0])[0]["test"], at)
        for header, at in zip(proxy.seen, proxy.times, strict=True)
        if header.startswith("run-")
    ]
    # The other row's run went at the first one's reply, not after its 2 s grace; a row's
    # own next run went only once its run before had settled, after that grace. A time is
    # what this checks: a run answers in well under a second here.
    assert len(sent) == 4 and sent[0][0] != sent[1][0], sent
    assert sent[1][1] - sent[0][1] < 1.5, sent
    first_again = next(at for test, at in sent[1:] if test == sent[0][0])
    assert first_again - sent[0][1] >= 2.0, sent
    summaries = [s for name, s in bench.summaries().items() if name.startswith("run-")]
    assert [s["outcome"] for s in summaries] == ["answered"] * 4


@pytest.mark.design("service.grace_keeps_late_calls")
def test_a_slow_background_task_lands_in_the_record_within_the_grace(bench: Bench) -> None:
    service = bench.start()
    code, out = bench.test(service.port, path="/background", grace=2.0)
    assert code == 0, out
    # The audit check runs half a second after the reply, inside the two-second grace.
    runs = [name for name in bench.summaries() if name.startswith("run-")]
    late = [x for x in bench.lines(runs[0]) if x.get("call") == "tool:check_inventory"]
    assert [x["action"] for x in late] == ["real"]


@pytest.mark.design("service.missing_last_batch")
def test_a_missing_last_batch_is_an_infrastructure_error_after_the_wait(bench: Bench) -> None:
    service = bench.start()
    dying = (
        '\n[[test]]\nname = "dies"\nquestion = "please die, order 1234?"\nexpect = ["shipped"]\n'
        '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = "shipped, Oct 30"\n'
        '\n[[test.mock]]\ncall = "tool:get_stock"\nreturns = {sku = "SKU-42", count = 12}\n'
    )
    # The default wait: the healthy row's last batch must come within it even on a loaded
    # box, and the dying row's wait is the bound this test pays for.
    code, out = bench.test(service.port, path="/dies", tests=dying)
    assert code == 3, out
    summaries = bench.summaries()
    died = [
        s
        for name, s in summaries.items()
        if name.startswith("run-") and bench.lines(name)[0]["test"] == "dies"
    ]
    assert [(s["outcome"], s["incomplete"]) for s in died] == [("infrastructure error", True)]
    service.proc.wait(30)  # the worker ended itself; the stop below finds it gone


@pytest.mark.design("service.full_cap_goes_on")
def test_a_full_cap_gives_infrastructure_errors_and_the_test_does_not_stop(bench: Bench) -> None:
    """A control server so slow that the service's eight control slots stay full past 250 ms:
    a ninth claim gets the gate's 503, its run is an infrastructure error, every row still
    has all its runs, and the test is not stopped for it."""
    service = bench.start()
    real = ControlServer.handle

    def slow(self: ControlServer, method: str, path: str, headers: Any, body: bytes) -> Any:
        if path == "/v1/claims" and b'"run-' in body:
            threading.Event().wait(1.0)  # a control server under load holds each claim
        return real(self, method, path, headers, body)

    bench.monkeypatch.setattr(ControlServer, "handle", slow)
    # Nine rows, one run each, as a row's own runs go one after another.
    more = "".join(
        f'\n[[test]]\nname = "order-status-{n}"\nquestion = "Where is order 1234?"\n'
        'expect = ["shipped"]\n'
        '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = "shipped, Oct 30"\n'
        '\n[[test.mock]]\ncall = "tool:get_stock"\nreturns = {sku = "SKU-42", count = 12}\n'
        for n in range(8)
    )
    code, out = bench.test(service.port, tests=more, parallel=9)
    runs = [s for name, s in bench.summaries().items() if name.startswith("run-")]
    assert code == 3 and len(runs) == 9, out
    assert any(s["outcome"] == "infrastructure error" for s in runs)
    assert "its plan was never claimed" not in out


@pytest.mark.design("service.refused_row")
def test_a_refused_call_stops_its_row_and_the_service_keeps_serving(bench: Bench) -> None:
    service = bench.start()
    unmocked = (
        '\n[[test]]\nname = "no-stock-mock"\nquestion = "Where is order 1234?"\n'
        'expect = ["shipped"]\n'
    )
    unmocked += '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = "shipped, Oct 30"\n'
    code, out = bench.test(service.port, runs=4, tests=unmocked, parallel=2)
    assert code == 2, out
    refused = [s for s in bench.summaries().values() if s.get("outcome") == "refused call"]
    # A row's runs go one after another, each once the one before is settled, so the
    # refusal stops its row before the next run is sent.
    assert len(refused) == 1, refused
    assert service.post("/echo")[0] == 200  # a plain request is served as before


@pytest.mark.design("service.control_load_reported")
def test_control_load_reaches_the_summary_the_report_and_json(bench: Bench) -> None:
    from test_schemas import validator

    service = bench.start()
    code, out = bench.test(service.port, json_out=True)
    doc = json.loads(out[: out.rindex("}") + 1])
    assert code == 0 and not list(validator("cli").iter_errors(doc))
    row = doc["rows"][0]
    run = row["run_details"][0]
    assert run["control_busy"] == 0 and isinstance(run["control_p99_ms"], int | float)
    assert row["control_busy"] == 0 and row["control_p99_ms"] == run["control_p99_ms"]
    summary = bench.summaries()[run["run"]]
    assert summary["control_busy"] == 0 and summary["control_p99_ms"] == run["control_p99_ms"]
    # A row whose runs had a busy count gets a line under the table. The load is set on the
    # result, as a busy process would report it, so the line's text is what is checked.
    from scrimmage_core import runner

    served = runner.Runner.served_result

    def busy(self: Any, *args: Any) -> Any:
        result = served(self, *args)
        result.control_busy, result.control_p99_ms = 3, 42.5
        return result

    bench.monkeypatch.setattr(runner.Runner, "served_result", busy)
    code, out = bench.test(service.port)
    assert code == 0 and "order-status baseline: control: 3 busy, p99 42.5 ms" in out, out
