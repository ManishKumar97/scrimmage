"""A service with workers, under load: every run lands in its own record, the control server's
64 connections are shared, each process sends at most 8 control requests at once, and a
flood of forged headers never reaches the control server.

Each test starts real uvicorn processes and checks by pid that none is left.
"""

import http.client
import json
import secrets
import socket
import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scrimmage import asgi_gate, protocol
from scrimmage.client import ControlClient
from scrimmage.config import Mock
from scrimmage.control import MAX_CONNECTIONS, ControlServer
from scrimmage.records import RecordStore
from service_harness import eventually
from test_service import Bench, Lab, bench, lab

pytestmark = pytest.mark.slow
__all__ = ["bench", "lab"]  # the fixtures, used by name
PER_PROCESS = 8  # the design's number, not the code's, so a change to it fails here


@pytest.mark.design("service.workers_spread")
def test_runs_reach_four_worker_pids_and_every_record_matches_its_plan(
    lab: Lab, tmp_path: Path
) -> None:
    """Four runs, each sent once the run before it holds its worker: a held worker accepts no
    connection, so each run reaches a worker no run holds yet, whatever order the workers
    accept in. Then all four go on, at once, and each record holds only that run's lines."""
    from conftest import ACME
    from fakes.openai_server import FakeOpenAI

    hold = tmp_path / "hold.sock"
    fake = FakeOpenAI(ACME)
    fake.start()
    waiting = socket.socket(socket.AF_UNIX)
    waiting.bind(str(hold))
    waiting.listen(4)
    waiting.settimeout(60)
    held: list[socket.socket] = []
    try:
        service = lab.start(
            workers=4, OPENAI_BASE_URL=fake.base_url, OPENAI_API_KEY="test", HOLD_SOCKET=str(hold)
        )
        workers = set(service.workers())
        assert len(workers) == 4, service.text
        mocks = (
            Mock("tool:get_order", "shipped, Oct 30", None),
            Mock("tool:get_stock", {"sku": "SKU-42", "count": 12}, "acme.models:Stock"),
        )
        # The openai client runs `uname -p` through platform to name its machine: listed.
        real = ("tool:check_inventory", "process:uname")
        runs = [lab.open(mocks=mocks, real=real) for _ in range(4)]
        reach: dict[str, int] = {}
        with ThreadPoolExecutor(4) as sending:
            replies = []
            for name, headers in runs:
                body = {"message": "Where is order 1234?"}
                replies.append(sending.submit(service.post, "/chat-held", body, headers))
                conn, _ = waiting.accept()  # this run's worker holds it, and its loop
                held.append(conn)
                reach[name] = int(conn.makefile().readline())
            for conn in held:
                conn.sendall(b"g")
            answered = [reply.result(60) for reply in replies]
        assert all(status == 200 for status, _ in answered), (answered, service.text[-3000:])
        assert set(reach.values()) == workers, reach
        for name, _ in runs:
            lines = lab.lines(name)
            # Every line is the run's own, from the worker its request reached, with the
            # row of the plan opened for it.
            assert lines and {x["run"] for x in lines} == {name}, name
            assert {x.get("proc", {}).get("pid") for x in lines} == {reach[name]}, name
            assert {x["row"] for x in lines} == {"baseline"}, name
    finally:
        for conn in held:
            conn.close()
        waiting.close()
        fake.stop()


@pytest.mark.design("service.workers_records")
def test_scrimmage_test_with_four_parallel_runs_on_four_workers_keeps_each_record_whole(
    bench: Bench,
) -> None:
    """The CLI's path: scrimmage test, four runs at once, against four workers. Wherever each
    run lands, every line of its record carries its run, its test and its plan's row."""
    service = bench.start(workers=4)
    workers = set(service.workers())
    assert len(workers) == 4, service.text
    code, out = bench.test(service.port, runs=4, parallel=4)
    assert code == 0, out
    records = {p.stem: p for p in (bench.root / "scrimmage-runs").glob("*/*.jsonl")}
    assert sum(name.startswith("run-") for name in records) == 4, sorted(records)
    for name, path in records.items():
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        assert lines and {(x["run"], x["test"]) for x in lines} == {(name, "order-status")}, name
        served = [x for x in lines if x.get("proc", {}).get("pid") in workers]
        assert served and {x["row"] for x in served} == {"baseline"}, name


