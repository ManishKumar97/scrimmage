"""The version rule between a service's SDK and the runner, and the service's ping.

A service in another venv has its own scrimmage-core, built here at the version a test names,
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
from scrimmage_core.cert import make_certificates
from scrimmage_core.client import PING_TIMEOUT_S, ControlClient
from scrimmage_core.control import ControlServer
from scrimmage_core.records import RecordStore
from service_harness import Service, drive
from test_service import BIN, Bench, bench

pytestmark = pytest.mark.slow
__all__ = ["bench"]  # a fixture, used by name

ROOT = Path(__file__).parents[1]
RUN = "run-0123456789ab"
HOST = "127.0.0.1"


# --- the rule itself -----------------------------------------------------------------------

RULE = [
    ("1.4.2", "1.4.2", versions.Verdict.SAME),
    ("1.4.2", "1.4.3", versions.Verdict.WARN),
    ("1.4.2", "1.5.0", versions.Verdict.WARN),
    ("1.4.2", "1.4.2.dev3", versions.Verdict.WARN),
    ("1.4.2", "1.4.2rc1", versions.Verdict.WARN),
    ("1.4.2", "1.4.2+gabc", versions.Verdict.WARN),
    ("1.4", "1.4.0", versions.Verdict.SAME),  # the same release, spelled shorter
    ("1.4.2", "2.0.0", versions.Verdict.MAJOR),
    ("1.4.2", "10.4.2", versions.Verdict.MAJOR),
    ("1.4.2", "0.9.0", versions.Verdict.MAJOR),
    ("0.4.2", "0.4.9", versions.Verdict.WARN),
    ("0.4.2", "0.5.0", versions.Verdict.MAJOR),  # while the major is 0, a minor is a major
    ("0.4.2", "1.4.2", versions.Verdict.MAJOR),
    ("1.4.2", "unknown", versions.Verdict.UNREADABLE),
    ("1.4.2", "", versions.Verdict.UNREADABLE),
    ("1.4.2", "v1.4.2", versions.Verdict.UNREADABLE),
    ("unknown", "unknown", versions.Verdict.UNREADABLE),
    ("1.4.2", "1.4.2junk", versions.Verdict.UNREADABLE),
    ("1.4.2", "1.4.2\n", versions.Verdict.UNREADABLE),  # `$` would have let this through
    (
        "1.4.2",
        "\N{ARABIC-INDIC DIGIT ONE}.\N{ARABIC-INDIC DIGIT FOUR}.\N{ARABIC-INDIC DIGIT TWO}",
        versions.Verdict.UNREADABLE,
    ),  # Arabic-Indic digits
    ("1.4.2", "1.4.2-1", versions.Verdict.UNREADABLE),
    ("1.4.2", "1.4.2.post1", versions.Verdict.WARN),
    ("1.4.2", "1.4.2rc1.dev0+abc.1", versions.Verdict.WARN),
]


@pytest.mark.parametrize(
    ("runner", "sdk", "verdict"),
    RULE,
    ids=[
        f"{runner}-vs-{sdk.encode('unicode_escape').decode() or 'empty'}" for runner, sdk, _ in RULE
    ],
)
def test_the_version_rule_reads_the_release_and_keeps_the_rest_as_text(
    runner: str, sdk: str, verdict: versions.Verdict
) -> None:
    assert versions.judge(sdk, runner) is verdict


def test_a_version_that_is_not_text_is_unreadable() -> None:
    assert versions.judge(None, "1.4.2") is versions.Verdict.UNREADABLE
    assert versions.judge(7, "1.4.2") is versions.Verdict.UNREADABLE


# --- SDKs of other versions, built as the runner would meet them ---------------------------


def uv(*args: str, cwd: Path) -> None:
    done = subprocess.run(["uv", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr


@pytest.fixture(scope="session")
def sdk_venv(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Path]:
    """The bin folder of a venv that runs the example service with scrimmage-core at the given
    version: the package built with uv from a copy whose version was set."""
    made: dict[str, Path] = {}

    def build(version: str) -> Path:
        if version in made:
            return made[version]
        folder = tmp_path_factory.mktemp("sdk-" + "".join(c for c in version if c.isalnum()))
        copy = folder / "core"
        shutil.copytree(
            ROOT / "packages" / "scrimmage-core",
            copy,
            ignore=shutil.ignore_patterns("__pycache__", "dist", "*.egg-info"),
        )
        pyproject = copy / "pyproject.toml"
        text = pyproject.read_text()
        assert 'version = "0.0.0"' in text
        pyproject.write_text(text.replace('version = "0.0.0"', f'version = "{version}"', 1))
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
            str(ROOT / "packages" / "scrimmage"),
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


@pytest.mark.design("service.version_major")
@pytest.mark.parametrize(
    ("sdk", "runner"),
    [("1.4.2", "0.0.0"), ("1.4.2", "10.0.0"), ("0.1.0", "0.0.0")],
    ids=["1-vs-0", "1-vs-10", "0_1-vs-0_0"],
)
def test_an_sdk_one_major_off_is_refused_at_the_probe(
    bench: Bench,
    sdk_venv: Callable[[str], Path],
    runner_is: Callable[[str], None],
    sdk: str,
    runner: str,
) -> None:
    runner_is(runner)
    code, out = against(bench, sdk_venv(sdk))
    assert code == 2, out
    assert f"scrimmage-core {sdk} and this runner is {runner}, another major release" in out
    assert f"install scrimmage-core {runner} in the service" in out
    sent_nothing(bench)


@pytest.mark.design("service.version_minor")
def test_an_sdk_one_minor_off_warns_and_passes(
    bench: Bench, sdk_venv: Callable[[str], Path], runner_is: Callable[[str], None]
) -> None:
    runner_is("1.5.0")
    code, out = against(bench, sdk_venv("1.4.2"))
    assert code == 0, out
    warning = "the service runs scrimmage-core 1.4.2 and this runner is 1.5.0"
    assert out.count(warning) == 1, out  # said once, in the report
    assert "warning: " + warning in out
    # The setup request and the run each claimed their plan, with the SDK's version in it.
    assert "claims received 2" in out, out


@pytest.mark.design("service.version_zero_x")
def test_on_0_x_a_minor_off_counts_as_a_major_and_a_patch_off_does_not(
    bench: Bench, sdk_venv: Callable[[str], Path], runner_is: Callable[[str], None]
) -> None:
    runner_is("0.5.0")
    code, out = against(bench, sdk_venv("0.1.0"))  # the SDK is 0.1.0 and the runner 0.5.0
    assert code == 2 and "another major release" in out, out
    runner_is("0.1.9")
    code, out = against(bench, sdk_venv("0.1.0"))  # the same 0.1 line: a patch off
    assert code == 0, out
    assert "the service runs scrimmage-core 0.1.0 and this runner is 0.1.9" in out


@pytest.mark.design("service.version_dev_local")
@pytest.mark.parametrize("sdk", ["0.0.0.dev3", "0.0.0+gabc"], ids=["dev", "local"])
def test_dev_and_local_versions_compare_by_release_and_warn(
    bench: Bench, sdk_venv: Callable[[str], Path], sdk: str
) -> None:
    code, out = against(bench, sdk_venv(sdk))  # this runner is 0.0.0
    assert code == 0, out
    assert f"the service runs scrimmage-core {sdk} and this runner is 0.0.0" in out


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
@pytest.mark.parametrize("found", ["unknown", "banana", ""], ids=["unknown", "text", "empty"])
def test_an_unknown_sdk_version_is_refused(bench: Bench, found: str) -> None:
    def edit(report: dict[str, Any]) -> None:
        report["sdk"] = found

    code, out = against(bench, proxy=rewritten(bench, edit))
    assert code == 2, out
    assert f"the service's SDK version is {found!r}" in out
    assert "not an installed scrimmage-core release" in out
    sent_nothing(bench)


@pytest.mark.design("service.version_report_v1")
def test_a_v1_startup_report_names_the_old_sdk(bench: Bench) -> None:
    def edit(report: dict[str, Any]) -> None:
        report["v"] = 1
        del report["control"]

    code, out = against(bench, proxy=rewritten(bench, edit))
    assert code == 2, out
    assert "the service's Scrimmage is older than this runner (startup report v1)" in out
    assert f"install scrimmage-core {versions.installed()} in the service" in out
    sent_nothing(bench)


def test_a_report_of_a_version_the_runner_does_not_read_is_refused(bench: Bench) -> None:
    def edit(report: dict[str, Any]) -> None:
        report["v"] = 3

    code, out = against(bench, proxy=rewritten(bench, edit))
    assert code == 2 and "startup report is version 3" in out, out
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
def test_a_worker_from_another_major_is_refused_at_its_first_claim_and_stops_the_test(
    bench: Bench, sdk_venv: Callable[[str], Path]
) -> None:
    """Two workers behind one port. The probe and the setup request reach the first, of this
    runner's major; the runs reach the second, of another."""
    from test_service import Forwarder

    good = bench.start()
    old = bench.start(bin_dir=sdk_venv("1.4.2"))
    proxy = Forwarder(good.port, runs_to=old.port)
    bench.closers.append(proxy)
    code, out = bench.test(proxy.port, runs=3)
    assert code == 2, out
    assert "scrimmage-core 1.4.2 and this runner is 0.0.0, another major release" in out
    assert "in every process of it" in out
    # The first run stopped the test: the other two were never sent.
    assert sum(1 for run in proxy.seen if run.startswith("run-")) == 1, proxy.seen
    assert "503 version" not in out  # the stop is the test's, not a run counted as an error


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
        service_client.check_control(report, service.server.runner_id)
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
    nobody = f"http://{HOST}:{free_port()}"  # no control server listens there
    code, out = against(bench, control_url=nobody)
    assert code == 3, out
    assert f"the service cannot reach the control server at {nobody}: " in out
    assert "Connection refused" in out
    assert "check that SCRIMMAGE_CONTROL_URL is an address the service can reach" in out
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
            data = json.dumps({"ok": True, "runner_id": "0" * 16, "server_time": time.time()})
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


@pytest.mark.design("service.tls_deadlines")
@pytest.mark.parametrize("skew", [-120, 120, 0], ids=["behind", "ahead", "true"])
def test_a_fresh_client_with_a_skewed_clock_sends_a_large_blob_after_a_small_request(
    control_server: ControlServer, skew: int
) -> None:
    """The server refuses a large body with a stale header before reading it, and closes the
    connection; the client learns the offset from a small request first, so it never meets
    that refusal."""
    control_server.start()
    try:
        client = ControlClient(control_server.url, control_server.token)
        client.offset = skew  # its clock is wrong by this much, and it has had no reply yet
        assert client.request("POST", "/v1/blobs", blob(5 * 1024 * 1024))[0] == 200
        assert abs(client.offset) < 5, client.offset
    finally:
        control_server.stop()


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
    nobody = f"http://{HOST}:{free_port()}"
    service = bench.start(SCRIMMAGE_CONTROL_URL=nobody)
    code, out = bench.test(service.port, json_out=True)
    found = document(out)
    assert code == 3 and found["status"] == "infra", out
    assert "Connection refused" in json.dumps(found["infra"])
    assert "control_ping" not in found
