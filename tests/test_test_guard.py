"""The fast tier's guard: a test not marked slow fails when it starts a process, whichever
of its threads does it, and a thread an earlier test left running is reported, not blamed.

Each case runs in a pytest of its own under the suite's conftest, so the guard sees one
small session it can be judged on.
"""

import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # each case starts a pytest

TESTS = Path(__file__).parent
PLUGINS_OFF = ("-p", "no:cacheprovider", "-p", "no:langsmith_plugin")
CASES = """
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

go, done = threading.Event(), threading.Event()


@pytest.fixture(scope="module")
def warm_pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(1) as pool:
        pool.submit(len, "warm").result()  # its one thread is born here, in a fixture
        yield pool


def test_direct_start() -> None:
    subprocess.run(["true"], check=True)


def test_start_from_its_own_thread() -> None:
    thread = threading.Thread(target=lambda: subprocess.run(["true"], check=True))
    thread.start()
    thread.join()


def test_start_through_a_module_fixtures_pool(warm_pool: ThreadPoolExecutor) -> None:
    warm_pool.submit(subprocess.run, ["true"], check=True).result()


@pytest.mark.slow
def test_slow_start() -> None:
    subprocess.run(["true"], check=True)


def test_leaves_a_thread() -> None:
    def later() -> None:
        go.wait(30)
        subprocess.run(["true"], check=True)
        done.set()

    threading.Thread(target=later, name="left-behind", daemon=True).start()


def test_runs_while_the_left_thread_starts_one() -> None:
    go.set()
    assert done.wait(30)
"""


def outcomes(report: Path) -> dict[str, str]:
    """Each test's outcome from a junit report: an error is a failed setup or teardown."""
    got: dict[str, str] = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case} & {"error", "failure", "skipped"}
        got[case.attrib["name"]] = kinds.pop() if kinds else "passed"
    return got


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, str], str]:
    folder = tmp_path_factory.mktemp("guard")
    (folder / "conftest.py").write_text((TESTS / "conftest.py").read_text())
    (folder / "test_cases.py").write_text(CASES)
    (folder / "pytest.ini").write_text(
        f"[pytest]\npythonpath = {TESTS}\nfilterwarnings = error\n"
        "markers =\n    slow: starts a process\n"
    )
    report = folder / "report.xml"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n", "0", *PLUGINS_OFF, f"--junitxml={report}"],
        cwd=folder,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return outcomes(report), done.stdout + done.stderr


def test_a_fast_test_that_starts_a_process_fails(session: tuple[dict[str, str], str]) -> None:
    got, out = session
    assert got["test_direct_start"] == "error", out
    assert "starts a process (subprocess.Popen in thread 'MainThread'), so mark it slow" in out


def test_a_start_from_a_thread_the_test_starts_fails(session: tuple[dict[str, str], str]) -> None:
    got, out = session
    assert got["test_start_from_its_own_thread"] == "error", out


def test_a_start_through_a_pool_a_fixture_made_fails(
    session: tuple[dict[str, str], str],
) -> None:
    """The pool's thread is older than the test, but a fixture made it: the start is the
    test's, as it would be through any shared pool."""
    got, out = session
    assert got["test_start_through_a_module_fixtures_pool"] == "error", out


def test_a_slow_test_may_start_a_process(session: tuple[dict[str, str], str]) -> None:
    got, out = session
    assert got["test_slow_start"] == "passed", out


def test_a_thread_an_earlier_test_left_is_reported_not_blamed(
    session: tuple[dict[str, str], str],
) -> None:
    got, out = session
    assert got["test_leaves_a_thread"] == "passed", out
    assert got["test_runs_while_the_left_thread_starts_one"] == "passed", out
    assert (
        "StrayProcessWarning: subprocess.Popen from thread 'left-behind'" in out
        and "left running by test_cases.py::test_leaves_a_thread" in out
    ), out
