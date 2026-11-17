"""The signed protocol between the SDK and the control server, and the record store."""

import base64
import dataclasses
import hashlib
import http.client
import json
import os
import queue
import signal
import stat
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scrimmage_core import client as client_module
from scrimmage_core import protocol, service_client, state
from scrimmage_core.budget import Ledger
from scrimmage_core.client import WAKE, ControlClient, ControlError, Sender
from scrimmage_core.control import ControlServer, proc_key
from scrimmage_core.plan import Plan, no_plan
from scrimmage_core.records import RecordStore

TOKEN = "a" * 64
PLAN: dict[str, object] = {"v": 1}


@pytest.fixture
def server(tmp_path: Path) -> Iterator[ControlServer]:
    control = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    control.start()
    control.open_run("run-1", "baseline", PLAN)
    yield control
    control.stop()


def a_plan() -> Plan:
    return Plan("run-1", "t", "baseline", (), (), (), ("tool:a",), 60.0)


def raw(
    control: ControlServer,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict[str, object], str]:
    host, port = control.server.server_address[:2]
    conn = http.client.HTTPConnection(str(host), port, timeout=30)
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


def served(fetched: object) -> object:
    """A served plan without the run's age, which the server adds to each copy."""
    assert isinstance(fetched, dict) and "elapsed_s" in fetched, fetched
    return {k: v for k, v in fetched.items() if k != "elapsed_s"}


def test_a_signed_request_gets_its_plan(server: ControlServer) -> None:
    assert served(ControlClient(server.url, TOKEN).fetch_plan("run-1")) == PLAN
    assert server.runs["run-1"].fetched


@pytest.mark.design("protocol.plan_says_run_age")
def test_a_served_plan_says_how_long_the_run_has_been_going(server: ControlServer) -> None:
    """Only a length of time is sent: a monotonic reading means nothing in another process."""
    server.runs["run-1"].opened -= 30.0  # the program took 30 s to load, so it asks late
    fetched = ControlClient(server.url, TOKEN).fetch_plan("run-1")
    assert isinstance(fetched, dict)
    assert 30.0 <= fetched["elapsed_s"] < 60.0  # 30 s, and however long the request took
    assert "elapsed_s" not in server.runs["run-1"].plan  # the stored plan is left as it was


@pytest.mark.design("protocol.run_age_sets_origin")
def test_a_fetched_plan_puts_the_runs_start_back_by_its_age() -> None:
    """A child fetches its plan late, and its time still counts from the run's start."""

    class Sink:
        def put(self, line: dict[str, object], *, keep: bool = False) -> None:
            del line, keep

    aged = dataclasses.replace(a_plan(), elapsed_s=45.0)
    run = state.RunState(None, Sink(), [], load=lambda: aged)
    loaded = time.monotonic()
    run.begin("a", {})  # its first gated call fetches the plan
    assert loaded - 45.0 <= run.began <= time.monotonic() - 45.0
    with pytest.raises(ValueError, match="elapsed_s is negative"):
        Plan.from_json({**aged.to_json(), "elapsed_s": -1.0})
    assert Plan.from_json(aged.to_json()) == aged
    assert "elapsed_s" not in a_plan().to_json()  # a stored plan round-trips as it was


