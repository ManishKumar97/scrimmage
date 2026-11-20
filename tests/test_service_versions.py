"""The version rule between a service's SDK and the runner, and the service's ping.

A service in another venv has its own scrimmage, built here at the version a test names,
so the SDK in the service is a real install and not a patched number. The runner is this
process; where a test needs a runner of another version, it patches the one function both
the runner and its control server read the version through.
"""

import asyncio
import base64
import contextlib
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ports import free_port
from scrimmage_core import asgi_gate, control, protocol, service_client, tls, versions
from scrimmage_core import runner as running
from scrimmage_core.cert import make_certificates
from scrimmage_core.client import PING_TIMEOUT_S, ControlClient
from scrimmage_core.control import ControlServer
from scrimmage_core.plan import Plan
from scrimmage_core.records import RecordStore
from service_harness import Service, drive
from test_service import BIN, Bench, bench

pytestmark = pytest.mark.slow
__all__ = ["bench"]  # a fixture, used by name

ROOT = Path(__file__).parents[1]
RUN = "run-0123456789ab"
HOST = "127.0.0.1"


# --- SDKs of other versions, built as the runner would meet them ---------------------------


def uv(*args: str, cwd: Path) -> None:
    done = subprocess.run(["uv", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr


@pytest.fixture(scope="session")
def sdk_venv(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Path]:
    """The bin folder of a venv that runs the example service with scrimmage at the given
    version: the package built with uv from a copy whose version was set."""
    made: dict[str, Path] = {}

    def build(version: str) -> Path:
        if version in made:
            return made[version]
        folder = tmp_path_factory.mktemp("sdk-" + "".join(c for c in version if c.isalnum()))
        copy = folder / "core"
        shutil.copytree(
            ROOT / "packages" / "scrimmage",
            copy,
            ignore=shutil.ignore_patterns("__pycache__", "dist", "*.egg-info"),
        )
        pyproject = copy / "pyproject.toml"
        text, found = re.subn(
            r'^version = "[^"]+"$',
            f'version = "{version}"',
            pyproject.read_text(),
            count=1,
            flags=re.M,
        )
        assert found == 1
        pyproject.write_text(text)
        uv("build", "--wheel", "--out-dir", str(folder / "dist"), str(copy), cwd=folder)
        wheel = next((folder / "dist").glob("*.whl"))
        uv("venv", "--no-project", "--python", sys.executable, "venv", cwd=folder)
        python = folder / "venv" / "bin" / "python"
        uv(
            "pip",
            "install",
            "--python",
            str(python),
            str(wheel),
            "openai>=3.46",
            "fastapi==0.162.5",
            "uvicorn==0.54.0",
            cwd=folder,
        )
        made[version] = python.parent
        return python.parent

    return build


@pytest.fixture
def runner_is(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """This process, the runner and its control server, reads its version as given."""

    def patch(version: str) -> None:
        monkeypatch.setattr(versions, "installed", lambda: version)

    return patch


def against(
    bench: Bench, bin_dir: Path | None = None, *, proxy: Any = None, control_url: str = ""
) -> tuple[int, str]:
    """A service in the given venv (this one if none), and scrimmage test against it."""
    service = bench.start(
        bin_dir=bin_dir or BIN, SCRIMMAGE_CONTROL_URL=control_url or bench.control
    )
    port = service.port
    if proxy is not None:
        made = proxy(service.port)
        bench.closers.append(made)
        port = made.port
    return bench.test(port)


def sent_nothing(bench: Bench) -> None:
    """No setup request and no run was sent: the test stopped at the probe."""
    names = [p.stem for p in (bench.root / "scrimmage-runs").glob("*/*.jsonl")]
    assert names == [], names
    assert bench.fake.requests == []


@pytest.mark.design("service.version_same")
@pytest.mark.parametrize(
    ("sdk", "runner"),
    [("0.4.1", "0.4.0"), ("1.4.2", "0.4.0"), ("0.1.0.dev3", "")],
    ids=["patch", "major", "dev"],
)
def test_another_sdk_version_stops_at_the_probe_naming_both(
    bench: Bench,
    sdk_venv: Callable[[str], Path],
    runner_is: Callable[[str], None],
    sdk: str,
    runner: str,
) -> None:
    if runner:
        runner_is(runner)
    ours = versions.installed()
    code, out = against(bench, sdk_venv(sdk))
    assert code == 2, out
    said = f"the service runs scrimmage {sdk} and this runner is {ours}; install scrimmage {ours}"
    assert said + " in the service" in out, out
    sent_nothing(bench)


@pytest.mark.design("service.version_same")
def test_the_same_version_passes_with_nothing_said(bench: Bench) -> None:
    code, out = against(bench)  # the service runs this venv's scrimmage, as the runner does
    assert code == 0, out
    assert "the service runs scrimmage" not in out, out
    assert "warning: " not in out, out
    # The setup request and the run each claimed their plan, with the SDK's version in it.
    assert "claims received 2" in out, out


# --- a report that says less than a version we can compare ---------------------------------


class Rewriter:
    """A proxy in front of the service that answers the probe with its report changed and
    signed again with the token, as a service of another SDK would have answered."""

    def __init__(self, service_port: int, token: str, edit: Callable[[dict[str, Any]], None]):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                headers = {
                    k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")
                }
                conn = http.client.HTTPConnection(HOST, service_port, timeout=60)
                try:
                    conn.request("POST", self.path, body=body, headers=headers)
                    reply = conn.getresponse()
                    data = reply.read()
                    named = {
                        k: v
                        for k, v in reply.getheaders()
                        if k.lower()
                        not in (
                            "connection",
                            "transfer-encoding",
                            "date",
                            "server",
                            "content-length",
                        )
                    }
                    if reply.getheader("x-scrimmage-probe") == "report":
                        document = json.loads(data)
                        edit(document)
                        data = json.dumps(document).encode()
                        named[protocol.SIGNATURE] = protocol.sign_reply(
                            token, self.headers[protocol.SIGNATURE] or "", 200, data
                        )
                    self.send_response(reply.status)
                    for name, value in named.items():
                        self.send_header(name, value)
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    conn.close()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def rewritten(bench: Bench, edit: Callable[[dict[str, Any]], None]) -> Callable[[int], Rewriter]:
    token = bench.token_file.read_text()
    return lambda port: Rewriter(port, token, edit)


@pytest.mark.design("service.version_unknown")
@pytest.mark.parametrize(
    ("found", "named", "runner"),
    [
        pytest.param("unknown", "unknown", "", id="known-runner-unknown"),
        pytest.param("banana", "banana", "", id="known-runner-text"),
        pytest.param("", "no version", "", id="known-runner-empty"),
        pytest.param("unknown", "unknown", "unknown", id="unknown-runner-unknown"),
        pytest.param("", "no version", "unknown", id="unknown-runner-empty"),
    ],
)
def test_an_unknown_version_never_matches_even_itself(
    bench: Bench, runner_is: Callable[[str], None], found: str, named: str, runner: str
) -> None:
    """A runner with no package metadata reads `unknown`, and a service reporting `unknown`
    still does not match it."""
    if runner:
        runner_is(runner)
    ours = versions.installed()
    assert not versions.same(found)

    def edit(report: dict[str, Any]) -> None:
        report["sdk"] = found

    code, out = against(bench, proxy=rewritten(bench, edit))
    assert code == 2, out
    said = (
        f"the service runs scrimmage {named} and this runner has no package metadata; "
        "install scrimmage in the runner"
        if runner
        else f"the service runs scrimmage {named} and this runner is {ours}; "
        f"install scrimmage {ours} in the service"
    )
    assert said in out, out
    if runner:
        assert "install scrimmage unknown" not in out, out
    sent_nothing(bench)


# --- the claim: a worker added later is checked too ----------------------------------------


def signed(server: ControlServer, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
    headers = protocol.request_headers(server.token, "POST", path, body, time.time())
    status, reply = server.handle("POST", path, headers, body)
    return status, dict(reply)


@pytest.fixture
def control_server(tmp_path: Path) -> Iterator[ControlServer]:
    server = ControlServer(secrets.token_hex(32), RecordStore(tmp_path / "records"))
    server.open_run(RUN, "baseline", {"run": RUN}, service=True)
    try:
        yield server
    finally:
        server.server.server_close()


@pytest.mark.design("service.version_claim")
def test_a_claim_with_no_sdk_field_is_refused(control_server: ControlServer) -> None:
    ours = versions.installed()
    for sent in (
        {"run": RUN, "proc": {"pid": 1}},  # no sdk
        {"run": RUN, "proc": {"pid": 1}, "sdk": "unknown"},
        {"run": RUN, "proc": {"pid": 1}, "sdk": 7},
        {"run": RUN, "proc": {"pid": 1}, "sdk": "9.9.9"},
    ):
        status, reply = signed(control_server, "/v1/claims", json.dumps(sent).encode())
        assert (status, reply["error"]) == (409, "version"), sent
    assert control_server.runs[RUN].claimed_by is None  # none of them took the run
    # Positive control: the same claim with the runner's own version takes it.
    good = {"run": RUN, "proc": {"pid": 1}, "sdk": ours}
    status, reply = signed(control_server, "/v1/claims", json.dumps(good).encode())
    assert status == 200 and "plan" in reply


@pytest.mark.design("service.version_claim")
def test_a_worker_of_another_version_is_refused_at_its_first_claim_and_stops_the_test(
    bench: Bench, sdk_venv: Callable[[str], Path]
) -> None:
    """Two workers behind one port. The probe and the setup request reach the first, of this
    runner's version; the runs reach the second, of another dev release of the same 0.1.0."""
    from test_service import Forwarder

    good = bench.start()
    other = bench.start(bin_dir=sdk_venv("0.1.0.dev3"))
    proxy = Forwarder(good.port, runs_to=other.port)
    bench.closers.append(proxy)
    code, out = bench.test(proxy.port, runs=3)
    assert code == 2, out
    ours = versions.installed()
    said = f"was refused: the service runs scrimmage 0.1.0.dev3 and this runner is {ours}; "
    assert said + f"install scrimmage {ours} in the service, in every process of it" in out, out
    # The first run stopped the test: the other two were never sent.
    assert sum(1 for run in proxy.seen if run.startswith("run-")) == 1, proxy.seen
    assert "503 version" not in out  # the stop is the test's, not a run counted as an error


@pytest.mark.design("service.plan_it_cannot_run")
def test_a_service_given_a_plan_it_cannot_run_ends_the_run_as_an_infrastructure_error(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner of a later version sends a fault word the service's SDK does not build. The
    service runs nothing in its place: the run is an infrastructure error, not a result."""
    sent = Plan.to_json

    def later(plan: Plan) -> dict[str, object]:
        doc = sent(plan)
        if plan.run.startswith("run-"):  # the setup request's plan is left as it was
            doc["faults"] = [{"call": "tool:get_order", "do": "melt", "when": "always"}]
        return doc

    service = bench.start()
    monkeypatch.setattr(Plan, "to_json", later)
    code, out = bench.test(service.port, runs=1)
    assert code == 3, out
    runs = {name: s for name, s in bench.summaries().items() if name.startswith("run-")}
    assert [s["outcome"] for s in runs.values()] == ["infrastructure error"], runs
    (name,) = runs
    assert f"{name}: the service's gate answered 503 plan-unavailable" in out, out
    assert runs[name]["model_calls"] == 0, runs  # nothing ran in the fault's place
    assert "claims received 2" in out, out  # the run was claimed, then refused


# --- the ping ------------------------------------------------------------------------------


def probe_report(svc: Service) -> dict[str, Any]:
    """The startup report the gate answers a signed probe with, through the entry gate."""
    signed_headers = protocol.request_headers(svc.token, "POST", "/chat", b"", time.time())
    headers = [
        (b"x-scrimmage-run", b"probe-0123456789ab"),
        (b"x-scrimmage-probe", b"1"),
        *[(k.lower().encode(), v.encode()) for k, v in signed_headers.items()],
    ]

    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("a signed probe never reaches the app")

    reply = asyncio.run(drive(asgi_gate.Entry(app), headers=headers, body=b""))
    assert reply.status == 200, reply.body
    return dict(json.loads(reply.body))


@pytest.fixture
def services(tmp_path: Path) -> Iterator[list[Service]]:
    made: list[Service] = []
    yield made
    for service in made:
        service.close()


@pytest.mark.design("service.ping_report")
def test_the_ping_reports_reached_tls_rtt_and_this_runners_id(
    tmp_path: Path, services: list[Service]
) -> None:
    plain = Service(tmp_path / "plain")
    services.append(plain)
    control_object = probe_report(plain)["control"]
    assert control_object == {
        "reached": True,
        "tls": "none",
        "error": None,
        "detail": None,
        "rtt_ms": control_object["rtt_ms"],
        "runner_id": plain.server.runner_id,
        "url": plain.server.url,
    }
    assert 0 <= control_object["rtt_ms"] < PING_TIMEOUT_S * 1000
    assert len(plain.server.runner_id) == 16 and int(plain.server.runner_id, 16) >= 0
    plain.close()
    services.remove(plain)
    # Over TLS the same ping says the certificate was verified.
    files = make_certificates(tmp_path / "pki", ips=(HOST,), names=("control.test",))
    secure = Service(
        tmp_path / "tls",
        server_tls=tls.server_context(files.cert, files.key),
        client_tls=tls.client_context(files.ca),
    )
    services.append(secure)
    found = probe_report(secure)["control"]
    assert found["reached"] is True and found["tls"] == "verified", found
    assert found["runner_id"] == secure.server.runner_id and found["url"].startswith("https://")


@pytest.mark.design("service.ping_report")
def test_a_ping_that_cannot_verify_the_certificate_names_the_error_and_the_fix(
    tmp_path: Path, services: list[Service]
) -> None:
    ours = make_certificates(tmp_path / "ours", ips=(HOST,), names=("control.test",))
    theirs = make_certificates(tmp_path / "theirs", ips=(HOST,), names=("control.test",))
    service = Service(
        tmp_path / "svc",
        server_tls=tls.server_context(theirs.cert, theirs.key),
        client_tls=tls.client_context(ours.ca),
    )
    services.append(service)
    report = probe_report(service)
    found = report["control"]
    assert found["reached"] is False and found["tls"] == "SSLCertVerificationError", found
    assert "certificate verify failed" in found["detail"] and found["runner_id"] is None
    with pytest.raises(service_client.Setup) as stopped:
        service_client.check_control(report, service.server.runner_id, asked_s=30.0)
    assert stopped.value.code == 3
    message = str(stopped.value)
    assert f"the service cannot reach the control server at {found['url']}: " in message
    assert "certificate verify failed" in message and message.endswith("check SCRIMMAGE_CONTROL_CA")


@pytest.mark.design("service.ping_stops")
def test_a_ping_answered_by_another_runner_stops_the_test(bench: Bench, tmp_path: Path) -> None:
    """The service was told the address of a second control server that holds the same token:
    it answers the ping, as a runner, but it is not this one."""
    other = ControlServer(
        bench.token_file.read_text(),
        RecordStore(tmp_path / "other-records"),
        address=(HOST, free_port()),
    )
    other.start()
    try:
        code, out = against(bench, control_url=other.url)
    finally:
        other.stop()
    assert code == 3, out
    assert f"answered as runner {other.runner_id}, not as this runner (" in out
    assert "another runner holds the same token" in out
    sent_nothing(bench)


@pytest.mark.design("service.ping_stops")
def test_a_ping_that_fails_stops_the_test_with_exit_3_before_setup_naming_the_error(
    bench: Bench,
) -> None:
    bench.monkeypatch.setattr(running, "READY_S", 0.5)  # the ping is asked again for this long
    nobody = f"http://{HOST}:{free_port()}"  # no control server listens there
    code, out = against(bench, control_url=nobody)
    assert code == 3, out
    assert re.search(
        rf"the service cannot reach the control server at {re.escape(nobody)} "
        r"after [0-9.]+ s of asking: ",
        out,
    )
    assert "Connection refused" in out
    sent_nothing(bench)


@pytest.mark.design("service.ping_signed")
def test_the_ping_stores_nothing_and_is_signed(
    control_server: ControlServer, tmp_path: Path
) -> None:
    folder = tmp_path / "records"

    def tree() -> list[str]:
        return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))

    before, runs = tree(), dict(control_server.runs)
    # Unsigned, or signed with another token, it is refused and answers nothing of the runner.
    status, reply = control_server.handle("POST", "/v1/ping", {}, b"")
    assert (status, reply) == (401, {"error": "bad_signature"})
    forged = protocol.request_headers("x" * 64, "POST", "/v1/ping", b"", time.time())
    assert control_server.handle("POST", "/v1/ping", forged, b"")[0] == 401
    # Signed, it answers who the runner is, and a client checks the answer is signed for it.
    status, reply = signed(control_server, "/v1/ping", b"")
    assert (status, reply) == (200, {"ok": True, "runner_id": control_server.runner_id})
    client = ControlClient(control_server.url, control_server.token)
    control_server.start()
    try:
        found = client.ping()
    finally:
        control_server.stop()
    assert found["reached"] is True and found["runner_id"] == control_server.runner_id
    assert tree() == before and dict(control_server.runs) == runs
    assert control_server.stats.claims_received == 0


@pytest.mark.design("service.ping_signed")
def test_a_ping_reply_with_a_bad_signature_is_not_taken(
    control_server: ControlServer,
) -> None:
    """A server without the token that answers 200 ok is not a reached control server."""

    class Liar(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            data = json.dumps({"ok": True, "runner_id": "0" * 16})
            self.send_response(200)
            self.send_header(protocol.SIGNATURE, "0" * 64)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data.encode())

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    liar = ThreadingHTTPServer((HOST, 0), Liar)
    thread = threading.Thread(target=liar.serve_forever, daemon=True)
    thread.start()
    try:
        found = ControlClient(f"http://{HOST}:{liar.server_address[1]}", "t" * 64).ping()
    finally:
        liar.shutdown()
        liar.server_close()
        thread.join()
    assert found["reached"] is False and found["runner_id"] is None
    assert "not signed for this request" in str(found["detail"])


@pytest.mark.design("service.probe_proves_the_gate")
def test_a_failed_probe_names_the_token_and_the_protocol_among_its_causes(bench: Bench) -> None:
    from test_service import Forwarder

    service = bench.start()
    proxy = Forwarder(service.port, drop=True)
    bench.closers.append(proxy)
    code, out = bench.test(proxy.port)
    assert code == 3, out
    assert "its token differs, or its SDK speaks another protocol version" in out


# --- a replayed header cannot make the server read a large body ----------------------------


@pytest.mark.design("service.tls_deadlines")
def test_a_signed_header_older_than_60_s_is_refused_before_any_body_is_read(
    control_server: ControlServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = [0]
    real = control._Reader.readinto

    def counted(self: control._Reader, buffer: Any) -> int:
        n = real(self, buffer)
        taken[0] += n
        return n

    monkeypatch.setattr(control._Reader, "readinto", counted)
    control_server.start()
    host, port = control_server.server.server_address[:2]
    try:
        for age, expected in ((120, "clock"), (-120, "clock")):
            taken[0] = 0  # each request is counted alone, not summed with the one before
            # A header the token made, captured and sent again: its signature is good.
            stale = protocol.request_headers(
                control_server.token, "POST", "/v1/blobs", b"", time.time() - age
            )
            conn = http.client.HTTPConnection(str(host), int(port), timeout=10)
            conn.putrequest("POST", "/v1/blobs")
            conn.putheader("Content-Length", str(14 * 1024 * 1024))
            for name, value in stale.items():
                conn.putheader(name, value)
            conn.endheaders()
            conn.auto_open = 0

            def push(conn: http.client.HTTPConnection = conn) -> None:
                chunk = b"x" * 65536
                with contextlib.suppress(OSError, http.client.HTTPException):
                    for _ in range(14 * 1024 * 1024 // len(chunk)):
                        conn.send(chunk)

            pusher = threading.Thread(target=push, daemon=True)
            pusher.start()
            reply = conn.getresponse()
            assert reply.status == 401 and json.loads(reply.read())["error"] == expected
            pusher.join(10)  # before the close, as a send under a close can crash
            conn.close()
            assert taken[0] < 64 * 1024, f"age {age}: {taken[0]} bytes of the body were read"
        # Positive control: a fresh signed body of the same kind is read in full.
        taken[0] = 0
        client = ControlClient(control_server.url, control_server.token)
        assert client.request("POST", "/v1/blobs", blob(5 * 1024 * 1024))[0] == 200
        assert taken[0] > 5 * 1024 * 1024
    finally:
        control_server.stop()


def blob(size: int) -> dict[str, str]:
    data = os.urandom(size)
    return {"run": RUN, "sha256": protocol.digest(data), "data": base64.b64encode(data).decode()}


def test_a_busy_https_setup_does_not_say_it_verified_nothing_was_needed(tmp_path: Path) -> None:
    files = make_certificates(tmp_path / "pki", ips=(HOST,), names=("control.test",))
    secure = ControlClient("https://127.0.0.1:9", "t" * 64, tls.client_context(files.ca))
    plain = ControlClient("http://127.0.0.1:9", "t" * 64)
    assert asgi_gate.unreached(secure, "busy")["tls"] == "not connected"
    assert asgi_gate.unreached(plain, "busy")["tls"] == "none"


def test_a_version_in_a_message_has_no_control_characters() -> None:
    assert versions.shown("9\x1b[31m\n\x00red") == "9[31mred"
    assert len(versions.shown("1" * 500)) == 64


# --- the ping in scrimmage test --json -------------------------------------------------------


def document(out: str) -> dict[str, Any]:
    """The one JSON document a --json run printed."""
    found, _ = json.JSONDecoder().raw_decode(out[out.index("{") :])
    return dict(found)


@pytest.mark.design("service.ping_report")
def test_json_of_a_run_that_reaches_setup_carries_the_ping(bench: Bench) -> None:
    service = bench.start()
    code, out = bench.test(service.port, json_out=True)
    assert code == 0, out
    ping = document(out)["control_ping"]
    assert ping["reached"] is True and ping["tls"] == "none" and len(ping["runner_id"]) == 16
    assert ping["url"] == bench.control


@pytest.mark.design("service.ping_stops")
def test_json_of_a_run_that_fails_the_ping_says_why_and_has_no_ping_fields(bench: Bench) -> None:
    bench.monkeypatch.setattr(running, "READY_S", 0.5)
    nobody = f"http://{HOST}:{free_port()}"
    service = bench.start(SCRIMMAGE_CONTROL_URL=nobody)
    code, out = bench.test(service.port, json_out=True)
    found = document(out)
    assert code == 3 and found["status"] == "infra", out
    assert "Connection refused" in json.dumps(found["infra"])
    assert "control_ping" not in found
