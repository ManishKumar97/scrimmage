"""The plan digest: the claiming process says which plan its run's state used, in its last
batch, and the runner compares it with its own plan. A different plan is an infrastructure
error, and a last batch from an older SDK with no digest is not compared."""

import contextlib
import hashlib
import hmac
import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scrimmage_core import protocol, runner
from scrimmage_core.config import Mock
from scrimmage_core.control import ControlServer
from scrimmage_core.plan import Plan
from test_service import Bench, bench

pytestmark = pytest.mark.slow
__all__ = ["bench"]  # the fixture, used by name

TOKEN = "f00d" * 16


def a_plan(**more: Any) -> Plan:
    mocks = (Mock("tool:get_order", "shipped, Oct 30", None),)
    return Plan("run-0000000000aa", "t", "baseline", mocks, (), (), (), 30.0, **more)


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
    workers, so both workers serve runs whatever order the kernel hands out connections in.
    A request alone for a second, such as the setup request, goes on."""

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
    changed = Plan(**{**plain.__dict__, "mocks": (Mock("tool:get_order", "lost", None),)})
    assert protocol.plan_digest(TOKEN, changed.to_json()) != digest
    # The argument is not changed: the same dict is digested again with its keys in place.
    doc = both.to_json()
    protocol.plan_digest(TOKEN, doc)
    assert "elapsed_s" in doc and "scrub" in doc


@pytest.mark.design("service.plan_digest")
def test_the_stored_digest_does_not_reveal_a_scrubbed_value(bench: Bench) -> None:
    """The record holds the digest. Someone who reads it and guesses a mock's value right
    still cannot confirm the guess with a hash they make themselves."""
    sent = the_plans_sent(bench)
    service = bench.start()
    code, out = bench.test(service.port, runs=1)
    assert code == 0, out
    (summary,) = runs_of(bench).values()
    (plan,) = [p for p in sent.values() if p["run"] in runs_of(bench)]
    guess = dict(plan)
    assert "shipped, Oct 30" in json.dumps(guess)  # the right guess is in the text hashed
    shown = json.dumps(guess, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    stored = summary["plan_digest"]
    assert stored != hashlib.sha256(shown.encode()).hexdigest()
    assert stored == protocol.plan_digest(token_of(bench), plan)  # and the token confirms it


@pytest.mark.design("service.plan_digest")
def test_the_claimer_reports_the_digest_of_the_plan_its_state_used(
    bench: Bench, hold: Hold
) -> None:
    sent = the_plans_sent(bench)
    service = bench.start(workers=2, HOLD_SOCKET=str(hold.server.getsockname()))
    workers = set(service.workers())
    assert len(workers) == 2, service.text
    code, out = bench.test(service.port, "/chat-held", runs=20, parallel=4)
    assert code == 0, out
    runs = runs_of(bench)
    assert len(runs) == 20
    for name, summary in runs.items():
        assert summary["outcome"] == "answered", name
        expected = protocol.plan_digest(token_of(bench), sent[name])
        assert summary["plan_digest"] == expected, name
        assert summary["claimed_plan_digest"] == expected, name
    pids = {s["claimed_by"]["pid"] for s in runs.values()}
    assert pids == workers, (pids, workers)
    assert all(s["claimed_by"]["host"] for s in runs.values())


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
def test_a_last_batch_with_no_digest_is_not_compared_and_says_so(bench: Bench) -> None:
    """The wire form of a last batch from an older SDK of the same major: no plan_digest."""
    served = ControlServer._records  # pyright: ignore[reportPrivateUsage]

    def older(self: ControlServer, body: bytes) -> Any:
        batch = json.loads(body)
        batch.pop("plan_digest", None)
        return served(self, json.dumps(batch).encode())

    bench.monkeypatch.setattr(ControlServer, "_records", older)
    service = bench.start(
        SCRIMMAGE_TEST_CHANGE_PLAN="1"
    )  # a changed plan would differ, if it were compared
    code, out = bench.test(service.port, runs=3)
    runs = runs_of(bench)
    assert len(runs) == 3
    for name, summary in runs.items():
        assert summary["claimed_plan_digest"] is None, name
        assert summary["plan_digest"], name
        assert summary["outcome"] == "answered", name
        notices = [x["notice"] for x in bench.lines(name) if "notice" in x]
        assert notices == ["plan_unreported"], name
    assert code == 0, out
    assert out.count(runner.PLAN_UNREPORTED) == 1, out  # once for the test, not once a run


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
