"""Real uvicorn processes under scrimmage-run: each worker is a service process, not a child.

Each test starts its own service, waits for the lines it prints, and stops it; every pid it
started, the supervisor and its workers, is checked gone after.
"""

import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import EXAMPLE
from scrimmage_core import protocol
from scrimmage_core.config import Mock, ModelHost
from scrimmage_core.control import ControlServer
from scrimmage_core.plan import Plan
from scrimmage_core.records import RecordStore

pytestmark = pytest.mark.slow
BIN = Path(sys.executable).parent
TOOLS = (
    "acme.tools:get_order,acme.tools:check_inventory,acme.tools:refund_order,acme.tools:get_stock"
)
ACTIVE = re.compile(r"scrimmage: active in service mode \(pid (\d+)\)")
READY = "Application startup complete"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Running:
    """One service: scrimmage-run uvicorn acme.service:app, its stderr read as it comes."""

    def __init__(self, root: Path, env: dict[str, str], args: list[str]) -> None:
        self.port = free_port()
        command = [str(BIN / "scrimmage-run"), str(BIN / "uvicorn"), "acme.service:app"]
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

    def start(self, *args: str, workers: int = 1, wait: bool = True, **env: str) -> Running:
        flags = ["--workers", str(workers)] if workers > 1 else []
        service = Running(self.root, self.env(**env), [*flags, *args])
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
    assert "has mode 644; it must be 600" in lab.ended(service)


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
