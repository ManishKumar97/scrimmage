"""The plan digest: the claiming process says which plan its run's state used, in its last
batch, and the runner compares it with its own plan. A different plan is an infrastructure
error, and a claimed run's last batch with no digest is refused."""

import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import importlib.util
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import EXAMPLE
from scrimmage import protocol, runner, versions
from scrimmage.config import Mock
from scrimmage.control import ControlServer
from scrimmage.plan import Plan
from scrimmage.records import RecordStore
from test_service import Bench, bench

pytestmark = pytest.mark.slow
__all__ = ["bench"]  # the fixture, used by name

TOKEN = "f00d" * 16


def a_plan(**more: Any) -> Plan:
    mocks = (Mock("tool:get_order", "shipped, Oct 30", None),)
    fields: dict[str, Any] = {"mocks": mocks, "faults": (), "tools": (), "real": ()}
    fields.update(more)
    return Plan("run-0000000000aa", "t", "baseline", time_limit_s=30.0, **fields)


def text_of(plan: Plan) -> str:
    doc = {k: v for k, v in plan.to_json().items() if k not in ("elapsed_s", "scrub")}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def the_plans_sent(bench: Bench) -> dict[str, dict[str, object]]:
    """Each plan the runner opened a run with, by run: the runner's own, before any claim."""
    sent: dict[str, dict[str, object]] = {}
    opened = ControlServer.open_run

    def record(self: ControlServer, run: str, row: str, plan: dict[str, object], **kw: Any) -> Any:
        sent[run] = dict(plan)
        return opened(self, run, row, plan, **kw)

    bench.monkeypatch.setattr(ControlServer, "open_run", record)
    return sent


class Hold:
    """The test's end of /chat-held. A request is held until two held requests come from two
    workers. A held worker's loop accepts nothing more, so the next new connection reaches the
    other worker; but a worker can accept several connections before its first one blocks, so
    a whole batch can still land on one worker, and the test below sends it again. A request
    alone for a second, such as the setup request, goes on."""

    def __init__(self, path: Path) -> None:
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen(64)
        self.lock = threading.Lock()
        self.held: dict[socket.socket, int] = {}
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.hold, args=(conn,), daemon=True).start()

    def hold(self, conn: socket.socket) -> None:
        pid = int(conn.makefile().readline())
        with self.lock:
            self.held[conn] = pid
            both = len(set(self.held.values())) >= 2
        if both:
            self.release()
        else:
            threading.Timer(1.0, self.release).start()

    def release(self) -> None:
        with self.lock:
            held, self.held = self.held, {}
        for conn in held:
            with contextlib.suppress(OSError):
                conn.sendall(b"g")
            conn.close()

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def hold(tmp_path: Path) -> Iterator[Hold]:
    made = Hold(tmp_path / "hold.sock")
    yield made
    made.close()


def token_of(bench: Bench) -> str:
    return bench.token_file.read_text()


