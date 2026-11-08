import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fakes.openai_server import FakeOpenAI
from scrimmage_core import runner
from scrimmage_core.cli import main
from scrimmage_core.runner import OK, ROW_FAILED

EXAMPLE = Path(__file__).parent / "example"
# The S1 conversation: the bot asks for the order, then the stock, then answers, or says it
# could not if a tool failed.
ACME = ["acme_get_order", "acme_get_stock", "acme_answer|acme_admits"]
EXAMPLE_FAULT = (
    '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'
    'expect = [["unable", "sorry", "try again"]]\nforbid = ["Oct 30", "October 30"]\n'
)


# Audit events for a new process. A test that makes one is in the slow tier.
PROCESS_STARTS = frozenset(
    {"subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.spawn"}
)
started: list[str] = []


def note_process_start(event: str, args: tuple[object, ...]) -> None:
    del args
    if event in PROCESS_STARTS:
        started.append(event)


sys.addaudithook(note_process_start)


@pytest.fixture(autouse=True)
def fast_tests_start_no_process(request: pytest.FixtureRequest) -> Iterator[None]:
    """A test not marked slow fails if it starts a process, so the fast tier stays fast."""
    started.clear()
    yield
    if started and request.node.get_closest_marker("slow") is None:
        pytest.fail(f"this test starts a process ({started[0]}), so mark it slow")


def pytest_sessionfinish(session: pytest.Session) -> None:
    """In CI a skipped test fails the run: a safety check that did not run proves nothing.

    A skip is fine on a laptop without Ubuntu's Python; CI must have everything.
    Under xdist only the controller has a terminal reporter, and it gets every worker's
    skips, so the check runs there and a worker's session does nothing.
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    if os.environ.get("CI") and skipped:
        names = ", ".join(report.nodeid for report in skipped)
        print(f"\nskipped tests are not allowed in CI: {names}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def signals_kept() -> Iterator[None]:
    """The test leaves SIGINT and SIGTERM as it found them, as scrimmage run in-process must."""
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    yield
    after = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    assert after == before, f"signal handlers changed: {before} -> {after}"


@dataclass
class TimeLimit:
    """When a run's time limit arrives, and whether each run reached it.

    The event is something the agent does once it is where the limit should find it,
    such as writing a marker file. The run's real run_timeout stays as a ceiling, so a
    test whose event never comes still ends.
    """

    event: Callable[[], bool] = lambda: False
    reached: list[bool] = field(default_factory=list[bool])
    given: list[float] = field(default_factory=list[float])  # the limit the runner passed

    def at(self, event: Callable[[], bool]) -> None:
        self.event = event

    def wait(self, proc: subprocess.Popen[bytes], seconds: float) -> bool:
        self.given.append(seconds)
        ceiling = time.monotonic() + seconds
        while proc.poll() is None:
            if self.event() or time.monotonic() > ceiling:
                self.reached.append(proc.poll() is None)
                return self.reached[-1]
            time.sleep(0.01)
        self.reached.append(False)
        return False


@pytest.fixture
def time_limit(monkeypatch: pytest.MonkeyPatch) -> TimeLimit:
    """The time limit of each run in this test arrives at an event, not on the clock.

    A limit of a few seconds races the program's start-up, which takes longer under load.
    """
    limit = TimeLimit()
    monkeypatch.setattr(runner, "wait_run", limit.wait)
    return limit


@pytest.fixture
def fake_openai() -> Iterator[Callable[..., FakeOpenAI]]:
    """Start a fake OpenAI API that serves the given fixtures, one per turn.

    At the end of the test it fails if the fake answered any request with an error or
    if a listed fixture was never asked for.
    """
    started: list[FakeOpenAI] = []

    def start(replies: list[str], **fixtures: str) -> FakeOpenAI:
        fake = FakeOpenAI(replies, **fixtures)
        fake.start()
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.stop()
        assert not fake.errors, (
            f"the fake OpenAI API got requests it had no reply for: {fake.errors}"
        )
        assert not fake.unused, f"fixtures listed but never asked for: {fake.unused}"


@dataclass
class Result:
    code: int
    out: str
    err: str
    root: Path

    @property
    def finished(self) -> bool:
        """Every row gave a result, passed or failed: for a test about anything but the answer."""
        return self.code in (OK, ROW_FAILED)

    def records(self, *, processes: bool = False) -> list[list[dict[str, object]]]:
        """Every run's record lines, one list per run. Each process's own line is left out
        unless asked for, so a test about calls reads only calls."""
        files = sorted((self.root / "scrimmage-runs").glob("*/*.jsonl"))
        runs = [[json.loads(line) for line in f.read_text().splitlines()] for f in files]
        return [[x for x in run if processes or x.get("notice") != "process"] for run in runs]

    def summaries(self) -> list[dict[str, object]]:
        return [lines[-1]["summary"] for lines in self.records()]  # type: ignore[misc]


@dataclass
class Project:
    root: Path
    capsys: pytest.CaptureFixture[str]
    monkeypatch: pytest.MonkeyPatch

    @property
    def toml(self) -> Path:
        return self.root / "scrimmage.toml"

    def edit(self, old: str, new: str) -> None:
        text = self.toml.read_text()
        assert old in text, old
        self.toml.write_text(text.replace(old, new))

    def test(self, fake: FakeOpenAI | None = None) -> Result:
        if fake is not None:
            self.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
            self.monkeypatch.setenv("OPENAI_API_KEY", "test")
        self.capsys.readouterr()
        code = main(["test", str(self.toml)])
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err, self.root)


@pytest.fixture
def project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Project:
    """A copy of Acme's example agent and its scrimmage.toml in a fresh folder."""
    root = tmp_path / "acme-bot"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("__pycache__"))
    return Project(root, capsys, monkeypatch)