@pytest.mark.design("protocol.wrong_signature")
def test_a_request_signed_with_another_token_is_refused(server: ControlServer) -> None:
    with pytest.raises(ControlError, match="not signed for this request"):
        ControlClient(server.url, "b" * 64).fetch_plan("run-1")  # nor can it trust the reply
    headers = protocol.request_headers(TOKEN, "GET", "/v1/plans/run-1", b"", time.time())
    headers[protocol.SIGNATURE] = "0" * 64
    assert raw(server, "GET", "/v1/plans/run-1", headers)[:2] == (
        401,
        {"error": "bad_signature"},
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
    body = json.dumps({"plan": {"v": 1, "evil": True}}).encode()
    impostor = Impostor(200, body, "f" * 64)
    try:
        with pytest.raises(ControlError, match="not signed for this request"):
            ControlClient(impostor.url, TOKEN).fetch_plan("run-1")
    finally:
        impostor.stop()


@pytest.fixture
def skewed(tmp_path: Path) -> Iterator[ControlServer]:
    """A control server whose clock is five minutes ahead of this machine's."""
    control = ControlServer(TOKEN, RecordStore(tmp_path / "r"), clock=lambda: time.time() + 300)
    control.start()
    control.open_run("run-1", "baseline", PLAN)
    yield control
    control.stop()


@pytest.mark.design("protocol.clock_off")
def test_a_five_minute_clock_gap_stops_at_the_ping_naming_ntp(skewed: ControlServer) -> None:
    client = ControlClient(skewed.url, TOKEN)
    ping = client.ping()
    assert (ping["reached"], ping["error"]) == (False, None), ping
    assert ping["detail"] == "the control server answered 401 clock", ping
    with pytest.raises(service_client.Setup) as stopped:
        service_client.check_control({"control": ping}, skewed.runner_id, asked_s=0.0)
    assert stopped.value.code == 3
    assert str(stopped.value) == (
        "the service's clock and this machine's differ by more than 60 s, so every signed "
        "request is refused; set both from the same NTP source"
    )


def test_the_client_never_retries_a_clock_refusal(
    skewed: ControlServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    handled: list[str] = []
    handle = skewed.handle

    def counted(
        method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, object]]:
        handled.append(path)
        return handle(method, path, headers, body)

    monkeypatch.setattr(skewed, "handle", counted)
    client = ControlClient(skewed.url, TOKEN)
    assert client.request("GET", "/v1/plans/run-1") == (401, {"error": "clock"})
    with pytest.raises(ControlError, match="no plan for run-1: clock"):
        client.fetch_plan("run-1")
    assert handled == ["/v1/plans/run-1", "/v1/plans/run-1"], handled  # one request each


def test_no_control_reply_carries_server_time(server: ControlServer) -> None:
    """A reply holds only its answer: the client signs with its own clock and learns no other."""
    replies: list[dict[str, object]] = []
    for path in ("/v1/plans/run-1", "/v1/plans/run-9", "/v1/other"):
        headers = protocol.request_headers(TOKEN, "GET", path, b"", time.time())
        replies.append(raw(server, "GET", path, headers)[1])
    stale = protocol.request_headers(TOKEN, "GET", "/v1/plans/run-1", b"", time.time() - 120)
    replies.append(raw(server, "GET", "/v1/plans/run-1", stale)[1])
    client = ControlClient(server.url, TOKEN)
    replies.append(client.request("POST", "/v1/ping")[1])
    replies.append(client.request("POST", "/v1/records", {"lines": []})[1])
    assert [sorted(reply) for reply in replies] == [
        ["plan"],
        ["error"],
        ["error"],
        ["error"],
        ["ok", "runner_id"],
        ["error"],
    ], replies
    timed = [reply for reply in replies if "server_time" in reply]
    assert not timed, timed


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
    conn = http.client.HTTPConnection(str(host), port, timeout=30)
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
    assert len(server.store.lines("run-1")) == 1200 and sender.unsent == []
    # the closing batch says how many lines this process made
    assert server.runs["run-1"].made == {proc_key(None): 1200}
    full = Sender(ControlClient(server.url, TOKEN), "run-1")
    full.queue.maxsize = 1
    full.put(line(5000))
    full.put(line(5001))  # no room: dropped, and the record is marked incomplete
    full.flush()
    assert server.runs["run-1"].dropped == 1


def test_flush_wakes_the_sender_instead_of_waiting_out_its_window(
    server: ControlServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A window of an hour: only the wake can end the thread inside flush's join, which
    # gives up after twice TIMEOUT_S. No clock is compared.
    monkeypatch.setattr(client_module, "BATCH_S", 3600.0)
    monkeypatch.setattr(client_module, "TIMEOUT_S", 0.5)
    sender = Sender(ControlClient(server.url, TOKEN), "run-1")
    sender.start()
    for seq in range(1, 4):
        sender.put(line(seq))
    sender.flush()
    assert not sender._thread.is_alive()  # type: ignore[reportPrivateUsage]
    assert len(server.store.lines("run-1")) == 3
    assert server.runs["run-1"].made == {proc_key(None): 3}


def test_the_stop_is_set_before_the_wake_is_put() -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
    seen: list[bool] = []

    class Watching(queue.Queue[dict[str, object]]):
        def put_nowait(self, item: dict[str, object]) -> None:
            seen.append(sender._stop.is_set())  # type: ignore[reportPrivateUsage]
            super().put_nowait(item)

    sender.queue = Watching(10)
    sender.start()
    sender.flush()
    assert seen == [True]


def test_lines_behind_the_wake_are_still_delivered(server: ControlServer) -> None:
    sender = Sender(ControlClient(server.url, TOKEN), "run-1")
    sender.queue.put(WAKE)
    for seq in range(1, 1201):
        sender.put(line(seq))
    sender.flush()  # no thread: flush itself drains, and the wake must not end the drain
    assert len(server.store.lines("run-1")) == 1200
    assert server.runs["run-1"].made == {proc_key(None): 1200}


def test_the_wake_is_not_a_line(server: ControlServer) -> None:
    sender = Sender(ControlClient(server.url, TOKEN), "run-1")
    batches: list[object] = []
    real = sender.client.request

    def spy(method: str, path: str, payload: object = None) -> tuple[int, dict[str, object]]:
        if path == "/v1/records":
            assert isinstance(payload, dict)
            batches.extend(payload["lines"])
        return real(method, path, payload)

    sender.client.request = spy  # type: ignore[method-assign]
    sender.queue.put(WAKE)
    sender.put(line(1))
    sender.queue.put(WAKE)
    sender.flush()
    assert batches == [line(1)]  # the wake is on no batch
    assert server.store.lines("run-1") == [line(1)]
    assert sender.unsent == []
    # a wake left in an eager sender's queue does not stop its next line
    eager = Sender(ControlClient(server.url, TOKEN), "run-1", eager=True)
    eager.queue.put(WAKE)
    eager.put(line(2))
    assert server.store.lines("run-1") == [line(1), line(2)]


@pytest.mark.design("record.resend")
def test_a_batch_that_fails_is_sent_again(server: ControlServer) -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
    sender.put(line(1))
    sender.flush()  # nobody listens: the line is kept, not lost
    assert sender.unsent == [line(1)]
    sender.client = ControlClient(server.url, TOKEN)
    sender.flush()
    assert server.store.lines("run-1") == [line(1)]
    assert server.runs["run-1"].made == {proc_key(None): 1}


@pytest.mark.design("record.child_sends_as_it_goes")
def test_a_child_sends_each_line_as_it_is_made_and_needs_no_thread(server: ControlServer) -> None:
    sender = Sender(ControlClient(server.url, TOKEN), "run-1", eager=True)
    sender.put(line(1))
    assert server.store.lines("run-1") == [line(1)]
    assert server.runs["run-1"].made == {proc_key(None): 1}  # no last batch is needed
    assert not sender._thread.is_alive()  # type: ignore[reportPrivateUsage]


def test_a_child_that_made_no_lines_sends_nothing_at_exit() -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1", eager=True)
    sender.client.request = None  # type: ignore[method-assign, assignment]  # any send would fail
    sender.flush()


def test_a_child_sends_again_at_exit_what_failed_the_first_time(server: ControlServer) -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1", eager=True)
    sender.put(line(1))
    assert sender.unsent == [line(1)]
    sender.client = ControlClient(server.url, TOKEN)
    sender.flush()
    assert server.store.lines("run-1") == [line(1)] and sender.unsent == []


def test_a_childs_settlement_goes_out_at_once(server: ControlServer) -> None:
    held = server.ledger.reserve(1.0)
    assert isinstance(held, int)
    sender = Sender(ControlClient(server.url, TOKEN), "run-1", eager=True)
    sender.put_settle(held, 0.25)
    assert not sender.settles


def test_a_forked_sender_starts_empty_with_new_locks(server: ControlServer) -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1", "the parent")
    sender.put(line(1))
    sender.forked("the fork")
    assert sender.queue.empty() and sender.made == 0 and sender.eager and sender.proc == "the fork"
    sender.client = ControlClient(server.url, TOKEN)
    sender.put(line(2))
    assert server.store.lines("run-1") == [line(2)]


def blocked_in(thread: threading.Thread) -> set[int]:
    """The signals a thread blocks, as Linux shows them: SigBlk is a hex mask, bit n-1."""
    status = Path(f"/proc/self/task/{thread.native_id}/status").read_text()
    mask = int(next(x for x in status.splitlines() if x.startswith("SigBlk:")).split()[1], 16)
    return {n for n in range(1, 65) if mask >> (n - 1) & 1}


@pytest.mark.design("stop.sdk_threads_deaf")
@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="reads Linux's /proc")
def test_the_sender_thread_blocks_the_stop_signals_and_its_creator_does_not() -> None:
    """The kernel can then give the runner's SIGTERM only to a thread that can run the
    handler, never to the SDK's own, which would leave the main thread stuck in its call."""
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    sender.start()
    try:
        assert {signal.SIGINT, signal.SIGTERM} <= blocked_in(sender._thread)  # type: ignore[reportPrivateUsage]
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before  # put back
        assert signal.SIGTERM not in blocked_in(threading.current_thread())
    finally:
        sender._stop.set()  # type: ignore[reportPrivateUsage]
        sender._thread.join(30)  # type: ignore[reportPrivateUsage]


@pytest.mark.design("stop.one_line_per_call")
def test_a_call_stopped_and_then_ended_has_one_line() -> None:
    lines: list[dict[str, object]] = []

    class Sink:
        def put(self, line: dict[str, object], *, keep: bool = False) -> None:
            del keep
            lines.append(line)

    run = state.RunState(a_plan(), Sink(), [])
    step = run.begin("a", {})
    run.stop_open()
    run.finish(step, result="late")
    assert len(lines) == 1 and "ScrimmageStopped" in str(lines[0]["error"])


@pytest.mark.design("boot.plan_failure_is_not_for_life")
def test_a_failed_plan_fetch_refuses_one_call_and_is_tried_again() -> None:
    answers = [no_plan("run-1", "the server was busy"), a_plan()]

    class Sink:
        def put(self, line: dict[str, object], *, keep: bool = False) -> None:
            del line, keep

    run = state.RunState(None, Sink(), [], load=lambda: answers.pop(0))
    first = run.begin("a", {})
    assert first.decision.reason == "no_plan" and run.refused is None
    assert "the server was busy" in str(run.refusal(first))
    assert run.begin("a", {}).decision.reason == "no_plan"  # too soon to ask again
    run._try_again_at = 0.0  # type: ignore[reportPrivateUsage]
    assert run.begin("a", {}).decision.action == "real"
    assert not answers


@pytest.mark.design("stop.daemon_poisoned")
def test_a_line_for_a_closed_run_poisons_the_process(server: ControlServer) -> None:
    run = state.RunState(
        a_plan(), Sender(ControlClient(server.url, TOKEN), "run-1", eager=True), []
    )
    run.sink.on_closed = run.run_closed  # type: ignore[attr-defined]
    server.close_run("run-1")
    run.begin("a", {})
    step = run.begin("a", {})
    run.finish(step)
    assert run.refused == state.CLOSED
    late = run.begin("a", {})
    assert late.decision.reason == "poisoned"
    assert "run has ended" in str(run.refusal(late))


def test_a_server_away_for_long_keeps_a_bounded_backlog(monkeypatch: pytest.MonkeyPatch) -> None:
    import scrimmage_core.client as client_module

    monkeypatch.setattr(client_module, "QUEUE_SIZE", 3)
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
    sender.unsent = [line(i) for i in range(1, 6)]
    sender.flush()
    assert len(sender.unsent) == 3 and sender.dropped == 2


@pytest.mark.design("record.keep_refusals")
def test_a_refusal_waits_for_room_instead_of_being_dropped(server: ControlServer) -> None:
    sender = Sender(ControlClient(server.url, TOKEN), "run-1")
    sender.queue.maxsize = 1
    sender.put(line(1))
    waiter = threading.Thread(target=sender.put, args=(line(2),), kwargs={"keep": True})
    waiter.start()
    time.sleep(0.2)
    assert waiter.is_alive()  # the queue is full, so the refusal waits ...
    sender.start()
    waiter.join(timeout=60)  # a ceiling: it returns once the sender makes room
    sender.flush()  # ... and is sent once there is room
    assert [x["seq"] for x in server.store.lines("run-1")] == [1, 2]
    assert server.runs["run-1"].dropped == 0


def test_a_refused_line_tells_the_runner_once(tmp_path: Path) -> None:
    told: list[str] = []
    control = ControlServer(
        TOKEN, RecordStore(tmp_path / "r"), on_refused=lambda run, _: told.append(run)
    )
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
        for _ in range(3000):  # the runner is told on a thread of its own
            if told:
                break
            time.sleep(0.01)
        assert told == ["run-1"]
    finally:
        control.stop()


@pytest.mark.design("protocol.window")
def test_a_request_more_than_a_minute_off_is_refused(server: ControlServer) -> None:
    for stamp in (time.time() - 120, float("nan")):
        headers = protocol.request_headers(TOKEN, "GET", "/v1/plans/run-1", b"", 0)
        headers[protocol.TIME] = f"{stamp:.3f}" if stamp == stamp else "nan"
        headers[protocol.SIGNATURE] = protocol.sign_request(
            TOKEN, "GET", "/v1/plans/run-1", headers[protocol.TIME], headers[protocol.NONCE], b""
        )
        error = raw(server, "GET", "/v1/plans/run-1", headers)[1]["error"]
        assert error == ("clock" if stamp == stamp else "bad_signature")


def test_a_bad_length_is_refused_and_an_idle_connection_is_let_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    import scrimmage_core.control as control_module

    monkeypatch.setattr(control_module, "READ_TIMEOUT_S", 0.5)
    control = ControlServer(TOKEN, RecordStore(tmp_path / "r"))
    control.start()
    try:
        for length in ("-1", "abc"):
            assert raw_status(control, {"Content-Length": length}) == 400
        host, port = control.server.server_address[:2]
        idle = socket.create_connection((str(host), port), timeout=30)
        assert idle.recv(1) == b""  # blocks until the server closes the silent connection
        idle.close()
    finally:
        control.stop()


def test_a_negative_dropped_count_is_ignored(server: ControlServer) -> None:
    client = ControlClient(server.url, TOKEN)
    client.request("POST", "/v1/records", {"run": "run-1", "lines": [], "dropped": -5})
    assert server.runs["run-1"].dropped == 0


def test_a_plan_that_cannot_be_sent_fails_when_the_run_opens(server: ControlServer) -> None:
    import datetime

    with pytest.raises(TypeError):
        server.open_run("run-2", "baseline", {"v": 1, "when": datetime.date(2026, 1, 1)})


def test_a_negative_made_count_is_refused(server: ControlServer) -> None:
    client = ControlClient(server.url, TOKEN)
    status, reply = client.request("POST", "/v1/records", {"run": "run-1", "lines": [], "made": -1})
    assert (status, reply["error"]) == (400, "bad_batch")


@pytest.mark.design("record.gate_never_waits")
def test_queueing_a_line_never_waits_for_a_batch_in_flight() -> None:
    arrived, release = threading.Event(), threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            arrived.set()
            release.wait(60)  # held until the test has queued its second line
            self.send_response(500)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    slow = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=slow.serve_forever, daemon=True).start()
    sender = Sender(ControlClient(f"http://127.0.0.1:{slow.server_address[1]}", TOKEN), "run-1")
    try:
        sender.start()
        sender.put(line(1))
        assert arrived.wait(60)  # the first batch is now waiting on the slow server
        second = threading.Thread(target=sender.put, args=(line(2),))
        second.start()
        second.join(30)
        # It returned while the batch was still held: a put that waited would block here.
        assert not second.is_alive() and not release.is_set()
    finally:
        release.set()
        # Stop the sender's thread, or it posts to a closed port for the rest of the session
        # and can fail an unrelated test later in the same worker.
        sender._stop.set()  # pyright: ignore[reportPrivateUsage]
        sender.queue.put_nowait(WAKE)
        sender._thread.join(30)  # pyright: ignore[reportPrivateUsage]
        slow.shutdown()
        slow.server_close()
    assert not sender._thread.is_alive()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.design("record.side_folder_owner_only")