def signed(server: ControlServer, path: str, doc: dict[str, object]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(doc).encode()
    headers = protocol.request_headers(server.token, "POST", path, body, time.time())
    status, reply = server.handle("POST", path, headers, body)
    return status, dict(reply)


def runs_of(bench: Bench) -> dict[str, dict[str, Any]]:
    return {n: s for n, s in bench.summaries().items() if n.startswith("run-")}


@pytest.mark.design("service.plan_digest")
def test_the_digest_is_an_hmac_over_to_json_without_elapsed_or_scrub() -> None:
    plain = a_plan()
    timed = a_plan(elapsed_s=12.5)
    scrubbed = a_plan(scrub=("a-secret-value",))
    both = a_plan(elapsed_s=3.0, scrub=("a-secret-value",))
    digest = protocol.plan_digest(TOKEN, plain.to_json())
    assert "elapsed_s" in timed.to_json() and "scrub" in scrubbed.to_json()
    for other in (timed, scrubbed, both):
        assert protocol.plan_digest(TOKEN, other.to_json()) == digest
    # It is the HMAC-SHA256 of that text under the token, and nothing else.
    expected = hmac.new(TOKEN.encode(), text_of(plain).encode(), hashlib.sha256).hexdigest()
    assert digest == expected
    assert protocol.plan_digest("0ddba11" * 9, plain.to_json()) != digest
    changed = a_plan(mocks=(Mock("tool:get_order", "lost", None),))
    assert protocol.plan_digest(TOKEN, changed.to_json()) != digest
    # The argument is not changed: the same dict is digested again with its keys in place.
    doc = both.to_json()
    protocol.plan_digest(TOKEN, doc)
    assert "elapsed_s" in doc and "scrub" in doc


SECRET_MOCK = """
[[test]]
name     = "scrubbed"
question = "Where is order 1234?"
expect   = ["shipped"]

[[test.mock]]
call    = "tool:get_order"
returns = "${E2_ORDER_STATUS}, shipped"
"""
SECRET = "e2-a-value-only-the-environment-holds"


@pytest.mark.design("service.plan_digest")
def test_the_stored_digest_does_not_reveal_a_scrubbed_value(bench: Bench) -> None:
    """A mock holds a ${NAME} value, which the plan lists in scrub. Someone who reads the
    record and guesses the value right still cannot confirm it with a hash they make."""
    bench.monkeypatch.setenv("E2_ORDER_STATUS", SECRET)
    sent = the_plans_sent(bench)
    service = bench.start()
    bench.test(service.port, runs=1, tests=SECRET_MOCK)
    mine = {n: s for n, s in runs_of(bench).items() if sent[n]["test"] == "scrubbed"}
    assert len(mine) == 1
    ((name, summary),) = mine.items()
    plan = sent[name]
    assert plan["scrub"] == [SECRET]  # the value is listed to be scrubbed ...
    assert SECRET in json.dumps(plan["mocks"])  # ... and is in the text a digest covers
    kept = {k: v for k, v in plan.items() if k not in ("elapsed_s", "scrub")}
    shown = json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    stored = summary["plan_digest"]
    assert SECRET in shown
    assert stored != hashlib.sha256(shown.encode()).hexdigest()  # the right guess, plain hash
    assert stored == protocol.plan_digest(token_of(bench), plan)  # the token confirms it


SPREAD_TRIES = 12  # a batch landed on one worker 3 times in 8, so 12 tries fail 1 in 10**5


@pytest.mark.design("service.plan_digest")
def test_the_claimer_reports_the_digest_of_the_plan_its_state_used(
    bench: Bench, hold: Hold
) -> None:
    """Both workers must serve runs for this to test anything. With parallel runs one worker's
    loop can accept several connections before the hold blocks it, so a batch of 20 can land
    on one worker. The batch is sent again, at most SPREAD_TRIES times, until both have served;
    every run of every batch must pass every check."""
    sent = the_plans_sent(bench)
    service = bench.start(workers=2, HOLD_SOCKET=str(hold.server.getsockname()))
    workers = set(service.workers())
    assert len(workers) == 2, service.text
    pids: set[int] = set()
    tries = 0
    while pids != workers and tries < SPREAD_TRIES:
        tries += 1
        code, out = bench.test(service.port, "/chat-held", runs=20, parallel=4)
        assert code == 0, out
        runs = runs_of(bench)
        assert len(runs) == 20 * tries
        for name, summary in runs.items():
            assert summary["outcome"] == "answered", name
            expected = protocol.plan_digest(token_of(bench), sent[name])
            assert summary["plan_digest"] == expected, name
            assert summary["claimed_plan_digest"] == expected, name
        assert all(s["claimed_by"]["host"] for s in runs.values())
        pids = {s["claimed_by"]["pid"] for s in runs.values()}
        assert pids <= workers, (pids, workers)
    assert pids == workers, (pids, workers, tries)


@pytest.mark.design("service.plan_digest")
def test_a_run_whose_state_changed_its_plan_is_an_infrastructure_error(bench: Bench) -> None:
    service = bench.start(SCRIMMAGE_TEST_CHANGE_PLAN="1")
    code, out = bench.test(service.port, runs=2)
    runs = runs_of(bench)
    assert len(runs) == 2
    for name, summary in runs.items():
        assert summary["claimed_plan_digest"] != summary["plan_digest"], name
        assert summary["claimed_plan_digest"] is not None, name
        assert summary["outcome"] == "infrastructure error", name
        notices = [x["notice"] for x in bench.lines(name) if "notice" in x]
        assert notices == ["plan_changed"], name
    assert code == 3, out
    assert out.count(runner.PLAN_CHANGED) == 2, out


@pytest.mark.design("service.plan_digest")
def test_a_claimed_runs_last_batch_with_no_digest_is_refused(bench: Bench, tmp_path: Path) -> None:
    """A claimer that leaves out its digest has its last batch refused as a bad batch, so the
    run is never taken as answered with a plan no one can check."""
    server = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    try:
        for digest in (None, "ab" * 32):
            run = f"run-{'00' if digest is None else 'ff'}00000000aa"
            server.open_run(run, "baseline", {"run": run}, service=True)
            claim = {"run": run, "proc": {"pid": 1}, "sdk": versions.installed()}
            assert signed(server, "/v1/claims", claim)[0] == 200
            last: dict[str, object] = {"run": run, "lines": [], "made": 0, "proc": {"pid": 1}}
            if digest is not None:  # positive control: the same batch with a digest is taken
                last["plan_digest"] = digest
            status, reply = signed(server, "/v1/records", last)
            if digest is None:
                assert (status, reply) == (400, {"error": "bad_batch"})
                assert not server.runs[run].ended.is_set()
            else:
                assert status == 200, reply
                assert server.runs[run].claimed_plan_digest == digest
    finally:
        server.server.server_close()

    served = ControlServer._records  # pyright: ignore[reportPrivateUsage]

    def undigested(self: ControlServer, body: bytes) -> Any:
        batch = json.loads(body)
        batch.pop("plan_digest", None)
        return served(self, json.dumps(batch).encode())

    bench.monkeypatch.setattr(ControlServer, "_records", undigested)
    service = bench.start()
    # The refused last batch never comes, so each run waits it out: a short wait.
    code, out = bench.test(service.port, runs=2, wait=2.0)
    runs = runs_of(bench)
    assert len(runs) == 2
    for name, summary in runs.items():
        assert summary["claimed_plan_digest"] is None, name
        assert summary["outcome"] == "infrastructure error", name
    assert code == 3, out


@pytest.mark.design("service.plan_digest")
def test_the_runner_digests_its_own_plan_not_the_claimers(bench: Bench) -> None:
    sent = the_plans_sent(bench)
    service = bench.start(SCRIMMAGE_TEST_CHANGE_PLAN="1")
    bench.test(service.port, runs=2)
    runs = runs_of(bench)
    assert len(runs) == 2
    for name, summary in runs.items():
        own = protocol.plan_digest(token_of(bench), sent[name])
        assert summary["plan_digest"] == own, name
        assert summary["claimed_plan_digest"] != own, name


# Every key Plan.to_json() can write, and every Plan field, as they are now.
DIGESTED_KEYS = {
    "v", "run", "test", "row", "mocks", "faults", "tools", "real", "children", "time_limit_s",
    "model_hosts", "budget", "renames", "record", "probe", "service", "record_grace_s",
    "background_real",
}  # fmt: skip
PLAN_FIELDS = {
    "run", "test", "row", "mocks", "faults", "tools", "real", "time_limit_s", "renames",
    "model_hosts", "budget_usd", "reserve_output_tokens", "prices", "file_prices", "file_fees",
    "service_tier",
    "children",
    "unavailable",
    "record", "probe", "elapsed_s", "scrub", "service", "record_grace_s", "background_real",
}  # fmt: skip


@pytest.mark.design("service.plan_digest")
def test_a_new_plan_key_must_be_digested_or_listed_as_undigested() -> None:
    full = a_plan(
        renames=(("acme.tools:get_order", "order"),),
        record=True,
        probe=True,
        elapsed_s=1.0,
        scrub=("x",),
        service=True,
        background_real=("tool:x",),
    )
    kept = set(full.to_json()) - set(protocol.UNDIGESTED)
    fields = {f.name for f in dataclasses.fields(Plan)}
    advice = (
        "Plan changed. A new key is digested unless it is listed in protocol.UNDIGESTED, "
        "as it should be if it is like elapsed_s and scrub; choose, then update this test's "
        "sets."
    )
    assert kept == DIGESTED_KEYS, (advice, sorted(kept ^ DIGESTED_KEYS))
    assert fields == PLAN_FIELDS, (advice, sorted(fields ^ PLAN_FIELDS))


@pytest.mark.design("service.plan_digest")
def test_the_seam_fails_loudly_for_a_plan_with_no_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    from scrimmage import asgi_gate

    spec = importlib.util.spec_from_file_location("plan_seam", EXAMPLE / "acme" / "plan_seam.py")
    assert spec is not None and spec.loader is not None
    plan_seam = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plan_seam)
    nothing: Any = None

    async def claim(gate: Any, name: str) -> Any:
        return SimpleNamespace(plan=a_plan(mocks=()), _plan=None), None

    monkeypatch.setenv(plan_seam.ENV, "1")
    monkeypatch.setattr(asgi_gate, "claimed", claim)
    plan_seam.install()
    with pytest.raises(RuntimeError, match="needs a plan with a mock"):
        asyncio.run(asgi_gate.claimed(nothing, "run-0000000000aa"))
