"""scrimmage check --service, and a service clock outside the signed window.

Real uvicorn processes under scrimmage-run, as test_service.py starts them. Where a test needs
a service clock that is off, it starts a small FastAPI app of its own (LOOSE) that logs every
request it receives and moves the gate's wall clock when told to.
"""

import asyncio
import contextlib
import dataclasses
import json
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from ports import free_port
from scrimmage import asgi_gate, cli, config, protocol, service_client, versions
from service_harness import Service, drive
from test_cli_json import validate
from test_service import FILE, TOOLS, Bench, bench

pytestmark = pytest.mark.slow
__all__ = ["bench"]  # a fixture, used by name

LOOSE = '''
"""A service that takes any JSON body at /chat, whatever its document says."""

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request

from acme.agent import answer

LOG = os.environ["REQUEST_LOG"]
SHIFT = float(os.environ.get("GATE_CLOCK_SHIFT", "0"))

if SHIFT:
    from scrimmage import asgi_gate

    class Shifted:
        """The gate's view of the time module, with the wall clock moved."""

        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return time.time() + SHIFT

    asgi_gate.time = Shifted()

JUMP = float(os.environ.get("CLAIM_CLOCK_JUMP", "0"))

if JUMP:
    from scrimmage import client as control_client

    claims = []

    class Jumped:
        """The SDK client's clock, which jumps by JUMP once the setup request's claim is done:
        the probe, the ping and the setup request pass, and every later request is off."""

        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return time.time() + (JUMP if len(claims) > 1 else 0)

    control_client.time = Jumped()
    first_claim = control_client.ControlClient.claim

    def claim(self, run, proc, *, held=False):
        claims.append(run)
        with open(LOG, "a") as handle:  # each claim the gate made, beside the app's requests
            handle.write(json.dumps({"method": "CLAIM", "path": run}) + "\\n")
        return first_claim(self, run, proc, held=held)

    control_client.ControlClient.claim = claim


async def chat(request: Request) -> dict[str, str]:
    body = await request.json()
    reply = await asyncio.get_running_loop().run_in_executor(
        None, answer, str(body.get("message") or "hello")
    )
    return {"reply": reply}


inner = FastAPI(docs_url=None, redoc_url=None)
inner.post("/chat")(chat)


class Logged:
    """Logs each request after it is served."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks = []

        async def spy():
            message = await receive()
            chunks.append(message.get("body", b""))
            return message

        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        try:
            return await self.app(scope, spy, send)
        finally:
            line = {
                "method": scope["method"],
                "path": scope["path"],
                "headers": headers,
                "body": b"".join(chunks).decode(),
            }
            with open(LOG, "a") as handle:
                handle.write(json.dumps(line) + "\\n")


app = Logged(inner)
'''

LABELS = ("service", "version", "python", "tools", "control", "notices")
TOKEN = "bearer-token-for-the-runs"
CLOCK_OFF = (
    "the service's clock and this machine's differ by more than 60 s, so every signed request "
    "is refused; set both from the same NTP source"
)