def test_a_side_folder_item_is_stored_by_its_hash_for_its_owner(server: ControlServer) -> None:
    data = b"the full model reply"
    sha = hashlib.sha256(data).hexdigest()
    payload = {"run": "run-1", "sha256": sha, "data": base64.b64encode(data).decode()}
    client = ControlClient(server.url, TOKEN)
    assert client.request("POST", "/v1/blobs", payload)[0] == 200
    assert client.request("POST", "/v1/blobs", payload)[0] == 200  # the same item is kept once
    stored = server.store.folder / "blobs" / sha
    assert stored.read_bytes() == data
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"sha256": "0" * 64}, "bad_hash"),
        ({"sha256": "not a hash"}, "bad_hash"),
        ({"data": "not base64!"}, "bad_request"),
        ({"run": "run-9"}, "run_closed"),
    ],
)
def test_a_bad_side_folder_item_is_refused(
    server: ControlServer, change: dict[str, str], code: str
) -> None:
    data = b"x"
    payload = {
        "run": "run-1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": base64.b64encode(data).decode(),
        **change,
    }
    assert (
        ControlClient(server.url, TOKEN).request("POST", "/v1/blobs", payload)[1]["error"] == code
    )


@pytest.mark.design("record.sender_delivers_settles")
def test_the_sender_delivers_other_requests_and_retries_them(server: ControlServer) -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
    assert sender.put_blob(hashlib.sha256(b"x").hexdigest(), b"x")
    sender.flush()  # nobody listens: the item is kept
    assert len(sender.unsent_posts) == 1
    sender.client = ControlClient(server.url, TOKEN)
    sender.flush()
    assert sender.unsent_posts == []
    assert len(list((server.store.folder / "blobs").iterdir())) == 1


