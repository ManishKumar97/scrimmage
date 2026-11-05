"""The signed protocol between the SDK and the control server, and the record store."""

import http.client
import json
import stat
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from faultline_sdk import protocol
from faultline_sdk.client import ControlClient, ControlError, Sender
from faultline_sdk.control import ControlServer
from faultline_sdk.records import RecordStore

TOKEN = "a" * 64
PLAN: dict[str, object] = {"v": 1}


@pytest.fixture
def server(tmp_path: Path) -> Iterator[ControlServer]:
    control = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    control.start()
    control.open_run("run-1", "baseline", PLAN)
    yield control
    control.stop()


def raw(
    control: ControlServer,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict[str, object], str]:
    host, port = control.server.server_address[:2]
    conn = http.client.HTTPConnection(str(host), port, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return (
        response.status,
        json.loads(data),
        response.getheader(protocol.SIGNATURE) or "",
    )


def line(seq: int, run: str = "run-1") -> dict[str, object]:
    return {"v": 1, "run": run, "row": "baseline", "seq": seq, "proc": {"pid": 1}}


def test_a_signed_request_gets_its_plan(server: ControlServer) -> None:
    assert ControlClient(server.url, TOKEN).fetch_plan("run-1") == PLAN
    assert server.runs["run-1"].fetched


@pytest.mark.design("protocol.wrong_signature")
def test_a_request_signed_with_another_token_is_refused(server: ControlServer) -> None:
    with pytest.raises(ControlError, match="not signed for this request"):
        ControlClient(server.url, "b" * 64).fetch_plan("run-1")  # nor can it trust the reply
    headers = protocol.request_headers(TOKEN, "GET", "/v1/plans/run-1", b"", time.time())
    headers[protocol.SIGNATURE] = "0" * 64
    assert raw(server, "GET", "/v1/plans/run-1", headers)[:2] == (
        401,
        {"error": "bad_signature", "server_time": pytest.approx(time.time(), abs=5)},
    )
    for missing in (protocol.NONCE, protocol.TIME):
        partial = protocol.request_headers(TOKEN, "GET", "/v1/plans/run-1", b"", time.time())
        del partial[missing]
        assert raw(server, "GET", "/v1/plans/run-1", partial)[0] == 401


def test_a_time_that_is_not_a_number_is_refused(server: ControlServer) -> None:
    nonce = "c" * 32
    sig = protocol.sign_request(TOKEN, "GET", "/v1/plans/run-1", "soon", nonce, b"")
    headers = {protocol.TIME: "soon", protocol.NONCE: nonce, protocol.SIGNATURE: sig}
    assert raw(server, "GET", "/v1/plans/run-1", headers)[1]["error"] == "bad_signature"


@pytest.mark.design("protocol.replay")
def test_a_replayed_request_is_refused(server: ControlServer) -> None:
    headers = protocol.request_headers(TOKEN, "GET", "/v1/plans/run-1", b"", time.time())
    assert raw(server, "GET", "/v1/plans/run-1", headers)[0] == 200
    status, reply, _ = raw(server, "GET", "/v1/plans/run-1", headers)
    assert (status, reply["error"]) == (401, "replayed")


def test_old_nonces_are_forgotten_after_two_minutes() -> None:
    store = protocol.NonceStore()
    assert store.first_use("n", now=1000.0)
    assert not store.first_use("n", now=1001.0)
    assert store.first_use("n", now=1000.0 + protocol.NONCE_KEEP_S + 1)


class Impostor:
    """A server at some address that answers every request with one fixed reply."""

    def __init__(self, status: int, body: bytes, signature: str) -> None:
        reply = (status, body, signature)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(reply[0])
                self.send_header(protocol.SIGNATURE, reply[2])
                self.send_header("Content-Length", str(len(reply[1])))
                self.end_headers()
                self.wfile.write(reply[1])

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.design("protocol.reply_bound_to_request")
def test_a_reply_made_for_another_request_is_refused(server: ControlServer) -> None:
    first = protocol.request_headers(TOKEN, "GET", "/v1/plans/run-1", b"", time.time())
    status, reply, signature = raw(server, "GET", "/v1/plans/run-1", first)
    # Someone replays that genuine, correctly signed reply to a later request.
    impostor = Impostor(status, json.dumps(reply).encode(), signature)
    try:
        with pytest.raises(ControlError, match="not signed for this request"):
            ControlClient(impostor.url, TOKEN).fetch_plan("run-1")
    finally:
        impostor.stop()


@pytest.mark.design("protocol.forged_reply")
def test_a_reply_not_signed_with_the_token_is_refused() -> None:
    body = json.dumps({"server_time": time.time(), "plan": {"v": 1, "evil": True}}).encode()
    impostor = Impostor(200, body, "f" * 64)
    try:
        with pytest.raises(ControlError, match="not signed for this request"):
            ControlClient(impostor.url, TOKEN).fetch_plan("run-1")
    finally:
        impostor.stop()


@pytest.mark.design("protocol.clock_retry")
def test_a_five_minute_clock_gap_is_recovered_with_one_retry(tmp_path: Path) -> None:
    control = ControlServer(TOKEN, RecordStore(tmp_path / "r"), clock=lambda: time.time() + 300)
    control.start()
    control.open_run("run-1", "baseline", PLAN)
    try:
        client = ControlClient(control.url, TOKEN)
        assert client.fetch_plan("run-1") == PLAN
        assert client.offset == pytest.approx(300, abs=5)
    finally:
        control.stop()


@pytest.mark.design("protocol.closed_run")
def test_lines_for_a_closed_run_and_its_plan_are_refused(server: ControlServer) -> None:
    client = ControlClient(server.url, TOKEN)
    server.close_run("run-1")
    status, reply = client.request("POST", "/v1/records", {"run": "run-1", "lines": [line(1)]})
    assert (status, reply["error"]) == (409, "run_closed")
    with pytest.raises(ControlError, match="no_such_run"):
        client.fetch_plan("run-1")


def test_bad_requests_are_refused(server: ControlServer) -> None:
    client = ControlClient(server.url, TOKEN)
    assert client.request("POST", "/v1/records", {"lines": []})[1]["error"] == "bad_batch"
    assert client.request("GET", "/v1/other")[1]["error"] == "not_found"
    assert client.request("POST", "/v1/records", {"run": "run-9", "lines": []})[0] == 409
    big = {protocol.TIME: "1", "Content-Length": str(60 * 1024 * 1024)}
    assert raw_status(server, big) == 413


def raw_status(control: ControlServer, headers: dict[str, str]) -> int:
    host, port = control.server.server_address[:2]
    conn = http.client.HTTPConnection(str(host), port, timeout=5)
    conn.putrequest("POST", "/v1/records")
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    status = conn.getresponse().status
    conn.close()
    return status


def test_an_unreachable_or_bad_server_is_a_control_error(tmp_path: Path) -> None:
    with pytest.raises(ControlError, match="not http://host:port"):
        ControlClient("ftp://x", TOKEN)
    with pytest.raises(ControlError, match="cannot reach"):
        ControlClient("http://127.0.0.1:9", TOKEN).fetch_plan("run-1")


@pytest.mark.design("record.dedupe")
def test_a_line_sent_twice_is_written_once(server: ControlServer) -> None:
    client = ControlClient(server.url, TOKEN)
    for _ in range(2):
        client.request(
            "POST",
            "/v1/records",
            {"run": "run-1", "lines": [line(1), line(1, run="run-2")]},
        )
    assert server.store.lines("run-1") == [line(1)]


@pytest.mark.design("record.owner_only")
def test_record_files_and_their_folder_are_owner_only(server: ControlServer) -> None:
    folder = server.store.folder
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(server.store.path("run-1").stat().st_mode) == 0o600


@pytest.mark.design("record.sender_batches")
def test_the_sender_sends_every_line_and_reports_drops(server: ControlServer) -> None:
    sender = Sender(ControlClient(server.url, TOKEN), "run-1")
    sender.start()
    for seq in range(1, 1201):
        sender.put(line(seq), keep=seq == 1)
    sender.flush()
    assert len(server.store.lines("run-1")) == 1200 and not sender.failed
    full = Sender(ControlClient(server.url, TOKEN), "run-1")
    full.queue.maxsize = 1
    full.put(line(5000))
    full.put(line(5001))  # no room: dropped, and the record is marked incomplete
    full.flush()
    assert server.runs["run-1"].dropped == 1


def test_a_sender_that_cannot_reach_the_server_marks_failure() -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
    sender.put(line(1))
    sender.flush()
    assert sender.failed


def test_a_refused_line_tells_the_runner_once(tmp_path: Path) -> None:
    told: list[str] = []
    control = ControlServer(TOKEN, RecordStore(tmp_path / "r"), on_refused=told.append)
    control.start()
    control.open_run("run-1", "baseline", PLAN)
    try:
        client = ControlClient(control.url, TOKEN)
        for seq in (1, 2):
            client.request(
                "POST",
                "/v1/records",
                {"run": "run-1", "lines": [{**line(seq), "action": "refused"}]},
            )
        assert told == ["run-1"]
    finally:
        control.stop()
