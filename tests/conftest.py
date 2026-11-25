import concurrent.futures
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
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import httpx
import jsonschema
import pytest
from jsonschema.exceptions import best_match

import scrimmage
from fakes.anthropic_server import FakeAnthropic
from fakes.openai_server import FakeOpenAI
from scrimmage import runner
from scrimmage.cli import main
from scrimmage.outcomes import OK, ROW_FAILED
from scrimmage.records import RecordStore
from scrimmage.state import RunState

EXAMPLE = Path(__file__).parent / "example"


def doc_pages(docs: Path) -> list[Path]:
    """Every page of the user docs. The maintainer's notes folder is not part of them."""
    return sorted(p for p in docs.rglob("*.md") if p.relative_to(docs).parts[0] != "private")


# The S1 conversation: the bot asks for the order, then the stock, then answers, or says it
# could not if a tool failed.
ACME = ["acme_get_order", "acme_get_stock", "acme_answer|acme_admits"]
# The words the example's fault row accepts as admitting the failure, in real model wording.
FAULT_WORDS = (
    "unable",
    "sorry",
    "unfortunately",
    "cannot",
    "can't",
    "couldn't",
    "try again",
    "again later",
    "trouble",
    "difficulty",
    "problem",
    "issue",
    "error",
)
FAULT_EXPECT = "expect = [[" + ", ".join(f'"{word}"' for word in FAULT_WORDS) + "]]\n"
EXAMPLE_FAULT = (
    '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'
    + FAULT_EXPECT
    + 'forbid = ["Oct 30", "October 30"]\n'
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
    """A process start seen by the audit hook, the thread that made it and the test whose
    work it was, if any."""

    event: str
    thread: threading.Thread
    stack: str
    left_by: str | None


@dataclass
class Watch:
    """The process starts of the running test, told apart by the test whose work made them.

    The audit hook sees every thread of the worker. A thread an earlier test's body started
    and left running, as loky's manager thread was, may start a process during this test;
    that start is a stray, not this test's. Work this test submitted to a pool is its own,
    on whichever thread it runs, even one an earlier test's body made. Any other thread
    counts against the test: its own, one born during it, and one a fixture made at any
    scope, such as a shared pool.

    Only ThreadPoolExecutor tasks carry their test, so some starts still only warn or go
    unseen. A worker born in an earlier test's body and fed some other way only warns: anyio's
    worker on a portal or TestClient kept between tests, or a raw thread fed by a queue. A
    start inside a ProcessPoolExecutor child or an InterpreterPoolExecutor's subinterpreter
    is not seen: the child reports nothing back, and the audit hook is this interpreter's.
    """

    test: threading.Thread | None = None  # None outside the guard's window
    node: str = ""
    body: bool = False  # the test function itself is running
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
    label_threads(None)  # born between tests or in the last teardown
    # First of all, so no module or session fixture starts for a test that will not run.
    for mark in item.iter_markers("needs_python"):
        if outside_python_range(mark):
            pytest.skip(mark.kwargs["reason"])


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    del item
    label_threads(None)  # born in this test's fixtures' setup
    watch.body = True


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    watch.body = False
    label_threads(item.nodeid)  # born in this test's body


class Work(threading.local):
    """The test that submitted the pool task this thread is running, if any.

    A thread-local, not a context variable: asyncio.to_thread runs its task in a copy of the
    caller's context, which would hide the tag."""

    owner: str | None = None


work = Work()


def owner_here() -> str | None:
    """The test whose work the current thread is doing, or None for no test's.

    A pool task belongs to the test that submitted it. Otherwise the thread belongs to the
    test whose body it was born in: the running one for the test's own thread and any
    thread not yet labeled during the body.
    """
    if work.owner is not None:
        return work.owner
    thread = threading.current_thread()
    if watch.body and (thread is watch.test or thread not in born_in):
        return watch.node
    return born_in.get(thread)


P = ParamSpec("P")
T = TypeVar("T")
real_submit = ThreadPoolExecutor.submit
# Python 3.14's subclass sends each task into a subinterpreter, and a closure cannot cross.
interpreter_pool: type | None = getattr(concurrent.futures, "InterpreterPoolExecutor", None)


def owned_submit(
    self: ThreadPoolExecutor, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> Future[T]:
    """Submit fn as the submitter's work, so a lazy pool's thread, born in an earlier
    test's body, does not make this test's start a stray.

    An InterpreterPoolExecutor's task is left as it is: it runs in a subinterpreter, where
    this interpreter's audit hook does not see a start, so a tag would tell nothing."""
    if interpreter_pool is not None and isinstance(self, interpreter_pool):
        return real_submit(self, fn, *args, **kwargs)
    owner = owner_here()

    def as_owner() -> T:
        before, work.owner = work.owner, owner
        try:
            return fn(*args, **kwargs)
        finally:
            work.owner = before

    return real_submit(self, as_owner)


ThreadPoolExecutor.submit = owned_submit


def note(event: str) -> None:
    if watch.test is None:
        return
    owner = owner_here()
    start = Start(event, threading.current_thread(), "".join(traceback.format_stack()[:-2]), owner)
    if owner is not None and owner != watch.node:
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


# The session's strays, one line each, for the summary at its end. Under xdist the
# controller gets each worker's warnings through this hook, so it holds them all.
strays: list[str] = []


def pytest_warning_recorded(warning_message: warnings.WarningMessage, nodeid: str) -> None:
    if warning_message.category is StrayProcessWarning:
        first = str(warning_message.message).splitlines()[0].removesuffix(":")
        strays.append(f"{first} during {nodeid}")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """A stray does not fail the run, so the run's end lists them where they are seen."""
    if strays:
        terminalreporter.write_line(f"{len(strays)} stray process start(s): {'; '.join(strays)}")


try:
    import httpx2
except ImportError:  # the gate tests need it; the rest of the suite does not
    httpx2 = None

SEND_OWNERS: tuple[Any, ...] = tuple(
    owner
    for module in (httpx, httpx2)
    if module is not None
    for owner in (module.Client, module.AsyncClient)
)


RECORD_SCHEMA = json.loads(
    (Path(scrimmage.__file__).parent / "schemas" / "record.v1.json").read_text()
)
RECORD_CHECK = jsonschema.Draft202012Validator(RECORD_SCHEMA)


def schema_error(line: object) -> str | None:
    """What record.v1.json refuses in a line, with the line, or None. The line is read as a
    reader of the file would read it: through JSON."""
    seen = json.loads(json.dumps(line, default=str))
    if (error := best_match(RECORD_CHECK.iter_errors(seen))) is None:
        return None
    where = "/".join(str(p) for p in error.absolute_path) or "(line)"
    return f"{where}: {error.message[:200]}\n    line: {json.dumps(seen)}"


@pytest.fixture(autouse=True)
def every_record_line_matches_the_public_schema(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Each record line is checked against record.v1.json, the schema the docs publish.

    A line the schema refuses is a record a user's own validator would refuse. Two places
    are wrapped. RunState.put sees the lines the gates of this process write. RecordStore.add
    and close_run are the one place every line reaches a file: the lines of a service worker
    or a child process, which come over /v1/records, and the runner's own lines, such as
    each summary. A test that posts hand-made lines on purpose is marked stub_record_lines
    and skips the store check. The errors are collected and fail the test at teardown, so
    the writers' own code paths are not changed.
    """
    bad: list[str] = []
    put, add, close = RunState.put, RecordStore.add, RecordStore.close_run

    def note(line: object) -> None:
        if (message := schema_error(line)) is not None:
            bad.append(message)

    def checked_put(self: RunState, line: dict[str, object], *, keep: bool) -> None:
        note(line)
        put(self, line, keep=keep)

    monkeypatch.setattr(RunState, "put", checked_put)
    if request.node.get_closest_marker("stub_record_lines") is None:

        def checked_add(self: RecordStore, run: str, lines: list[dict[str, object]]) -> int:
            for line in lines:
                note(line)
            return add(self, run, lines)

        def checked_close(
            self: RecordStore, run: str, test: str, row: str, summary: dict[str, object]
        ) -> None:
            note({"v": 1, "run": run, "test": test, "row": row, "summary": summary})
            close(self, run, test, row, summary)

        monkeypatch.setattr(RecordStore, "add", checked_add)
        monkeypatch.setattr(RecordStore, "close_run", checked_close)
    yield
    monkeypatch.undo()
    if bad:
        pytest.fail(
            f"{len(bad)} record line(s) fail record.v1.json:\n" + "\n".join(bad[:5]), pytrace=False
        )


@pytest.fixture(autouse=True)
def gated_sends_are_put_back() -> Iterator[None]:
    """A gate patches `_send_single_request` on the sync and async client of each library.
    Whatever a test leaves there, the next test gets the library's own again."""
    before = [(owner, owner.__dict__["_send_single_request"]) for owner in SEND_OWNERS]
    yield
    for owner, send in before:
        if owner.__dict__["_send_single_request"] is not send:
            owner._send_single_request = send


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


# In CI a skip is expected only when the test's own marks say so, never from its reason text.
# needs_python(min=, max=, reason=) skips outside that range of Python versions (both ends
# inclusive, as (major, minor)), and a skip it makes is expected. A group mark names tests
# that another CI step runs, with the variable set to 1: there the same skip fails the run,
# so a step that lost its dependency cannot go quiet. Everywhere else it is expected.
GROUPS = {
    "needs_brotli": "SCRIMMAGE_BROTLI",
    "needs_brotlipy": "SCRIMMAGE_BROTLIPY",
    "needs_nemo": "SCRIMMAGE_NEMO",
    "needs_system_python": "SCRIMMAGE_SYSTEM_PYTHON",
    "needs_inherit_context": "SCRIMMAGE_INHERIT_CONTEXT",
}


def outside_python_range(mark: pytest.Mark) -> bool:
    now = sys.version_info[:2]
    low, high = mark.kwargs.get("min"), mark.kwargs.get("max")
    return (low is not None and now < low) or (high is not None and now > high)


def skip_is_expected(item: pytest.Item) -> bool:
    if any(outside_python_range(m) for m in item.iter_markers("needs_python")):
        return True
    if item.get_closest_marker("with_inherit_context_flag") and getattr(
        sys.flags, "thread_inherit_context", 0
    ):
        return True
    return any(
        item.get_closest_marker(name) and os.environ.get(variable) != "1"
        for name, variable in GROUPS.items()
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.skipped and call.when == "setup" and skip_is_expected(item):
        report.user_properties.append(("expected_skip", True))
    return report


def pytest_sessionfinish(session: pytest.Session) -> None:
    """In CI an unexpected skip fails the run: a safety check that did not run proves nothing.

    A skip is fine on a laptop without Ubuntu's Python; CI must have everything, except
    what skip_is_expected names. Under xdist only the controller has a terminal reporter,
    and it gets every worker's skips, so the check runs there and a worker does nothing.
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    # A collection report, from a skip at module level, has no user_properties.
    unexpected = [
        r for r in skipped if ("expected_skip", True) not in getattr(r, "user_properties", [])
    ]
    if os.environ.get("CI") and unexpected:
        names = ", ".join(r.nodeid for r in unexpected)
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


@pytest.fixture
def fake_anthropic() -> Iterator[Callable[..., FakeAnthropic]]:
    """Start a fake Anthropic API that serves the given fixtures, one per turn.

    At the end of the test it fails if the fake answered any request with an error or if a
    listed fixture was never asked for.
    """
    started: list[FakeAnthropic] = []

    def start(replies: list[str], **fixtures: Any) -> FakeAnthropic:
        fake = FakeAnthropic(replies, **fixtures)
        fake.start()
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.stop()
        assert not fake.errors, (
            f"the fake Anthropic API got requests it had no reply for: {fake.errors}"
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