def test_a_full_side_folder_queue_says_so() -> None:
    sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
    sender.posts.maxsize = 1
    assert sender.put_blob("a" * 64, b"x") is True
    assert sender.put_blob("b" * 64, b"x") is False


@pytest.mark.design("budget.settles_never_dropped")
def test_a_settlement_is_never_crowded_out_and_survives_a_failed_send(tmp_path: Path) -> None:
    ledger = Ledger(1.0)
    control = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=ledger)
    control.start()
    control.open_run("run-1", "baseline", PLAN)
    try:
        held = ledger.reserve(0.5)
        assert isinstance(held, int)
        sender = Sender(ControlClient("http://127.0.0.1:9", TOKEN), "run-1")
        sender.posts.maxsize = 1
        sender.put_blob("a" * 64, b"x")  # the side-folder queue is full
        sender.put_settle(held, 0.1)
        sender.flush()  # nobody listens: the settlement is kept
        assert len(sender.settles) == 1
        sender.client = ControlClient(control.url, TOKEN)
        sender.flush()
        assert not sender.settles
        assert (ledger.reserved, ledger.spent) == (0.0, 0.1)
    finally:
        control.stop()


@pytest.mark.design("control.quiet_on_dropped_clients")
def test_a_client_that_drops_its_connection_prints_no_traceback(
    server: ControlServer, capsys: pytest.CaptureFixture[str]
) -> None:
    for error in (BrokenPipeError(), ConnectionResetError()):
        try:
            raise error
        except ConnectionError:
            server.server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""
    try:
        raise ValueError("a real bug")
    except ValueError:
        server.server.handle_error(None, ("127.0.0.1", 1))
    assert "a real bug" in capsys.readouterr().err


