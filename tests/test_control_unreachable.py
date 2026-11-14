"""The service's ping asks again while a cluster routes to it, and each failure names its fix.

The control address is made to fail in each way on loopback: nothing listens (a refusal), a relay
that takes the connection and says nothing (a drop), a name that does not resolve, a certificate
from another authority. The class of the error is in the ping's `error`, so none of this needs
https.
"""

import contextlib
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import test_service
from ports import free_port
from scrimmage_core import asgi_gate, service_client, tls
from scrimmage_core import runner as running
from scrimmage_core.cert import make_certificates
from scrimmage_core.client import ControlClient
from service_harness import eventually
from test_service import Bench, Forwarder, a_service_runner, bench
from test_service_versions import against, sent_nothing

pytestmark = pytest.mark.slow
__all__ = ["bench"]  # a fixture, used by name

HOST = "127.0.0.1"
TOKEN = "t" * 64
DROPS = (
    "; nothing answered, as when a NetworkPolicy or a firewall drops the traffic; allow the "
    "service's pods to reach this runner on port "
)
REFUSES = (
    "; nothing listens there, as when a Kubernetes Service has no ready endpoints; check that "
    "the Service's selector matches this runner's pod"
)
WAITING = "scrimmage: waiting for the service to reach the control server at "


class Relay:
    """A TCP relay from a port to the control server. It listens only after `open_in` is called
    and its delay has passed, so before that the port refuses. silent: it takes each connection
    and sends nothing, as a NetworkPolicy's drop looks to a client that connected."""

    def __init__(self, upstream: int, *, silent: bool = False) -> None:
        self.port = free_port()
        self.url = f"http://{HOST}:{self.port}"
        self.upstream, self.silent = upstream, silent
        self.accepted = 0
        self.stopping = threading.Event()
        self.held: list[socket.socket] = []
        self.opens = 0.0
        self.opened = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def open_in(self, seconds: float) -> None:
        self.opens = seconds
        self.opened.set()

    def serve(self) -> None:
        while not self.opened.wait(0.1):
            if self.stopping.is_set():
                return
        if self.stopping.wait(self.opens):
            return
        listener = socket.create_server((HOST, self.port))
        listener.settimeout(0.1)
        try:
            while not self.stopping.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                self.accepted += 1
                self.held.append(client)
                if not self.silent:
                    threading.Thread(target=self.pipe, args=(client,), daemon=True).start()
        finally:
            listener.close()

    def pipe(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection((HOST, self.upstream))
        except OSError:
            client.close()
            return
        self.held.append(upstream)
        for source, target in ((client, upstream), (upstream, client)):
            threading.Thread(target=self.copy, args=(source, target), daemon=True).start()

    @staticmethod
    def copy(source: socket.socket, target: socket.socket) -> None:
        try:
            with contextlib.suppress(OSError):
                while data := source.recv(65536):
                    target.sendall(data)
        finally:
            for sock in (source, target):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self.stopping.set()
        for sock in self.held:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # wakes a copy thread blocked in recv
            sock.close()
        self.thread.join(5)


def upstream_port(bench: Bench) -> int:
    return int(bench.control.rsplit(":", 1)[1])


def asked_for(out: str, url: str) -> bool:
    """The failure's own line, with the seconds the runner really asked for."""
    pattern = rf"the service cannot reach the control server at {re.escape(url)} after [0-9.]+ s "
    return re.search(pattern + "of asking: ", out) is not None


def probes(forwarder: Forwarder) -> int:
    return sum(1 for seen in forwarder.seen if seen.startswith("probe-"))


def line_count(out: str, text: str) -> int:
    return sum(1 for line in out.splitlines() if line.startswith(text))


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe asks again every 0.2 s, not every second."""
    monkeypatch.setattr(running, "CONTROL_STEP_S", 0.2)


# --- the ping's error ------------------------------------------------------------------------


@pytest.mark.design("service.ping_error")
def test_the_ping_reports_its_error_class_on_http_and_https(tmp_path: Path) -> None:
    refused = f"http://{HOST}:{free_port()}"
    ping = ControlClient(refused, TOKEN).ping()
    assert (ping["tls"], ping["error"]) == ("none", "ConnectionRefusedError"), ping
    assert "Connection refused" in str(ping["detail"])
    # The class is there on http, where `tls` has nothing to say, and the hint follows it.
    with pytest.raises(service_client.Setup) as stopped:
        service_client.check_control({"control": ping}, "r" * 16, asked_s=30.0)
    assert str(stopped.value).startswith(
        f"the service cannot reach the control server at {refused} after 30 s of asking: "
    )
    assert str(stopped.value).endswith(REFUSES)

    gone = ControlClient("http://no-such-name.invalid:7700", TOKEN).ping()
    assert (gone["tls"], gone["error"]) == ("none", "gaierror"), gone

    ours = make_certificates(tmp_path / "ours", ips=(HOST,), names=("control.test",))
    https = ControlClient(f"https://{HOST}:{free_port()}", TOKEN, tls.client_context(ours.ca))
    found = https.ping()  # nothing listens: the class goes in both fields over https
    assert (found["tls"], found["error"]) == ("ConnectionRefusedError",) * 2, found
    with pytest.raises(service_client.Setup) as certificate:
        service_client.check_control(
            {"control": {**found, "error": "SSLCertVerificationError"}}, "r" * 16, asked_s=30.0
        )
    assert str(certificate.value).endswith("check SCRIMMAGE_CONTROL_CA")

    # No ping was made, so there is no error for the runner to ask again about.
    busy = asgi_gate.unreached(https, "busy")
    assert busy["error"] is None and not service_client.still_routing({"control": busy})


def test_busy_control_slots_are_not_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = a_service_runner(tmp_path, monkeypatch)
    busy = asgi_gate.unreached(ControlClient(f"http://{HOST}:9", TOKEN), "busy")
    probed = service_client.Probed({"control": busy}, service_client.Reply())

    def never(*_: Any) -> None:
        raise AssertionError("the probe was sent again")

    monkeypatch.setattr(service_client, "probe", never)
    try:
        assert made.control_reached(probed, time.monotonic() + 30) is probed
    finally:
        made.server.server.server_close()


def test_a_name_that_resolves_later_is_reached_on_the_next_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(running, "CONTROL_STEP_S", 0.01)
    url = "https://scrimmage:7700"
    failed = {"reached": False, "error": "gaierror", "detail": "[Errno -2] x", "url": url}
    reports = [{"control": failed}, {"control": failed}, {"control": {"reached": True}}]
    asked: list[int] = []

    def probe(*_: Any) -> service_client.Probed:
        asked.append(1)
        return service_client.Probed(reports[len(asked)], service_client.Reply())

    monkeypatch.setattr(service_client, "probe", probe)
    made = a_service_runner(tmp_path, monkeypatch)
    try:
        first = service_client.Probed(reports[0], service_client.Reply())
        found = made.control_reached(first, time.monotonic() + 30)
    finally:
        made.server.server.server_close()
    assert found.report == reports[2] and len(asked) == 2
    assert line_count(capsys.readouterr().err, WAITING) == 1


# --- the ping asked again --------------------------------------------------------------------


@pytest.mark.design("service.ping_retry")
def test_a_ping_refused_at_first_is_asked_again_until_it_is_reached(
    bench: Bench, quick: None
) -> None:
    relay = Relay(upstream_port(bench))
    bench.closers.append(relay)
    service = bench.start(SCRIMMAGE_CONTROL_URL=relay.url)
    watcher = Forwarder(service.port)
    bench.closers.append(watcher)

    def opens_after_the_first_probe() -> None:
        eventually(lambda: watcher.seen, within=60)
        relay.open_in(3.0)  # the first probe met a refusal, as a pod not yet routed to does

    threading.Thread(target=opens_after_the_first_probe, daemon=True).start()
    code, out = bench.test(watcher.port)
    assert code == 0, out + service.text
    assert line_count(out, WAITING + relay.url) == 1, out
    assert probes(watcher) > 2  # the probe was sent again while the port refused
    # A run is sent once: only the probe is asked again.
    assert sum(s.startswith("run-") for s in watcher.seen) == 1, watcher.seen
    assert sum(s.startswith("setup-") for s in watcher.seen) == 1, watcher.seen
    assert f"{relay.url} (nothing listens yet)" in out, out


@pytest.mark.design("service.ping_retry")
def test_a_ping_that_times_out_for_the_whole_wait_names_a_dropping_policy(
    bench: Bench, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(running, "READY_S", 6.0)  # one ping waits 5 s, so this is two tries
    relay = Relay(upstream_port(bench), silent=True)
    bench.closers.append(relay)
    relay.open_in(0.0)
    code, out = against(bench, control_url=relay.url)
    assert code == 3, out
    assert asked_for(out, relay.url), out
    assert f"timed out{DROPS}{relay.port}" in out, out
    assert relay.accepted >= 2  # it was asked again
    assert line_count(out, WAITING + relay.url) == 1, out
    sent_nothing(bench)


@pytest.mark.design("service.ping_retry")
def test_a_ping_refused_for_the_whole_wait_names_the_services_selector(
    bench: Bench, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(running, "READY_S", 1.5)
    nobody = f"http://{HOST}:{free_port()}"
    service = bench.start(SCRIMMAGE_CONTROL_URL=nobody)
    watcher = Forwarder(service.port)
    bench.closers.append(watcher)
    code, out = bench.test(watcher.port)
    assert code == 3, out
    assert asked_for(out, nobody) and f"Connection refused{REFUSES}" in out, out
    assert probes(watcher) >= 3  # about every 0.2 s for 1.5 s
    sent_nothing(bench)


@pytest.mark.design("service.ping_retry")
def test_a_control_name_that_does_not_resolve_names_the_service(
    bench: Bench, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(running, "READY_S", 1.0)
    monkeypatch.setattr(running, "CONTROL_STEP_S", 0.2)
    files = make_certificates(tmp_path / "pki", ips=(HOST,), names=("control.test",))
    url = "https://no-such-name.invalid:7700"
    service = bench.start(SCRIMMAGE_CONTROL_URL=url, SCRIMMAGE_CONTROL_CA=str(files.ca))
    code, out = bench.test(service.port)
    assert code == 3, out
    assert asked_for(out, url), out
    assert (
        "; the name no-such-name.invalid does not resolve where the service runs; check the "
        "host in SCRIMMAGE_CONTROL_URL and that the Service is in the service's namespace"
    ) in out
    sent_nothing(bench)


@pytest.mark.design("service.ping_retry")
def test_a_certificate_error_is_not_asked_again(
    bench: Bench, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ours = make_certificates(tmp_path / "ours", ips=(HOST,), names=("control.test",))
    theirs = make_certificates(tmp_path / "theirs", ips=(HOST,), names=("control.test",))
    port = free_port()
    extra = f'control = "{HOST}:{port}"\ncontrol_tls = {{cert = "{ours.cert}", key = "{ours.key}"}}'
    from test_service import FILE

    monkeypatch.setattr("test_service.FILE", FILE.replace("EXTRA", f"EXTRA\n{extra}"))
    service = bench.start(
        SCRIMMAGE_CONTROL_URL=f"https://{HOST}:{port}", SCRIMMAGE_CONTROL_CA=str(theirs.ca)
    )
    watcher = Forwarder(service.port)
    bench.closers.append(watcher)
    began = time.monotonic()
    code, out = bench.test(watcher.port)
    assert code == 3, out
    assert time.monotonic() - began < 6, out
    assert "certificate verify failed" in out and out.rstrip().endswith(
        "check SCRIMMAGE_CONTROL_CA"
    )
    assert "of asking" not in out and WAITING not in out
    assert probes(watcher) == 1
    sent_nothing(bench)


@pytest.mark.design("service.ping_retry")
def test_no_run_is_sent_when_the_ping_never_succeeds(
    bench: Bench, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(running, "READY_S", 1.0)
    service = bench.start(SCRIMMAGE_CONTROL_URL=f"http://{HOST}:{free_port()}")
    watcher = Forwarder(service.port)
    bench.closers.append(watcher)
    code, out = bench.test(watcher.port, runs=3)
    assert code == 3, out
    assert watcher.seen and all(s.startswith("probe-") for s in watcher.seen), watcher.seen
    assert not (bench.root / "scrimmage-runs").exists() or not list(
        (bench.root / "scrimmage-runs").glob("*/*.jsonl")
    )
    sent_nothing(bench)


@pytest.mark.design("service.ping_retry")
def test_the_probes_wait_and_the_pings_wait_share_one_deadline(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service's address listens 3 s late, and then its ping is refused: the two waits
    are one READY_S counted from the first probe, not 3 s plus a fresh READY_S."""
    monkeypatch.setattr(running, "READY_S", 4.0)
    monkeypatch.setattr(running, "CONTROL_STEP_S", 0.2)
    service = bench.start(SCRIMMAGE_CONTROL_URL=f"http://{HOST}:{free_port()}")
    late = Relay(service.port)  # the runner's address: refused until it opens
    bench.closers.append(late)
    late.open_in(3.0)
    began = time.monotonic()
    code, out = bench.test(late.port)
    spent = time.monotonic() - began
    assert code == 3 and "Connection refused" in out, out
    # One deadline ends near 4 s; a fresh wait for the ping would end past 7 s. The 2 s margin
    # covers the runner's own start on a loaded machine.
    assert spent < running.READY_S + 2.0, spent
    assert re.search(r"after (3\.[5-9]|4\.[0-9]) s of asking", out), out


def test_a_service_that_stops_answering_during_the_wait_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(running, "CONTROL_STEP_S", 0.01)
    failed = {"reached": False, "error": "ConnectionRefusedError", "detail": "x", "url": "u"}
    first = service_client.Probed({"control": failed}, service_client.Reply())
    silent = service_client.Probed(None, service_client.Reply(failed="reset"))
    monkeypatch.setattr(service_client, "probe", lambda *_: silent)
    made = a_service_runner(tmp_path, monkeypatch)
    try:
        found = made.control_reached(first, time.monotonic() + 30)
    finally:
        made.server.server.server_close()
    assert found is first and made.went_silent
    with pytest.raises(service_client.Setup) as stopped:
        service_client.check_control(
            {"control": failed}, "r" * 16, asked_s=made.asked_s, silent=made.went_silent
        )
    assert "(then the service stopped answering the probe)" in str(stopped.value)


def refused_then_silent(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> Relay:
    """A service whose ping is refused, reached through a relay that stops listening after its
    second probe: the runner's re-probe then gets no report. READY_S is 20 s."""
    monkeypatch.setattr(running, "READY_S", 20.0)
    monkeypatch.setattr(running, "CONTROL_STEP_S", 0.2)
    service = bench.start(SCRIMMAGE_CONTROL_URL=f"http://{HOST}:{free_port()}")
    relay = Relay(service.port)
    bench.closers.append(relay)
    relay.open_in(0.0)

    def stops_after_two() -> None:
        eventually(lambda: relay.accepted >= 2, within=60)
        relay.close()

    threading.Thread(target=stops_after_two, daemon=True).start()
    return relay


SILENT = "(then the service stopped answering the probe)"


@pytest.mark.design("service.ping_retry")
def test_a_service_that_goes_silent_in_a_test_says_so_and_how_long_it_asked(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = refused_then_silent(bench, monkeypatch)
    code, out = bench.test(relay.port)
    assert code == 3 and SILENT in out, out
    seconds = re.search(r"after ([0-9.]+) s of asking", out)
    assert seconds is not None and float(seconds.group(1)) < 10, out  # not READY_S


@pytest.mark.design("service.ping_retry")
def test_check_service_says_the_same_when_the_service_goes_silent(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scrimmage_core.cli import main

    relay = refused_then_silent(bench, monkeypatch)
    text = test_service.FILE.replace("PORT", str(relay.port)).replace("PATH", "/chat")
    for key, value in (
        ("EXTRA", ""),
        ("TOOL_LIST", json.dumps(test_service.TOOLS.split(","))),
        ("RUNS", "1"),
        ("ANSWER", "reply"),
        ("TIMEOUT", "30"),
        ("WAIT", "10"),
        ("GRACE", "0.2"),
    ):
        text = text.replace(key, value)
    path = tmp_path / "f-check.toml"
    path.write_text(text)
    bench.capsys.readouterr()
    code = main(["check", str(path), "--service"])
    out = bench.capsys.readouterr()
    assert code == 3 and SILENT in out.out + out.err, out
    seconds = re.search(r"after ([0-9.]+) s of asking", out.out + out.err)
    assert seconds is not None and float(seconds.group(1)) < 10, out