@pytest.mark.design("service.sixty_fifth_connection")
def test_a_sixty_fifth_control_connection_waits_and_is_served(tmp_path: Path) -> None:
    token = secrets.token_hex(32)
    server = ControlServer(token, RecordStore(tmp_path / "records"))
    server.start()
    host, port = server.server.server_address[:2]
    held: list[socket.socket] = []
    try:
        server.open_run("run-0123456789ab", "baseline", {"run": "run-0123456789ab"}, service=True)
        for _ in range(MAX_CONNECTIONS):  # each holds a thread: a request that never ends
            held.append(socket.create_connection((str(host), int(port))))
            held[-1].sendall(b"POST /v1/claims HTTP/1.1\r\nHost: x\r\n")
        eventually(lambda: server.server.slots._value == 0)  # type: ignore[attr-defined]
        client = ControlClient(server.url, token)
        with ThreadPoolExecutor(1) as pool:
            claim = pool.submit(client.claim, "run-0123456789ab", {"pid": 1})
            # Every thread is held, so the claim waits in the accept queue. Waiting is what
            # this checks, so the wait is a time, well under the client's 10 s.
            threading.Event().wait(0.5)
            assert not claim.done()
            held.pop().close()  # one thread frees, and the waiting connection is served
            plan = claim.result(10)
            assert isinstance(plan, dict) and plan["run"] == "run-0123456789ab"
    finally:
        for conn in held:
            conn.close()
        server.stop()


@pytest.mark.design("service.eight_per_process")
def test_each_process_sends_at_most_eight_control_requests_at_once(lab: Lab) -> None:
    service = lab.start()
    inflight, most, lock = [0], [0], threading.Lock()
    go = threading.Event()
    handle = lab.server.handle

    def watched(method: str, path: str, *args: Any) -> Any:
        with lock:
            inflight[0] += 1
            most[0] = max(most[0], inflight[0])
        try:
            if path == "/v1/claims":
                go.wait(30)  # the runner is slow: every claim waits here
            return handle(method, path, *args)
        finally:
            with lock:
                inflight[0] -= 1

    lab.server.handle = watched  # type: ignore[method-assign]
    runs = [lab.open() for _ in range(3 * PER_PROCESS)]
    with ThreadPoolExecutor(len(runs)) as pool:
        replies = [pool.submit(service.post, "/echo", None, headers) for _, headers in runs]
        # The 8 that took a slot hold it; the rest find none on the loop and are refused.
        eventually(lambda: sum(f.done() for f in replies) == len(runs) - PER_PROCESS, within=30)
        assert inflight[0] == PER_PROCESS
        go.set()
        statuses = [f.result(30)[0] for f in replies]
    assert most[0] == PER_PROCESS, most
    assert statuses.count(200) == PER_PROCESS and statuses.count(503) == len(runs) - PER_PROCESS


def flood(port: int, rate: float, headers: Callable[[], dict[str, str]], path: str) -> Any:
    """Requests at a steady rate on threads of their own, until stopped."""
    stop = threading.Event()
    sent, done, began, counting = [0], [0], time.monotonic(), threading.Lock()
    opened: list[http.client.HTTPConnection] = []
    local = threading.local()  # each sender keeps its connection open, as a client would

    def one() -> None:
        try:
            conn = getattr(local, "conn", None)
            if conn is None:
                conn = local.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                with counting:
                    opened.append(conn)
            conn.request("POST", path, body=b"{}", headers=headers())
            conn.getresponse().read()
            with counting:
                done[0] += 1
        except (OSError, http.client.HTTPException):
            local.conn.close()
            local.conn = None

    def pace() -> None:
        pool = ThreadPoolExecutor(16)
        try:
            while not stop.is_set():
                pool.submit(one)
                sent[0] += 1
                # A rate is what this keeps, so the wait is a time.
                stop.wait(max(0.0, began + sent[0] / rate - time.monotonic()))
        finally:
            pool.shutdown(cancel_futures=True)  # what was not sent yet is not sent
            for conn in opened:
                conn.close()

    thread = threading.Thread(target=pace, daemon=True)
    thread.start()
    return SimpleNamespace(
        stop=stop, thread=thread, rate=lambda: done[0] / (time.monotonic() - began)
    )