def test_a_burst_of_settlements_all_arrive(tmp_path: Path) -> None:
    ledger = Ledger(100.0)
    control = ControlServer(TOKEN, RecordStore(tmp_path / "r"), ledger=ledger)
    control.start()
    control.open_run("run-1", "baseline", PLAN)
    try:
        sender = Sender(ControlClient(control.url, TOKEN), "run-1")
        held = [ledger.reserve(0.5) for _ in range(200)]
        for reservation in held:
            assert isinstance(reservation, int)
            sender.put_settle(reservation, 0.5)
        sender.flush()
        assert (ledger.reserved, ledger.spent) == (0.0, 100.0)
    finally:
        control.stop()


class Died(BaseException):
    """Stands for the process ending: the test's os.kill raises it."""


def test_a_sigterm_inside_the_exit_drain_loses_no_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler runs on the thread that holds the send lock, and the worker it starts to
    send the last lines needs that lock. The drain must end first, then the process."""
    from scrimmage_core import bootstrap

    delivered: list[dict[str, object]] = []
    finals: list[object] = []

    class Stub(ControlClient):
        def __init__(self) -> None:
            super().__init__("http://127.0.0.1:1", TOKEN)
            self.first = True

        def request(
            self,
            method: str,
            path: str,
            payload: object = None,
            *,
            held: bool = False,
            timeout: float = 10.0,
        ) -> tuple[int, dict[str, object]]:
            del held, timeout
            if self.first:
                self.first = False
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)  # the signal arrives inside the first send
            batch = payload if isinstance(payload, dict) else {}
            delivered.extend(batch["lines"])
            if "made" in batch:
                finals.append(batch["made"])
            return 200, {}

    def kill(pid: int, signum: int) -> None:
        raise Died

    monkeypatch.setattr(bootstrap, "TERM_WAIT_S", 0.2)  # a stalled worker fails fast, not slow
    monkeypatch.setattr(os, "kill", kill)
    before = signal.getsignal(signal.SIGTERM)
    sender = Sender(Stub(), "run-1")
    for seq in range(1, 701):
        sender.put(line(seq))
    bootstrap.handle_sigterm(sender.flush, sender)
    try:
        with pytest.raises(Died):
            sender.flush()
    finally:
        signal.signal(signal.SIGTERM, before)
    assert sorted({int(str(entry["seq"])) for entry in delivered}) == list(range(1, 701))
    assert finals and finals[0] == 700
