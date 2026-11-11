"""scrimmage test called from Python: a host's own signals and threads are left alone.

No agent is started. The runner's one-run function is replaced by one that sends the signal
a test needs, so these stay in the fast tier.
"""

import asyncio
import json
import os
import signal
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from conftest import Project
from scrimmage_core import cli, config, entry, runner
from scrimmage_core.runner import Runner, RunResult

pytestmark = pytest.mark.usefixtures("signals_kept")
WAIT_S = 30  # a ceiling for an event that should come at once; it only ends a failing test


@pytest.fixture(autouse=True)
def nothing_pending() -> Iterator[None]:
    entry.pending.clear()
    yield
    entry.pending.clear()


def on_first_run(monkeypatch: pytest.MonkeyPatch, act: Callable[[], None]) -> None:
    """Replace the runner's one-run function with one that does `act` once and runs nothing."""
    done: list[bool] = []

    def one(self: Runner, row: object) -> RunResult | None:
        del self, row
        if not done:
            done.append(True)
            act()
        return None

    monkeypatch.setattr(Runner, "one", one)


def fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def threads() -> set[threading.Thread]:
    return set(threading.enumerate())


def document(project: Project) -> dict[str, object]:
    return cast(dict[str, object], json.loads(project.capsys.readouterr().out))


def run_on_loop(project: Project, signum: signal.Signals) -> tuple[list[int], int]:
    """cli.main on an asyncio loop's own (main) thread, with a handler for `signum`.

    Returns how many times the handler fired, and the exit code. The loop is blocked inside
    cli.main, so the handler runs once control comes back to it.
    """
    hits: list[int] = []

    async def host() -> int:
        loop = asyncio.get_running_loop()
        seen = asyncio.Event()

        def handler() -> None:
            hits.append(1)
            seen.set()

        loop.add_signal_handler(signum, handler)
        code = cli.main(["test", "--json", str(project.toml)])
        await asyncio.wait_for(seen.wait(), WAIT_S)
        await asyncio.sleep(0)  # a second, wrongly doubled firing would run here
        return code

    return hits, asyncio.run(host())