def plain_p99(port: int, count: int = 200) -> float:
    """The 99th percentile of plain requests' round trips, in ms."""
    times: list[float] = []
    for _ in range(count):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        began = time.perf_counter()
        conn.request("POST", "/echo", body=b"{}", headers={"content-type": "application/json"})
        conn.getresponse().read()
        times.append((time.perf_counter() - began) * 1000)
        conn.close()
    return statistics.quantiles(times, n=100)[98]


def forged() -> dict[str, str]:
    name = f"run-{secrets.token_hex(6)}"
    return {"x-scrimmage-run": f"{name}.{secrets.token_hex(8)}"}  # a MAC no token made


@pytest.mark.design("service.flood")
def test_a_header_flood_turns_runs_into_nothing_worse_than_infrastructure_errors(
    lab: Lab,
) -> None:
    from conftest import ACME
    from fakes.openai_server import FakeOpenAI

    fake = FakeOpenAI(ACME)
    fake.start()
    counted: list[str] = []  # the run each control request names
    handle = lab.server.handle

    def counting(method: str, path: str, headers: Any, body: bytes) -> Any:
        try:
            doc = json.loads(body) if body else {}
        except ValueError:
            doc = {}
        named = doc.get("run") if isinstance(doc, dict) else None
        counted.append(str(named or path.rpartition("/")[2]))
        return handle(method, path, headers, body)

    lab.server.handle = counting  # type: ignore[method-assign]
    try:
        service = lab.start(OPENAI_BASE_URL=fake.base_url, OPENAI_API_KEY="test")
        base = flood(service.port, 500, dict, "/no-such-path")  # the same load, no header
        try:
            baseline = plain_p99(service.port)
            base_rate = base.rate()
        finally:
            base.stop.set()
            base.thread.join()
        forgery = flood(service.port, 500, forged, "/echo")
        try:
            during = plain_p99(service.port)
            forged_rate = forgery.rate()
            assert counted == []  # the flood alone sent the control server nothing
            mocks = (
                Mock("tool:get_order", "shipped, Oct 30", None),
                Mock("tool:get_stock", {"sku": "SKU-42", "count": 12}, "acme.models:Stock"),
            )
            opened = [lab.open(mocks=mocks, real=("process:uname",)) for _ in range(4)]
            with ThreadPoolExecutor(4) as pool:
                asks = [
                    pool.submit(service.post, "/chat", {"message": "Where is order 1234?"}, h)
                    for _, h in opened
                ]
                replies = [f.result(60) for f in asks]
        finally:
            forgery.stop.set()
            forgery.thread.join()
    finally:
        fake.stop()
    assert all(status == 200 for status, _ in replies), replies
    assert all("Oct 30" in str(reply) for _, reply in replies), replies
    # Every control request named an opened run: none came from a forged header.
    names = {name for name, _ in opened}
    assert all(lab.lines(name) for name in names)
    assert counted and set(counted) <= names, set(counted) - names
    print(
        f"plain p99: {baseline:.1f} ms under a header-free flood at {base_rate:.0f}/s, "
        f"{during:.1f} ms under a forged one at {forged_rate:.0f}/s"
    )


@pytest.mark.design("service.mac_cost")
def test_the_mac_check_costs_microseconds() -> None:
    token = secrets.token_hex(32)
    gate = cast(asgi_gate.Gate, SimpleNamespace(token=token))
    name = "run-0123456789ab"
    headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"user-agent", b"python-httpx/0.28"),
        (b"accept", b"*/*"),
        (b"content-type", b"application/json"),
        (b"content-length", b"64"),
        (b"authorization", b"Bearer abc"),
        (asgi_gate.RUN, f"{name}.{protocol.run_mac(token, name)}".encode()),
    ]
    rounds = 100_000
    began = time.perf_counter()
    for _ in range(rounds):
        runs = [value for key, value in headers if key.lower() == asgi_gate.RUN]
        asgi_gate.checked_name(gate, runs)
    mean_us = (time.perf_counter() - began) / rounds * 1e6
    print(f"header parse and MAC check: {mean_us:.2f} µs a request")
    assert mean_us < 50