class Lab:
    """A bench, the loose service's files, and the file scrimmage reads."""

    def __init__(self, bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bench = bench
        self.root = bench.root
        self.log = self.root / "requests.jsonl"
        (self.root / "loose.py").write_text(LOOSE)
        monkeypatch.setenv("FILE_TOKEN", TOKEN)  # what the file's header is expanded from

    def start(self, **env: str) -> int:
        """The loose service. Its port."""
        more = dict(env, REQUEST_LOG=str(self.log))
        return self.bench.start(app="loose:app", **cast(dict[str, Any], more)).port

    def file(
        self,
        port: int,
        path: str = "/chat",
        body: str = 'body    = {message = "{question}", session = "{session}"}',
        extra: str = 'headers = {Authorization = "Bearer ${FILE_TOKEN}"}',
    ) -> Path:
        text = FILE.replace("PORT", str(port)).replace("PATH", path)
        text = text.replace("TOOL_LIST", json.dumps(TOOLS.split(",")))
        text = text.replace("RUNS", "1").replace("ANSWER", "reply").replace("TIMEOUT", "30")
        text = text.replace("WAIT", "10").replace("GRACE", "0.2").replace("EXTRA", extra)
        text = text.replace('body    = {message = "{question}", session = "{session}"}', body)
        target = self.root / "service.toml"
        target.write_text(text)
        return target

    def check(self, path: Path, *flags: str) -> tuple[int, str]:
        self.bench.capsys.readouterr()
        code = cli.main(["check", str(path), "--service", *flags])
        out = self.bench.capsys.readouterr()
        return code, out.out + out.err

    def requests(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def lab(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return Lab(bench, monkeypatch)


def line_of(out: str, label: str) -> str:
    """The text of the report line with this label (the line's first row only)."""
    found = [x for x in out.splitlines() if x.startswith(f"{label:<12}")]
    assert len(found) == 1, out
    return found[0][12:]


# --- check --service -----------------------------------------------------------------------


def test_check_service_prints_the_startup_report(lab: Lab) -> None:
    service = lab.bench.start()  # the example service: real FastAPI, real uvicorn
    code, out = lab.check(lab.file(service.port))
    assert code == 0, out
    order = [x[:12].strip() for x in out.splitlines() if x[:12].strip() in LABELS]
    assert order == list(LABELS), out
    bench = lab.bench
    assert re.fullmatch(
        rf"POST http://127\.0\.0\.1:{service.port}/chat   gated \(.*asgi.*\), pid \d+",
        line_of(out, "service"),
    ), out
    ours = versions.installed()
    assert line_of(out, "version") == f"scrimmage {ours} in the service and here"
    assert line_of(out, "python").startswith(sys.version.split()[0]) and " at /" in out
    names = "check_inventory, get_order, get_stock, refund_order"
    assert line_of(out, "tools") == f"{names}   (match the file)"
    assert re.fullmatch(
        rf"{re.escape(bench.control)} reached, no TLS, on this machine, this runner, \d+ ms",
        line_of(out, "control"),
    ), out
    assert line_of(out, "notices") == "none"
    assert len(out.splitlines()) == 1 + len(LABELS), out  # "is valid", these six, no more


def test_check_service_prints_one_version_line(lab: Lab) -> None:
    service = lab.bench.start()
    theirs = versions.installed()
    code, out = lab.check(lab.file(service.port))
    assert code == 0, out
    assert line_of(out, "version") == f"scrimmage {theirs} in the service and here"
    # A runner of another version: the one line names both, and nothing else says so.
    lab.bench.monkeypatch.setattr(versions, "installed", lambda: "9.9.9")
    code, out = lab.check(lab.file(service.port))
    assert code == 2, out
    said = (
        f"the service runs scrimmage {theirs} and this runner is 9.9.9; "
        "install scrimmage 9.9.9 in the service"
    )
    assert line_of(out, "version") == said, out
    assert out.count(f"scrimmage {theirs} and this runner") == 1, out  # said once, on its line
    assert "versions" not in out, out


def test_check_without_service_still_reads_the_file_only(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.5)
    try:
        path = lab.file(listener.getsockname()[1])
        capsys.readouterr()
        assert cli.main(["check", str(path)]) == 0
        assert "is valid" in capsys.readouterr().out
        with pytest.raises(TimeoutError):  # nothing connected: not a probe
            listener.accept()
    finally:
        listener.close()
    assert not (lab.root / "scrimmage-runs").exists()


def test_check_service_spends_nothing_and_sends_no_run(lab: Lab) -> None:
    port = lab.start()
    code, out = lab.check(lab.file(port))
    assert code == 0, out
    assert lab.bench.fake.requests == []  # no model call
    assert not (lab.root / "scrimmage-runs").exists()  # no records folder, no setup record
    assert lab.requests() == []  # the probe is the gate's, and nothing reached the app


def test_check_service_json_matches_the_schema(lab: Lab) -> None:
    port = lab.start()
    path = lab.file(port)
    lab.bench.capsys.readouterr()
    code = cli.main(["check", str(path), "--service", "--json"])
    doc = json.loads(lab.bench.capsys.readouterr().out)
    assert code == 0 and doc["exit"] == 0 and doc["status"] == "valid", json.dumps(doc)
    validate(doc)
    service = doc["service"]
    assert service["tools_match"] is True and service["sdk"] == service["runner_sdk"]
    assert sorted(service) == [
        "control",
        "executable",
        "gates",
        "notices",
        "pid",
        "python",
        "runner_sdk",
        "sdk",
        "tools",
        "tools_match",
        "url",
    ], service
    assert doc["warnings"] == [], doc


def test_check_service_prints_every_line_when_one_is_wrong(lab: Lab) -> None:
    port = lab.start(SCRIMMAGE_TOOLS="acme.tools:get_order")
    code, out = lab.check(lab.file(port))
    assert code == 2, out
    assert "the service gates the tools ['get_order']" in line_of(out, "tools")
    for label in ("service", "version", "python", "control", "notices"):
        line_of(out, label)  # still printed, once


# --- the clock -----------------------------------------------------------------------------


def test_strict_is_not_an_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "scrimmage.toml"
    for flags in (["--service", "--strict"], ["--strict"]):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["check", str(path), *flags])
        assert stopped.value.code == 2
        assert "unrecognized arguments: --strict" in capsys.readouterr().err


def test_a_probe_outside_the_window_gets_a_signed_clock_reply_with_no_time_and_exit_3_naming_ntp(
    lab: Lab,
) -> None:
    port = lab.start(GATE_CLOCK_SHIFT="65")
    path = lab.file(port)
    token = lab.bench.token_file.read_text().strip()
    probed = service_client.probe(config.load(path).agent, token)
    reply = probed.reply
    assert (reply.status, reply.headers["x-scrimmage-probe"]) == (409, "clock"), reply
    assert json.loads(reply.body) == {"error": "clock"}, reply.body  # signed, and no time
    assert probed.clock_refused and probed.report is None
    code, out = lab.bench.test(port)
    assert code == 3, out
    assert CLOCK_OFF in out, out
    assert "x-scrimmage-run does not reach Scrimmage" not in out  # not read as an ungated service
    assert lab.requests() == []  # the app never saw the stale probe
    assert lab.bench.fake.requests == []
    code, shown = lab.check(path)
    assert code == 3 and CLOCK_OFF in shown, shown
    # A service behind this machine gets the same words: no gap is measured.
    behind = lab.start(GATE_CLOCK_SHIFT="-70")
    code, out = lab.bench.test(behind)
    assert code == 3 and CLOCK_OFF in out, out


class Shifted:
    """The gate's view of the time module, with the wall clock moved."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def time(self) -> float:
        return time.time() + self.seconds


async def plain_app(scope: Any, receive: Any, send: Any) -> None:
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"x-app", b"1")]})
    await send({"type": "http.response.body", "body": b"app"})


def stale_probe(svc: Service, age: float = 65.0) -> tuple[list[tuple[bytes, bytes]], str]:
    signed = protocol.request_headers(svc.token, "POST", "/chat", b"", time.time() - age)
    headers = [
        (b"x-scrimmage-run", b"probe-0123456789ab"),
        (b"x-scrimmage-probe", b"1"),
        *[(k.lower().encode(), v.encode()) for k, v in signed.items()],
    ]
    return headers, signed[protocol.SIGNATURE]


def test_a_replayed_stale_probe_is_answered_as_documented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = Service(tmp_path / "records")
    try:
        headers, request_signature = stale_probe(svc)
        entry = asgi_gate.Entry(plain_app)

        def send() -> Any:
            return asyncio.run(drive(entry, headers=headers, body=b""))

        def is_clock_reply(reply: Any) -> None:
            assert reply.status == 409 and reply.headers["x-scrimmage-probe"] == "clock"
            assert json.loads(reply.body) == {"error": "clock"}  # the service's clock is not in it
            signature = protocol.sign_reply(svc.token, request_signature, 409, reply.body)
            assert protocol.same(reply.headers[protocol.SIGNATURE.lower()], signature)

        is_clock_reply(send())  # a stale, genuine probe: the signed clock reply
        replayed = send()  # the same worker, within 120 s, has the nonce: a plain request
        assert replayed.status == 200 and replayed.body == b"app"
        assert "x-scrimmage-probe" not in replayed.headers
        # Another worker keeps its own nonces, so the replay gets the signed reply there too:
        # the leak the docs state.
        first = svc.gate
        other = dataclasses.replace(first, nonces=protocol.NonceStore())
        asgi_gate.GATE = other
        try:
            is_clock_reply(send())
        finally:
            asgi_gate.GATE = first
        # The same worker after it has forgotten the nonce (after 120 s) answers it again.
        monkeypatch.setattr(asgi_gate, "time", Shifted(130))
        is_clock_reply(send())
    finally:
        svc.close()


def test_a_clock_jump_after_the_probe_stops_the_test_at_the_first_run(lab: Lab) -> None:
    port = lab.start(CLAIM_CLOCK_JUMP="300")
    code, out = lab.bench.test(port, runs=2)
    assert code == 3, out  # the setup request passed; the first run's claim was refused
    (said,) = [line for line in out.splitlines() if CLOCK_OFF in line]
    assert re.fullmatch(rf"scrimmage: run-[0-9a-f]{{12}} was refused: {re.escape(CLOCK_OFF)}", said)
    assert "503 no-such-run" not in out and "503 clock" not in out, out
    seen = [(r["method"], r["path"]) for r in lab.requests()]
    claims = [path for method, path in seen if method == "CLAIM"]
    # The setup request's claim, then the first run's, refused; the second run never reached
    # the gate, and only the setup request reached the app.
    assert len(claims) == 2 and claims[0].startswith("setup-"), seen
    assert [method for method, _ in seen].count("POST") == 1, seen


# --- a probe reply that never ends ---------------------------------------------------------


class Flood(BaseHTTPRequestHandler):
    """Answers the probe with 400 MB."""

    def do_POST(self) -> None:
        size = 400 * 1024 * 1024
        self.send_response(200)
        self.send_header("content-length", str(size))
        self.end_headers()
        chunk = b"\0" * 65536
        with contextlib.suppress(OSError):
            for _ in range(size // len(chunk)):
                self.wfile.write(chunk)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class Drip(BaseHTTPRequestHandler):
    """Sends its reply one byte every 0.2 s, about 20 s in all, so no single read times out."""

    def do_POST(self) -> None:
        reply = b"HTTP/1.1 200 OK\r\ncontent-length: 60\r\n\r\n"
        if self.path == "/headers":  # the headers themselves are dripped
            reply += b"x" * 60
        else:
            self.wfile.write(reply)
            reply = b"x" * 60
        with contextlib.suppress(OSError):
            for byte in reply:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.2)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def served(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def test_a_probe_reply_over_1_mib_is_cut_with_exit_3(lab: Lab) -> None:
    server, port = served(Flood)
    try:
        began = time.monotonic()
        code, out = lab.bench.test(port)
        elapsed = time.monotonic() - began
    finally:
        server.shutdown()
        server.server_close()
    assert code == 3, out
    assert (
        "the service at POST /chat answered the probe with more than 1 MiB; Scrimmage's "
        "startup report is a few KB, so something other than Scrimmage answered"
    ) in out, out
    assert elapsed < 10, elapsed


@pytest.mark.parametrize("path", ["/headers", "/body"])
def test_a_probe_reply_that_drips_is_cut_at_the_whole_request_deadline(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(service_client, "PROBE_S", 1.5)  # 30 s in use; each read waits 30 s
    server, port = served(Drip)
    try:
        began = time.monotonic()
        code, out = lab.bench.test(port, path)
        elapsed = time.monotonic() - began
    finally:
        server.shutdown()
        server.server_close()
    assert code == 3, out
    assert (
        f"the service at POST {path} did not finish its reply to the probe within 1.5 s"
    ) in out, out
    assert elapsed < 6, elapsed


@pytest.fixture
def empty_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """TMPDIR is an empty folder, so anything a command leaves behind shows."""
    folder = tmp_path / "scratch"
    folder.mkdir()
    monkeypatch.setenv("TMPDIR", str(folder))
    monkeypatch.setattr("tempfile.tempdir", str(folder))
    return folder


def test_check_service_leaves_nothing_in_the_temp_folder(lab: Lab, empty_tmp: Path) -> None:
    port = lab.start()
    code, out = lab.check(lab.file(port))
    assert code == 0, out
    assert list(empty_tmp.iterdir()) == []


@pytest.mark.parametrize("unset", ["SCRIMMAGE_TOKEN_FILE", "SCRIMMAGE_CONTROL_URL", "held"])
def test_check_service_that_stops_before_it_probes_leaves_nothing_behind(
    lab: Lab, empty_tmp: Path, monkeypatch: pytest.MonkeyPatch, unset: str
) -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    if unset == "held":  # the control address is taken by another process
        monkeypatch.setenv("SCRIMMAGE_CONTROL_URL", f"http://127.0.0.1:{holder.getsockname()[1]}")
    else:
        monkeypatch.delenv(unset)
    try:
        code, out = lab.check(lab.file(free_port()))
    finally:
        holder.close()
    assert code in (2, 3), out
    assert list(empty_tmp.iterdir()) == []
