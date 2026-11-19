import json
import multiprocessing.process
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fakes.openai_server import FakeOpenAI
from scrimmage_core import runner
from scrimmage_core.cli import main
from scrimmage_core.outcomes import OK, ROW_FAILED

EXAMPLE = Path(__file__).parent / "example"


def doc_pages(docs: Path) -> list[Path]:
    """Every page of the user docs. The maintainer's notes folder is not part of them."""
    return sorted(p for p in docs.rglob("*.md") if p.relative_to(docs).parts[0] != "private")


# The S1 conversation: the bot asks for the order, then the stock, then answers, or says it
# could not if a tool failed.
ACME = ["acme_get_order", "acme_get_stock", "acme_answer|acme_admits"]
EXAMPLE_FAULT = (
    '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'
    'expect = [["unable", "sorry", "try again"]]\nforbid = ["Oct 30", "October 30"]\n'
)


# Audit events for a new process. A test that makes one is in the slow tier.
PROCESS_STARTS = frozenset(
    {
        "subprocess.Popen",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.exec",
        "os.spawn",
        "os.system",
    }
)


@dataclass
class Start:
    """A process start seen by the audit hook, the thread that made it and, for a thread an
    earlier test's body left running, that test."""

    event: str
    thread: threading.Thread
    stack: str
    left_by: str | None


@dataclass
class Watch:
    """The process starts of the running test, told apart by thread.

    The audit hook sees every thread of the worker. A thread an earlier test's body started
    and left running, as loky's manager thread was, may start a process during this test;
    that start is a stray, not this test's. Any other thread counts against the test: its
    own, one born during it, and one a fixture made at any scope, such as a shared pool.
    """

    test: threading.Thread | None = None  # None outside the guard's window
    node: str = ""
    mine: list[Start] = field(default_factory=list[Start])
    stray: list[Start] = field(default_factory=list[Start])


watch = Watch()
# Each live thread, and the test whose body it was born in, or None for one born in a
# fixture's setup or teardown or outside any test. Strong references, so a label cannot
# vanish under a lookup; dead threads are dropped at each phase, so it stays small.
born_in: dict[threading.Thread, str | None] = {}


def label_threads(test: str | None) -> None:
    """Every thread born since the last phase began was born in the phase now ending."""
    for thread in [t for t in born_in if not t.is_alive()]:
        del born_in[thread]
    for thread in threading.enumerate():
        born_in.setdefault(thread, test)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    del item
    label_threads(None)  # born between tests or in the last teardown


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    del item
    label_threads(None)  # born in this test's fixtures' setup


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    label_threads(item.nodeid)  # born in this test's body


def note(event: str) -> None:
    if watch.test is None:
        return
    thread = threading.current_thread()
    left_by = born_in.get(thread)  # None too for a thread born in this phase
    start = Start(event, thread, "".join(traceback.format_stack()[:-2]), left_by)
    if thread is not watch.test and left_by is not None and left_by != watch.node:
        watch.stray.append(start)
    else:
        watch.mine.append(start)


def note_process_start(event: str, args: tuple[object, ...]) -> None:
    del args
    if event in PROCESS_STARTS:
        note(event)


sys.addaudithook(note_process_start)
# multiprocessing's spawn and forkserver start a process with no audit event, so its start
# is noted by hand. Its fork method raises os.fork as well.
real_process_start = multiprocessing.process.BaseProcess.start


def noted_process_start(self: multiprocessing.process.BaseProcess) -> None:
    note("multiprocessing.Process.start")
    real_process_start(self)


multiprocessing.process.BaseProcess.start = noted_process_start


class StrayProcessWarning(pytest.PytestWarning):
    """A thread an earlier test left running started a process during this test."""


@pytest.fixture(autouse=True)
def no_sender_thread_outlives_its_test() -> Iterator[None]:
    """A record sender left running posts to a closed port for the rest of the session, and
    a later, unrelated test then fails on its exception. So a test stops what it starts."""
    before = {t for t in threading.enumerate() if t.name == "scrimmage-sender"}
    yield
    left = [t for t in threading.enumerate() if t.name == "scrimmage-sender" and t not in before]
    for thread in left:
        thread.join(5)  # one that is stopping gets its batch window to finish
    if alive := [t for t in left if t.is_alive()]:
        pytest.fail(f"this test leaves {len(alive)} scrimmage-sender thread(s) running")


@pytest.fixture(autouse=True)
def fast_tests_start_no_process(request: pytest.FixtureRequest) -> Iterator[None]:
    """A test not marked slow fails if it starts a process, so the fast tier stays fast.

    A start from a thread an earlier test's body left running is reported as a warning
    naming that thread and that test, and does not fail this one.
    """
    watch.mine, watch.stray = [], []
    watch.node = request.node.nodeid
    watch.test = threading.current_thread()
    yield
    watch.test = None
    with warnings.catch_warnings():  # the run's "error" filter would fail this test for it
        warnings.simplefilter("always", StrayProcessWarning)
        for start in watch.stray:
            warnings.warn(
                StrayProcessWarning(
                    f"{start.event} from thread {start.thread.name!r} "
                    f"(ident {start.thread.ident}), left running by {start.left_by}:\n"
                    f"{start.stack}"
                ),
                stacklevel=1,
            )
    if watch.mine and request.node.get_closest_marker("slow") is None:
        first = watch.mine[0]
        pytest.fail(
            f"this test starts a process ({first.event} in thread {first.thread.name!r}), "
            f"so mark it slow\n{first.stack}"
        )


def pytest_configure(config: pytest.Config) -> None:
    """Coverage on Python 3.11 fails tests at random, so such a run stops before it starts.

    3.11's sys.settrace raises RuntimeError in a thread that starts while another thread is
    in a Python audit hook (gh-98257, fixed in 3.12). Coverage calls it as each thread
    starts, and the hook above, like the SDK's, runs on every audit event. The thread dies
    before its first line, and the test fails on whatever that thread was for.
    """
    # platform starts `uname -p` on its first use. Once here, no test's no-process teardown
    # meets it, whether the run is parallel or -n0.
    platform.platform()
    if sys.version_info >= (3, 12) or hasattr(config, "workerinput"):
        return
    coverage = sys.modules.get("coverage")
    if coverage is not None and coverage.Coverage.current() is not None:
        raise pytest.UsageError(
            "coverage cannot be measured on Python 3.11, whose sys.settrace races audit hooks "
            "(gh-98257); measure it on 3.12 or later, as tests/README.md shows"
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A kind test makes a cluster, which takes minutes and memory, so it is left out unless
    SCRIMMAGE_KIND=1. Left out, not skipped: a skip would fail a CI run."""
    if os.environ.get("SCRIMMAGE_KIND") == "1":
        return
    left_out = [item for item in items if item.get_closest_marker("kind") is not None]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = [item for item in items if item not in left_out]


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

    def start(replies: list[str], **fixtures: Any) -> FakeOpenAI:
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
        unless asked for, so a test about calls reads only calls.

        Folders are in the order they were written. Their names are not: two `scrimmage
        test` runs in one second share a stamp, and a random suffix then decides."""
        files = list((self.root / "scrimmage-runs").glob("*/*.jsonl"))
        newest: dict[Path, int] = {}
        for file in files:
            newest[file.parent] = max(newest.get(file.parent, 0), file.stat().st_mtime_ns)
        files.sort(key=lambda file: (newest[file.parent], file))
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