def test_a_hosts_own_signal_still_fires_during_a_run(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_first_run(monkeypatch, lambda: os.kill(os.getpid(), signal.SIGUSR1))
    hits: list[int] = []
    code = 99
    try:
        hits, code = run_on_loop(project, signal.SIGUSR1)
    except TimeoutError:
        pytest.fail("the host's SIGUSR1 handler never fired")
    assert hits == [1]
    assert code == document(project)["exit"]


def test_host_signals_after_a_stop_signal_still_fire_until_the_run_ends(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def both() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGUSR1)

    on_first_run(monkeypatch, both)
    try:
        hits, code = run_on_loop(project, signal.SIGUSR1)
    except TimeoutError:
        pytest.fail("the host's SIGUSR1 handler never fired after a stop signal")
    assert hits == [1] and code == 3
    infra = cast(list[dict[str, str]], document(project)["infra"])
    assert [item["message"] for item in infra] == ["stopped by SIGTERM; every run was stopped"]


def test_a_stop_signal_read_by_a_late_watcher_is_reported_once(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher is held until run_all has cleared the flag, as on a busy machine."""
    cleared = threading.Event()
    original_watch = Runner._watch  # pyright: ignore[reportPrivateUsage]
    original_catch = Runner._catch_signals  # pyright: ignore[reportPrivateUsage]

    def late(self: Runner, wake_read: int, forward: int) -> None:
        cleared.wait(WAIT_S)
        original_watch(self, wake_read, forward)

    def catching(self: Runner) -> Callable[[], None]:
        release = original_catch(self)

        def after_the_clear() -> None:
            cleared.set()  # release runs once run_all has read and cleared the flag
            release()

        return after_the_clear

    monkeypatch.setattr(Runner, "_watch", late)
    monkeypatch.setattr(Runner, "_catch_signals", catching)
    on_first_run(monkeypatch, lambda: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    infra = cast(list[dict[str, str]], document(project)["infra"])
    assert [item["message"] for item in infra] == ["stopped by SIGTERM; every run was stopped"]


def test_a_full_host_wakeup_pipe_drops_the_byte_and_the_run_goes_on(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_first_run(monkeypatch, lambda: os.kill(os.getpid(), signal.SIGUSR1))
    seen = threading.Event()
    before = signal.signal(signal.SIGUSR1, lambda *_: seen.set())
    read, write = os.pipe()
    os.set_blocking(write, False)
    try:
        while True:  # fill the host's pipe to the brim
            try:
                os.write(write, b"\x00" * 4096)
            except BlockingIOError:
                break
        old = signal.set_wakeup_fd(write, warn_on_full_buffer=False)
        try:
            code = cli.main(["test", "--json", str(project.toml)])
        finally:
            signal.set_wakeup_fd(old)
        assert seen.wait(WAIT_S)  # the host's Python-level handler still ran
    finally:
        signal.signal(signal.SIGUSR1, before)
        os.close(read)
        os.close(write)
    doc = document(project)
    assert doc["exit"] == code and code != 3  # the run was not stopped, and nothing failed


def test_main_from_a_worker_thread_prints_one_document(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.Runner, "one", lambda self, row: None)
    codes: list[int] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            codes.append(cli.main(["test", "--json", str(project.toml)]))
        except BaseException as exc:  # pytest.raises cannot cross a thread
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(WAIT_S)
    assert not thread.is_alive() and not errors, errors
    out = project.capsys.readouterr().out
    doc = json.loads(out)  # one document and nothing else
    assert doc["command"] == "test" and doc["exit"] == codes[0]
    assert codes[0] != 3, doc["infra"]  # setting a handler off the main thread is no failure


def test_main_from_a_worker_thread_leaks_no_thread_and_no_fd(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.Runner, "one", lambda self, row: None)
    handlers = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

    codes: list[int] = []

    def work() -> None:
        codes.append(cli.main(["test", "--json", str(project.toml)]))

    before_fds, before_threads = fds(), threads()
    for _ in range(5):
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(WAIT_S)
        assert not thread.is_alive()
    project.capsys.readouterr()
    assert 3 not in codes and len(codes) == 5, codes
    assert threads() == before_threads
    assert fds() == before_fds
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == handlers


def test_a_failing_signal_setup_on_the_main_thread_leaks_no_thread_and_no_fd(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.Runner, "one", lambda self, row: None)
    real = signal.signal

    def second_fails(sig: int, handler: object) -> object:
        second_fails.calls += 1  # type: ignore[attr-defined]
        if second_fails.calls == 2:  # type: ignore[attr-defined]
            raise ValueError("a host that patched signal.signal")
        return real(sig, handler)  # type: ignore[arg-type]

    second_fails.calls = 0  # type: ignore[attr-defined]
    before_fds, before_threads = fds(), threads()
    codes = []
    with monkeypatch.context() as patch:
        patch.setattr(signal, "signal", second_fails)
        for _ in range(5):
            second_fails.calls = 0  # type: ignore[attr-defined]
            codes.append(cli.main(["test", str(project.toml)]))  # text: only the runner sets any
    assert codes == [3] * 5  # Scrimmage's own failure, said as one
    project.capsys.readouterr()
    assert threads() == before_threads
    assert fds() == before_fds


def test_a_signal_noted_before_the_handlers_still_stops_the_runs(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    original = Runner.one

    def watching(self: Runner, row: object) -> RunResult | None:
        result = original(self, row)  # type: ignore[arg-type]
        if result is not None:
            started.append(result.run)
        return result

    monkeypatch.setattr(Runner, "one", watching)
    run = Runner(config.load(project.toml), stop_flag=["SIGTERM"])  # as cli passes entry.pending
    try:
        with pytest.raises(runner.Interrupted) as stopped:
            run.run_all()
    finally:
        run.server.server.server_close()
    assert str(stopped.value) == "SIGTERM"
    assert started == []
    assert not list(Path(run.folder).glob("*.log")), "a run reached its agent"


def test_the_noted_signal_stops_the_runs_without_waiting_for_the_watcher(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher is held, so only the runner's own cancel can stop the first run."""
    gate = threading.Event()
    original = Runner.one
    started: list[str] = []

    def held(self: Runner, wake_read: int, forward: int) -> None:
        del self, wake_read, forward
        gate.wait(WAIT_S)

    def watching(self: Runner, row: object) -> RunResult | None:
        result = original(self, row)  # type: ignore[arg-type]
        gate.set()
        if result is not None:
            started.append(result.run)
        return result

    monkeypatch.setattr(Runner, "_watch", held)
    monkeypatch.setattr(Runner, "one", watching)
    run = Runner(config.load(project.toml), stop_flag=["SIGTERM"])
    try:
        with pytest.raises(runner.Interrupted):
            run.run_all()
    finally:
        run.server.server.server_close()
    assert started == [] and not list(run.folder.glob("*.log"))
