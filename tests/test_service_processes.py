"""Nothing a service run does may reach another process unless the test lists it.

Every process start under a run is refused unless real lists its program; a fork, a
multiprocessing process, every multiprocessing send and the process pools are refused too.
The same calls outside any run are untouched. Each target here would write a file; the file
must not exist after a refused call.
"""

import contextvars
import multiprocessing
import multiprocessing.connection
import multiprocessing.pool
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from scrimmage_core import process_gate, state, threadctx
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.plan import Plan
from service_harness import eventually
from test_service_units import Sink, sink_of

PLAIN = state.Plain()  # one plain request, still being served

pytestmark = pytest.mark.slow
ON_314 = sys.version_info >= (3, 14)
_hooked: list[bool] = []


def write(path: str) -> str:
    Path(path).write_text("written")
    return path


def write_pair(path: str, _: object) -> str:
    return write(path)


@pytest.fixture
def service() -> Iterator[state.ProcessState]:
    process = state.ProcessState(service=True)
    process.stop = lambda message: pytest.fail(message)
    state.install_process(process)
    if not _hooked:  # an audit hook cannot be removed: it does nothing outside service mode
        sys.addaudithook(process_gate.service_hook)
        _hooked.append(True)
    process_gate.install_service()
    threadctx.install(process)
    yield process
    state.install_process(None)


def a_run(
    process: state.ProcessState, real: tuple[str, ...] = (), background: tuple[str, ...] = ()
) -> state.RunState:
    sink = Sink()
    plan = Plan(
        "run-0123456789ab",
        "t",
        "baseline",
        (),
        (),
        (),
        real,
        60.0,
        service=True,
        background_real=background,
    )
    run = state.RunState(plan, sink, [], blobs=sink, process=process)
    process.register(run)
    return run


def under(run: state.RunState, fn: Callable[[], Any]) -> Any:
    context = contextvars.copy_context()
    context.run(state.SLOT.set, state.Joined(run.name, run))
    return context.run(fn)


def plain(fn: Callable[[], Any]) -> Any:
    context = contextvars.copy_context()
    context.run(state.SLOT.set, PLAIN)
    return context.run(fn)


def bounded(fn: Callable[[], Any], within: float = 30.0) -> Any:
    """fn as a plain request, on a thread of its own, failing if it does not end in time."""
    got: list[Any] = []
    worker = threading.Thread(target=lambda: got.append(fn()), daemon=True)
    plain(worker.start)
    worker.join(within)
    assert not worker.is_alive(), "the call did not end: the pool could not send its work"
    return got[0] if got else None


def refused(run: state.RunState, fn: Callable[[], Any]) -> str:
    with pytest.raises(ScrimmageRefused) as caught:
        under(run, fn)
    return str(caught.value)


def lines(run: state.RunState) -> list[dict[str, Any]]:
    return [x for x in sink_of(run).lines if "call" in x]


@pytest.mark.design("service.fork_refused")
def test_a_fork_in_a_service_run_is_refused_and_outside_a_run_is_untouched(
    service: state.ProcessState,
) -> None:
    run = a_run(service)
    assert "os.fork" in refused(run, os.fork)

    def forked() -> int:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        return os.waitpid(pid, 0)[1]

    assert plain(forked) == 0
    assert lines(run)[0]["reason"] == "service_child"


@pytest.mark.design("service.forkpty_refused")
def test_forkpty_in_a_run_is_refused(service: state.ProcessState) -> None:
    run = a_run(service)
    assert "os.fork" in refused(run, os.forkpty)


def pool_calls(target: Path) -> dict[str, Callable[[Any], Any]]:
    path = str(target)
    return {
        "submit": lambda pool: pool.submit(write, path).result(),
        "map": lambda pool: list(pool.map(write, [path])),
        "apply": lambda pool: pool.apply(write, (path,)),
        "apply_async": lambda pool: pool.apply_async(write, (path,)).get(10),
        "pool.map": lambda pool: pool.map(write, [path]),
        "map_async": lambda pool: pool.map_async(write, [path]).get(10),
        "starmap": lambda pool: pool.starmap(write_pair, [(path, 1)]),
        "imap": lambda pool: list(pool.imap(write, [path])),
    }


@pytest.mark.design("service.pools_refused")
@pytest.mark.parametrize("method", ["fork", "spawn", "forkserver"])
def test_a_process_pool_made_before_the_run_is_refused_under_a_run(
    service: state.ProcessState, tmp_path: Path, method: str
) -> None:
    context = multiprocessing.get_context(method)
    executor = ProcessPoolExecutor(max_workers=1, mp_context=context)
    pool = context.Pool(1)
    run = a_run(service)
    try:
        for name, call in pool_calls(tmp_path / "written").items():
            target = executor if name in ("submit", "map") else pool
            refused(run, lambda c=call, t=target: c(t))
            assert not (tmp_path / "written").exists(), name
        for name, call in pool_calls(tmp_path / "plain").items():  # the same calls, plainly
            target = executor if name in ("submit", "map") else pool
            bounded(lambda c=call, t=target: c(t))  # a pool that cannot send fails, not hangs
            assert (tmp_path / "plain").exists(), name
            (tmp_path / "plain").unlink()
        # A refusal after the work was queued would let the pool run it once woken: by now
        # the plain calls woke it many times, and the run's work must still never have run.
        assert not (tmp_path / "written").exists()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)  # never waits on a stuck task
        bounded(pool.terminate)
        stopped(pool)


@pytest.mark.design("service.processes_refused")
@pytest.mark.parametrize("method", ["fork", "spawn", "forkserver"])
def test_a_multiprocessing_process_started_in_a_run_is_refused(
    service: state.ProcessState, tmp_path: Path, method: str
) -> None:
    run = a_run(service)
    target = tmp_path / "written"
    context: Any = multiprocessing.get_context(method)
    child = context.Process(target=write, args=(str(target),))
    refused(run, child.start)
    assert not target.exists()


@pytest.mark.design("service.shell_refused")
def test_shell_true_python_write_in_a_run_is_refused_and_the_file_is_not_written(
    service: state.ProcessState, tmp_path: Path
) -> None:
    (tmp_path / "write.py").write_text(f"open({str(tmp_path / 'written')!r}, 'w').write('x')\n")
    line = f"{sys.executable} {tmp_path / 'write.py'}"
    message = refused(a_run(service), lambda: subprocess.run(line, shell=True, check=True))
    assert 'Add "process:sh" to [agent] real' in message
    assert not (tmp_path / "written").exists()
    listed = a_run(service, real=("process:sh", "process:" + os.path.basename(sys.executable)))
    under(listed, lambda: subprocess.run(line, shell=True, check=True))
    assert (tmp_path / "written").exists()


@pytest.mark.design("service.interpreters_refused")
@pytest.mark.skipif(not ON_314, reason="subinterpreters are new in Python 3.14")
def test_an_interpreter_pool_executor_submit_in_a_run_is_refused(
    service: state.ProcessState, tmp_path: Path
) -> None:
    from concurrent.futures import InterpreterPoolExecutor  # type: ignore[attr-defined]

    with InterpreterPoolExecutor(max_workers=1) as pool:
        refused(a_run(service), lambda: pool.submit(write, str(tmp_path / "w")).result())
        assert not (tmp_path / "w").exists()
        # A plain request's task runs: a standard-library function crosses into it.
        assert isinstance(plain(lambda: pool.submit(os.getpid).result()), int)


def interpreter() -> Any:
    from concurrent import interpreters  # type: ignore[attr-defined]

    return interpreters.create()  # type: ignore[attr-defined]  # 3.14 and later


@pytest.mark.design("service.interpreters_refused")
@pytest.mark.skipif(not ON_314, reason="subinterpreters are new in Python 3.14")
def test_interpreter_exec_in_a_run_is_refused(service: state.ProcessState, tmp_path: Path) -> None:
    sub = interpreter()
    code = f"open({str(tmp_path / 'w')!r}, 'w').write('x')"
    refused(a_run(service), lambda: sub.exec(code))
    assert not (tmp_path / "w").exists()
    sub.close()


@pytest.mark.design("service.interpreters_refused")
@pytest.mark.skipif(not ON_314, reason="subinterpreters are new in Python 3.14")
def test_interpreter_call_in_a_run_is_refused(service: state.ProcessState, tmp_path: Path) -> None:
    sub = interpreter()
    refused(a_run(service), lambda: sub.call(write, str(tmp_path / "w")))
    assert not (tmp_path / "w").exists()
    sub.close()


@pytest.mark.design("service.interpreters_refused")
@pytest.mark.skipif(not ON_314, reason="subinterpreters are new in Python 3.14")
def test_interpreter_call_in_thread_in_a_run_is_refused(
    service: state.ProcessState, tmp_path: Path
) -> None:
    sub = interpreter()
    refused(a_run(service), lambda: sub.call_in_thread(write, str(tmp_path / "w")))
    assert not (tmp_path / "w").exists()
    sub.close()


@pytest.mark.design("service.child_names_fix")
def test_a_service_child_refusal_names_the_real_entry_in_the_error_setup_and_report(
    service: state.ProcessState,
) -> None:
    """The error and the run's line here; the setup request's exit 2 and the report under the
    row read the same line, in the runner's tests."""
    run = a_run(service)
    message = refused(run, lambda: subprocess.run(["git", "--version"], check=True))
    fix = (
        "git was started inside a test run, and service runs refuse every process start. "
        'Add "process:git" to [agent] real if it is safe.'
    )
    assert message == fix
    line = lines(run)[0]
    assert (line["call"], line["reason"], line["mistake"]) == ("process:git", "service_child", fix)


@pytest.mark.design("service.hidden_shell_out")
def test_a_service_child_refusal_is_by_config_and_a_hidden_shell_out_is_a_setup_mistake(
    service: state.ProcessState,
) -> None:
    import ctypes.util

    run = a_run(service)
    message = refused(run, lambda: ctypes.util.find_library("no-such-library-anywhere"))
    assert 'Add "process:' in message  # ldconfig or gcc, by platform: the line names it
    line = lines(run)[0]
    assert line["reason"] == "service_child" and line["by_config"] is True


@pytest.mark.design("service.shutdown_refused")
def test_executor_shutdown_inside_a_run_is_refused(service: state.ProcessState) -> None:
    executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    plain(lambda: executor.submit(os.getpid).result())
    try:
        refused(a_run(service), executor.shutdown)  # its wakeup pipe is a send: fail closed
    finally:
        executor.shutdown()


@pytest.mark.design("service.every_start_refused")
def test_every_process_start_in_a_run_is_refused_unless_listed(
    service: state.ProcessState, tmp_path: Path
) -> None:
    other = Path(sys.base_prefix) / "bin" / "python3"
    starts: dict[str, tuple[str, Callable[[], Any]]] = {
        "git": (
            "git",
            lambda: subprocess.run(["git", "--version"], check=True, capture_output=True),
        ),
        "env": ("env", lambda: subprocess.run(["env", "python3", "-c", "0"], check=True)),
        "uv": ("uv", lambda: subprocess.run(["uv", "--version"], check=True, capture_output=True)),
        "second python": (other.name, lambda: subprocess.run([str(other), "-c", "0"], check=True)),
        "os.system": ("sh", lambda: os.system("true")),
        "posix_spawn": (
            "true",
            lambda: os.waitpid(os.posix_spawn("/bin/true", ["true"], dict(os.environ)), 0),
        ),
    }
    for name, (program, start) in starts.items():
        message = refused(a_run(service), start)
        assert f'Add "process:{program}" to [agent] real' in message, name
        under(a_run(service, real=(f"process:{program}", "process:python3")), start)


@pytest.mark.design("service.fork_exec_once")
def test_a_fork_exec_after_an_allowed_popen_passes(service: state.ProcessState) -> None:
    run = a_run(service, real=("process:true",))
    for _ in range(3):  # on 3.14 each Popen raises fork_exec too: the same start, once
        assert under(run, lambda: subprocess.run(["true"], check=True)).returncode == 0
    assert [x["call"] for x in lines(run)] == ["process:true"] * 3


@pytest.mark.design("service.fork_exec_once")
def test_a_popen_that_starts_through_posix_spawn_is_one_start(
    service: state.ProcessState,
) -> None:
    run = a_run(service, real=("process:true",))
    true = shutil.which("true")
    assert true is not None
    # A full path and close_fds=False: subprocess starts it with os.posix_spawn.
    assert under(run, lambda: subprocess.run([true], close_fds=False)).returncode == 0
    assert [x["call"] for x in lines(run)] == ["process:true"]


def leftover_start(service: state.ProcessState, popen: Callable[[], object]) -> str:
    """A listed Popen, then a start of a program not listed, which no Popen made."""
    run = a_run(service, real=("process:true",))
    echo = shutil.which("echo")
    assert echo is not None
    under(run, popen)
    import multiprocessing.util

    path = os.fsencode(echo)
    return refused(run, lambda: multiprocessing.util.spawnv_passfds(path, [path, b"x"], ()))


@pytest.mark.design("service.fork_exec_once")
@pytest.mark.skipif(not ON_314, reason="_posixsubprocess.fork_exec is audited from 3.14")
def test_a_popen_that_went_through_posix_spawn_lets_no_later_start_pass(
    service: state.ProcessState,
) -> None:
    true = shutil.which("true")
    assert true is not None
    started = leftover_start(service, lambda: subprocess.run([true], close_fds=False))
    assert "echo was started inside a test run" in started


@pytest.mark.design("service.fork_exec_once")
@pytest.mark.skipif(not ON_314, reason="_posixsubprocess.fork_exec is audited from 3.14")
def test_a_popen_that_failed_before_its_start_lets_no_later_start_pass(
    service: state.ProcessState,
) -> None:
    def failing() -> None:
        with pytest.raises(ValueError, match="illegal environment variable name"):
            subprocess.run(["true"], env={"A=B": "x"})

    assert "echo was started inside a test run" in leftover_start(service, failing)


def consumer(kind: str, end: Any, path: str) -> None:
    """A process made before the run: it writes whatever it is sent to the file."""
    got = (
        end.get()
        if kind in ("queue", "simple", "joinable", "put_nowait")
        else (end.recv() if kind == "send" else end.recv_bytes())
    )
    Path(path).write_text(str(got))


@pytest.mark.design("service.send_points")
@pytest.mark.parametrize(
    "kind", ["queue", "simple", "joinable", "put_nowait", "send", "send_bytes"]
)
def test_multiprocessing_send_points_refuse_under_a_run(
    service: state.ProcessState, tmp_path: Path, kind: str
) -> None:
    context = multiprocessing.get_context("spawn")
    mine: Any
    end: Any
    if kind == "queue":
        end = mine = context.Queue()
    elif kind in ("joinable", "put_nowait"):  # its own put, which does not call Queue.put
        end = mine = context.JoinableQueue()
    elif kind == "simple":
        end = mine = context.SimpleQueue()
    else:
        mine, end = context.Pipe()
    target = tmp_path / "written"
    child = context.Process(target=consumer, args=(kind, end, str(target)))
    plain(child.start)
    put = {
        "queue": lambda: mine.put("run's data"),
        "simple": lambda: mine.put("run's data"),
        "joinable": lambda: mine.put("run's data"),
        "put_nowait": lambda: mine.put_nowait("run's data"),
        "send": lambda: mine.send("run's data"),
        "send_bytes": lambda: mine.send_bytes(b"run's data"),
    }[kind]
    try:
        refused(a_run(service), put)
        child.join(1)
        assert not target.exists()
        plain(put)  # the same send from a plain request goes
        child.join(30)
        assert target.exists()
    finally:
        child.kill()
        child.join()


@pytest.mark.design("service.send_points_untouched")
def test_threadpool_and_scrimmage_traffic_never_meet_a_send_point(
    service: state.ProcessState, tmp_path: Path
) -> None:
    import asyncio

    from service_harness import Service, drive

    met: list[str] = []
    real = process_gate.refuse_other

    def watched(*args: Any) -> None:
        met.append(str(args[2]))
        real(*args)

    process_gate.refuse_other = watched  # type: ignore[assignment]
    try:
        run = a_run(service)
        pool = under(run, lambda: multiprocessing.pool.ThreadPool(1))
        assert under(run, lambda: pool.apply(os.getpid)) == os.getpid()
        pool.terminate()
        stopped(pool)
        svc = Service(tmp_path / "records")
        try:
            name = svc.open()

            async def app(scope: Any, receive: Any, send: Any) -> None:
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            from scrimmage_core import asgi_gate

            reply = asyncio.run(drive(asgi_gate.Entry(app), headers=[svc.header(name)]))
            assert reply.status == 200
            svc.closed(name)
        finally:
            svc.close()
    finally:
        process_gate.refuse_other = real  # type: ignore[assignment]
    assert met == []


@pytest.mark.design("service.loky_refused")
def test_a_joblib_loky_executor_made_before_the_run_is_refused(
    service: state.ProcessState, tmp_path: Path
) -> None:
    import joblib
    from joblib.externals.loky import get_reusable_executor

    executor = get_reusable_executor(max_workers=1)
    plain(lambda: executor.submit(os.getpid).result())  # made and warm before the run
    run = a_run(service)
    refused(run, lambda: executor.submit(write, str(tmp_path / "loky")).result())
    assert not (tmp_path / "loky").exists()
    with joblib.parallel_backend("loky", n_jobs=1):
        pass
    refused(
        run,
        lambda: joblib.Parallel(n_jobs=2, backend="loky")(
            joblib.delayed(write)(str(tmp_path / "jl")) for _ in range(2)
        ),
    )
    assert not (tmp_path / "jl").exists()
    executor.shutdown()


def in_ended_request(fn: Callable[[], Any]) -> Any:
    """fn in a plain request that has ended once it returns, so any thread it started, such
    as a pool's manager or a queue's feeder, runs outside any request after it."""
    request = state.Plain()
    context = contextvars.copy_context()
    context.run(state.SLOT.set, request)
    try:
        return context.run(fn)
    finally:
        request.done = True


def outside(fn: Callable[[], Any]) -> Any:
    """fn in code outside any request, such as a background job a request left running."""
    return contextvars.Context().run(fn)


def refused_outside(fn: Callable[[], Any]) -> str:
    with pytest.raises(ScrimmageRefused) as caught:
        outside(fn)
    return str(caught.value)


@pytest.mark.design("service.outside_process_entries")
@pytest.mark.parametrize("method", ["fork", "spawn"])
def test_process_pools_used_outside_any_request_meet_the_outside_rules(
    service: state.ProcessState, tmp_path: Path, method: str
) -> None:
    context = multiprocessing.get_context(method)
    executor = ProcessPoolExecutor(max_workers=1, mp_context=context)
    pool = context.Pool(1)
    written = tmp_path / "written"
    made: list[Any] = []  # every pool made below, stopped at the end whatever happens
    try:
        run = a_run(service)
        # Making a process pool outside any request is a decided start: refused, and its
        # initializer never runs.
        initialized = tmp_path / "initialized"
        refused_outside(
            lambda: made.append(context.Pool(1, initializer=write, initargs=(str(initialized),)))
        )
        assert made == [] and not initialized.exists()
        assert [x["reason"] for x in lines(run)] == ["orphan"]
        sink_of(run).lines.clear()
        calls = pool_calls(written)
        for name, call in calls.items():
            target = executor if name in ("submit", "map") else pool
            message = refused_outside(lambda c=call, t=target: c(t))
            assert "outside any request" in message and "background_real" in message, name
            assert not written.exists(), name
        assert [x["reason"] for x in lines(run)] == ["orphan"] * len(calls)
        # Stopping a pool, as a lifespan's shutdown does, sends none of the team's work.
        spare_executor = ProcessPoolExecutor(max_workers=1, mp_context=context)
        spare_pool, other_pool = plain(lambda: (context.Pool(1), context.Pool(1)))
        made += [spare_pool, other_pool]
        assert plain(lambda: spare_executor.submit(os.getpid).result()) > 0
        outside(spare_executor.shutdown)
        outside(spare_pool.close)
        outside(spare_pool.join)
        outside(other_pool.terminate)
        outside(other_pool.join)
        assert len(lines(run)) == len(calls)
        # The pools' own threads still carry a plain request's work while the run is open.
        assert plain(lambda: executor.submit(write, str(written)).result()) == str(written)
        assert plain(lambda: pool.apply_async(write, (str(written),)).get(30)) == str(written)
        service.unregister(run)
        listed = a_run(service, background=("process:multiprocessing",))
        written.unlink()
        assert outside(lambda: pool.apply_async(write, (str(written),)).get(30)) == str(written)
        assert outside(lambda: executor.submit(write, str(written)).result()) == str(written)
        # A listed pool made outside any request starts, and its initializer runs.
        listed_pool = outside(
            lambda: context.Pool(1, initializer=write, initargs=(str(initialized),))
        )
        made.append(listed_pool)
        assert listed_pool.apply_async(os.getpid).get(30) > 0 and initialized.exists()
        # A listed submit that starts the pool's processes: the starts are the pool's own.
        fresh = ProcessPoolExecutor(max_workers=1, mp_context=context)
        assert outside(lambda: fresh.submit(write, str(written)).result()) == str(written)
        outside(fresh.shutdown)
        notices = {x.get("notice") for x in sink_of(listed).lines if x.get("notice")}
        assert notices == {"background"}
    finally:
        executor.shutdown()
        for each in [pool, *made]:
            each.terminate()
            stopped(each)


@pytest.mark.design("service.outside_process_entries")
@pytest.mark.parametrize("kind", ["start", "queue", "simple", "send", "send_bytes", "fork"])
def test_a_process_start_or_send_outside_any_request_meets_the_outside_rules(
    service: state.ProcessState, tmp_path: Path, kind: str
) -> None:
    context = multiprocessing.get_context("spawn")
    target = tmp_path / "written"
    queue, simple = context.Queue(), context.SimpleQueue()
    mine, end = context.Pipe()
    child = context.Process(target=write, args=(str(target),))
    act = {
        "start": child.start,
        "queue": lambda: queue.put("outside data"),
        "simple": lambda: simple.put("outside data"),
        "send": lambda: mine.send("outside data"),
        "send_bytes": lambda: mine.send_bytes(b"outside data"),
        "fork": os.fork,
    }[kind]
    run = a_run(service)
    message = refused_outside(act)
    assert "outside any request" in message, message
    assert [x["reason"] for x in lines(run)] == ["orphan"]
    assert not end.poll() and queue.empty() and simple.empty()
    assert not target.exists()


@pytest.mark.design("service.outside_process_entries")
def test_a_queues_feeder_a_plain_request_started_still_sends_after_the_request_returned(
    service: state.ProcessState,
) -> None:
    queue: Any = multiprocessing.get_context("spawn").Queue()
    request = state.Plain()
    context = contextvars.copy_context()
    context.run(state.SLOT.set, request)
    context.run(queue.put, "first")  # starts the queue's feeder thread, in the request
    assert queue.get(timeout=10) == "first"
    request.done = True
    run = a_run(service)
    plain(lambda: queue.put("second"))  # the feeder sends it, outside any request
    assert queue.get(timeout=10) == "second"
    assert lines(run) == []
    queue.close()
    queue.join_thread()


@pytest.mark.design("service.outside_process_entries")
def test_a_thread_pools_task_outside_any_request_is_never_a_process_pools_own_work(
    service: state.ProcessState, tmp_path: Path
) -> None:
    executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("fork"))
    threads = multiprocessing.pool.ThreadPool(1)
    workers = ThreadPoolExecutor(max_workers=1)
    target = tmp_path / "written"

    def send() -> str:
        return executor.submit(write, str(target)).result()

    try:
        assert plain(send) == str(target)  # every pool is started before the run
        assert plain(lambda: threads.apply(send)) == str(target)
        assert plain(lambda: workers.submit(send).result()) == str(target)
        target.unlink()
        run = a_run(service)
        refused_outside(lambda: threads.apply(send))
        refused_outside(lambda: workers.submit(send).result())
        assert [x["reason"] for x in lines(run)] == ["orphan", "orphan"]
        assert not target.exists()
    finally:
        executor.shutdown()
        workers.shutdown()
        threads.terminate()
        threads.join()


class Team:
    """Team code a pool's own thread runs: it starts sh to write a file, and says whether it
    ran or was refused."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self.seen: list[str] = []
        self.done = threading.Event()

    def __call__(self, *_: object) -> None:
        try:
            subprocess.run(["sh", "-c", f"echo x > {self.target}"], check=True)
            self.seen.append("ran")
        except ScrimmageRefused as refusal:
            self.seen.append(f"refused: {refusal}")
        finally:
            self.done.set()

    def items(self) -> Iterator[str]:
        self()
        yield "a"

    def refused(self) -> bool:
        assert self.done.wait(10)
        return self.seen[0].startswith("refused: ") and not self.target.exists()


@pytest.mark.design("service.pool_callbacks_follow_their_run")
@pytest.mark.parametrize("hook", ["callback", "error_callback", "imap items"])
def test_a_thread_pools_callbacks_and_imap_items_follow_the_run_that_handed_them_over(
    service: state.ProcessState, tmp_path: Path, hook: str
) -> None:
    """A pool's handler threads are the pool's own; the team's code they run is not."""
    pool = plain(lambda: multiprocessing.pool.ThreadPool(1))
    team = Team(tmp_path / "written")
    run = a_run(service)
    try:
        if hook == "callback":
            under(run, lambda: pool.apply_async(len, ("x",), callback=team))
        elif hook == "error_callback":
            under(run, lambda: pool.apply_async(len, (1,), error_callback=team))
        else:
            under(run, lambda: list(pool.imap(len, team.items())))
        assert team.refused(), team.seen
        assert any(x.get("reason") == "service_child" for x in lines(run)), [
            (x["call"], x.get("action"), x.get("reason")) for x in lines(run)
        ]
    finally:
        pool.terminate()
        stopped(pool)


@pytest.mark.design("service.pool_callbacks_follow_their_run")
def test_a_process_pools_callback_outside_any_request_meets_the_outside_rules(
    service: state.ProcessState, tmp_path: Path
) -> None:
    """The submit is listed under background_real; the callback's own start is not."""
    pool = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    team = Team(tmp_path / "written")
    a_run(service, background=("process:multiprocessing",))
    try:
        outside(lambda: pool.apply_async(os.getpid, callback=team))
        assert team.refused(), team.seen
    finally:
        pool.terminate()
        stopped(pool)


def read_one(fd: int) -> int:
    return len(os.read(fd, 1))


@pytest.mark.design("service.pool_callbacks_follow_their_run")
def test_a_done_callback_a_run_adds_follows_that_run_on_the_pools_manager_thread(
    service: state.ProcessState, tmp_path: Path
) -> None:
    held, release = os.pipe()  # the task waits on it, so the callback is added first
    executor = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("fork"))
    team = Team(tmp_path / "written")
    try:
        future = plain(lambda: executor.submit(read_one, held))
        run = a_run(service)
        under(run, lambda: future.add_done_callback(team))
        os.write(release, b"x")
        assert future.result(10) == 1
        assert team.refused(), team.seen
    finally:
        os.close(held)
        os.close(release)
        executor.shutdown()


@pytest.mark.design("service.thread_pool_in_a_run")
def test_a_thread_pool_used_and_closed_in_a_run_runs_and_stops(
    service: state.ProcessState,
) -> None:
    run = a_run(service)

    def go() -> list[int]:
        with multiprocessing.pool.ThreadPool(1) as pool:
            return pool.map(len, ["ab"])

    assert under(run, go) == [2]
    pool = under(run, lambda: multiprocessing.pool.ThreadPool(1))
    assert under(run, lambda: pool.map(len, ["abc"])) == [3]
    under(run, pool.close)
    under(run, pool.join)
    assert lines(run) == []


@pytest.mark.design("service.fork_exec_once")
def test_a_listed_popen_that_returned_lets_no_later_spawn_pass(
    service: state.ProcessState,
) -> None:
    """On 3.11 to 3.13 a Popen's own fork raises no event that would spend its decision."""
    run = a_run(service, real=("process:true",))
    true, echo = shutil.which("true"), shutil.which("echo")
    assert true is not None and echo is not None
    under(run, lambda: subprocess.run([true]))

    def spawn() -> int:
        return os.waitpid(os.posix_spawn(echo, [true], dict(os.environ)), 0)[1]

    assert "echo was started inside a test run" in refused(run, spawn)
    # The same program with the same arguments, after the Popen returned, is its own start:
    # decided, so it has its own line.
    other = a_run(service, real=("process:true",))
    under(other, lambda: subprocess.run([true]))
    under(other, lambda: os.waitpid(os.posix_spawn(true, [true], dict(os.environ)), 0))
    assert [x["call"] for x in lines(other)] == ["process:true", "process:true"]


@pytest.mark.design("service.worker_pong")
def test_a_uvicorn_without_always_pong_stops_the_service_naming_why(
    service: state.ProcessState,
) -> None:
    import types

    stops: list[str] = []
    service.stop = stops.append
    module = types.ModuleType("uvicorn.supervisors.multiprocess")
    module.Process = type("Process", (), {})  # type: ignore[attr-defined]
    process_gate.patch_uvicorn_worker(module)
    assert stops and "always_pong" in stops[0]


@pytest.mark.design("service.pool_stops")
def test_a_process_pools_stop_under_a_run_is_refused_and_leaves_the_pool_as_it_was(
    service: state.ProcessState,
) -> None:
    pool = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    executor = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("fork"))
    try:
        assert plain(lambda: executor.submit(os.getpid).result()) > 0
        run = a_run(service)
        for stop in (pool.close, pool.terminate, executor.shutdown):
            assert "inside a test run" in refused(run, stop)
        # Nothing was half done: the pool and the executor still work.
        assert plain(lambda: pool.apply_async(os.getpid).get(30)) > 0
        assert plain(lambda: executor.submit(os.getpid).result()) > 0
    finally:
        plain(executor.shutdown)
        plain(pool.terminate)
        stopped(pool)


def stopped(pool: Any, within: float = 20.0) -> None:
    """pool.join, bounded: a pool left half closed fails here instead of hanging the test."""
    done = threading.Thread(target=pool.join, daemon=True)
    plain(done.start)
    done.join(within)
    assert not done.is_alive(), "the pool's join hung: it was left half closed"


def sh_write(target: str) -> str:
    subprocess.run(["sh", "-c", f"echo x > {target}"], check=True)
    return "started"


def attempt(fn: Callable[[], object]) -> str:
    try:
        fn()
        return "ran"
    except ScrimmageRefused:
        return "refused"


FAILED_ONCE: list[bool] = []


def init_fails_once() -> None:
    if not FAILED_ONCE:
        FAILED_ONCE.append(True)
        raise SystemExit  # the worker thread ends, and the pool's handler starts another


@pytest.mark.design("service.no_inherited_exemption")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # the death
def test_a_thread_pools_replacement_worker_is_never_the_pools_own(
    service: state.ProcessState, tmp_path: Path
) -> None:
    """A worker the pool's own handler starts, after one died, runs the team's tasks: it is
    decided like any other thread, and outside work on it meets the outside rules."""
    target = tmp_path / "written"
    FAILED_ONCE.clear()
    pool = outside(lambda: multiprocessing.pool.ThreadPool(2, initializer=init_fails_once))
    try:
        for _ in range(3):  # make sure the replacement worker has taken tasks
            assert outside(lambda: pool.apply_async(len, ("ab",)).get(30)) == 2
        assert FAILED_ONCE
        run = a_run(service)
        tried = [
            outside(lambda: attempt(lambda: pool.apply_async(sh_write, (str(target),)).get(30)))
            for _ in range(6)
        ]
        assert tried == ["refused"] * 6 and not target.exists(), tried
        assert {x["reason"] for x in lines(run)} == {"orphan"}
        assert not any(t in state.INTERNAL for t in pool._pool)  # type: ignore[attr-defined]
    finally:
        plain(pool.terminate)
        stopped(pool)


@pytest.mark.design("service.no_inherited_exemption")
def test_a_pools_own_threads_are_its_handlers_and_nothing_else(
    service: state.ProcessState,
) -> None:
    """A process pool's handlers, started inside its decided making, are its own; a thread
    pool's are not, as its making decides nothing, and no worker or team thread ever is."""
    pool = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    threads = plain(lambda: multiprocessing.pool.ThreadPool(1))
    handle_tasks = multiprocessing.pool.Pool._handle_tasks  # type: ignore[attr-defined]
    team = threading.Thread(target=handle_tasks, args=(None,) * 5)
    try:
        handlers = {pool._worker_handler, pool._task_handler, pool._result_handler}  # type: ignore[attr-defined]
        assert handlers <= set(state.INTERNAL)
        others = {threads._worker_handler, threads._task_handler, *threads._pool}  # type: ignore[attr-defined]
        assert not others & set(state.INTERNAL)
        assert not state.internal_thread(team)  # the team's thread with a pool's target
    finally:
        for each in (pool, threads):
            plain(each.terminate)
            stopped(each)


@pytest.mark.design("service.no_inherited_exemption")
def test_a_process_pools_manager_thread_carries_a_plain_task_while_a_run_is_open(
    service: state.ProcessState,
) -> None:
    """The executor's manager thread puts each task on its call queue: its own put, which
    a plain request's decided submit carries."""
    executor = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("fork"))
    try:
        # Warmed in a request that then ends: its threads now run outside any request.
        assert in_ended_request(lambda: executor.submit(os.getpid).result(10)) > 0
        assert executor._executor_manager_thread in state.INTERNAL  # type: ignore[attr-defined]
        run = a_run(service)
        assert plain(lambda: executor.submit(len, "abc").result(10)) == 3
        assert lines(run) == []
    finally:
        plain(
            lambda: executor.shutdown(wait=False, cancel_futures=True)
        )  # never waits on a stuck task


@pytest.mark.design("service.mcp_stdio_decided")
def test_an_mcp_stdio_server_started_in_a_run_is_decided_as_its_program(
    service: state.ProcessState, tmp_path: Path
) -> None:
    import asyncio

    import mcp.client.stdio as stdio

    from scrimmage_core import mcp_gate

    if not getattr(stdio, "_patched_for_tests", False):
        mcp_gate.patch_stdio(stdio)
        stdio._patched_for_tests = True  # type: ignore[attr-defined]
    target = tmp_path / "written"

    async def start() -> int:
        line = ["-c", f"echo x > {target}"]
        proc = await stdio._create_platform_compatible_process("sh", line, dict(os.environ))  # type: ignore[attr-defined]
        return await proc.wait()

    run = a_run(service)
    assert 'Add "process:sh"' in refused(run, lambda: asyncio.run(start()))
    assert not target.exists()
    listed = a_run(service, real=("process:sh",))
    assert under(listed, lambda: asyncio.run(start())) == 0 and target.exists()


@pytest.mark.design("service.spawnv_decided")
def test_a_direct_spawnv_passfds_in_a_run_is_decided(
    service: state.ProcessState, tmp_path: Path
) -> None:
    from multiprocessing import util

    target = tmp_path / "written"
    run = a_run(service)

    def spawn() -> int:
        pid = util.spawnv_passfds(b"/bin/sh", [b"sh", b"-c", f"echo x > {target}".encode()], ())
        return os.waitpid(pid, 0)[1]

    assert 'Add "process:sh"' in refused(run, spawn)
    assert not target.exists()


@pytest.mark.design("service.pool_teardown")
@pytest.mark.filterwarnings("ignore:unclosed running multiprocessing pool:ResourceWarning")
def test_a_pool_dropped_under_a_run_tears_itself_down_with_no_line(
    service: state.ProcessState,
) -> None:
    import gc

    pool = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    workers = list(pool._pool)  # type: ignore[attr-defined]
    run = a_run(service)
    held = [pool]
    del pool

    def drop() -> None:
        held.clear()
        gc.collect()

    under(run, drop)
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()
    assert lines(run) == []


@pytest.mark.design("service.every_start_refused")
def test_asyncio_os_pty_and_exec_starts_in_a_run_are_refused(
    service: state.ProcessState, tmp_path: Path
) -> None:
    import asyncio
    import pty

    target = tmp_path / "written"
    line = f"echo x > {target}"
    run = a_run(service)

    async def exec_() -> int:
        return await (await asyncio.create_subprocess_exec("sh", "-c", line)).wait()

    async def shell() -> int:
        return await (await asyncio.create_subprocess_shell(line)).wait()

    async def loop_exec() -> object:
        loop = asyncio.get_running_loop()
        return await loop.subprocess_exec(asyncio.SubprocessProtocol, "sh", "-c", line)

    starts: dict[str, Callable[[], object]] = {
        "create_subprocess_exec": lambda: asyncio.run(exec_()),
        "create_subprocess_shell": lambda: asyncio.run(shell()),
        "loop.subprocess_exec": lambda: asyncio.run(loop_exec()),
        "posix_spawnp": lambda: os.posix_spawnp("sh", ["sh", "-c", line], dict(os.environ)),
        "spawnv": lambda: os.spawnv(os.P_WAIT, "/bin/sh", ["sh", "-c", line]),
        "pty.spawn": lambda: pty.spawn(["sh", "-c", line]),
    }
    for name, start in starts.items():
        assert attempt(lambda s=start: under(run, s)) == "refused", name
    assert not target.exists()

    def exec_in_a_child() -> int:
        pid = os.fork()
        if pid == 0:
            try:
                under(run, lambda: os.execv("/bin/sh", ["sh", "-c", line]))
            except ScrimmageRefused:
                os._exit(3)
            os._exit(4)
        return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

    assert plain(exec_in_a_child) == 3 and not target.exists()


@pytest.mark.design("service.processes_refused")
def test_a_manager_started_or_sent_to_in_a_run_is_refused(
    service: state.ProcessState, tmp_path: Path
) -> None:
    from multiprocessing.managers import BaseManager, SyncManager

    class Writer(BaseManager):
        pass

    Writer.register("write", callable=write)
    target = tmp_path / "written"
    manager = Writer(ctx=multiprocessing.get_context("fork"))
    plain(manager.start)
    try:
        run = a_run(service)
        assert attempt(lambda: under(run, lambda: manager.write(str(target)))) == "refused"  # type: ignore[attr-defined]
        fresh = SyncManager(ctx=multiprocessing.get_context("fork"))
        assert attempt(lambda: under(run, fresh.start)) == "refused"
        assert not target.exists()
    finally:
        plain(manager.shutdown)


class StartsWhenPickled:
    """The team's own object whose __reduce__ starts a program, as pickling runs it."""

    def __init__(self, target: Path) -> None:
        self.target = target

    def __reduce__(self) -> tuple[type, tuple[str]]:
        subprocess.run(["sh", "-c", f"echo x > {self.target}"], check=True)
        return (str, ("pickled",))


@pytest.mark.design("service.pickling_gap")
def test_a_reduce_that_starts_a_program_while_a_decided_send_pickles_is_not_decided_again(
    service: state.ProcessState, tmp_path: Path
) -> None:
    """A known gap, in docs/limits.md: outside any request, a send background_real lists is
    decided once; the team's __reduce__ that the call itself pickles runs inside it and is
    not decided again. Under a run the send is refused before anything is pickled. A queue's
    feeder, which pickles later on its own thread, decides what the __reduce__ starts."""
    simple: Any = multiprocessing.get_context("spawn").SimpleQueue()
    fed: Any = multiprocessing.get_context("spawn").Queue()
    target = tmp_path / "written"
    try:
        run = a_run(service)
        assert "inside a test run" in refused(run, lambda: simple.put(StartsWhenPickled(target)))
        assert not target.exists()
        service.unregister(run)
        listed = a_run(service, background=("process:multiprocessing",))
        outside(lambda: simple.put(StartsWhenPickled(target)))
        assert simple.get() == "pickled"
        assert target.exists()  # the gap: sh ran inside the decided put, and no line says so
        target.unlink()
        outside(lambda: fed.put(StartsWhenPickled(target)))  # pickled by its feeder, later
        eventually(lambda: any(x.get("reason") == "orphan" for x in sink_of(listed).lines))
        assert not target.exists()  # the feeder's pickling is decided: sh was refused
    finally:
        simple.close()
        fed.close()
        fed.join_thread()


@pytest.mark.design("service.loky_own_queues")
def test_a_plain_and_a_listed_outside_loky_task_run_while_a_run_is_open(
    service: state.ProcessState,
) -> None:
    """loky's queues have a feeder of their own: it sends what a decided put queued."""
    from joblib.externals.loky import get_reusable_executor

    executor = outside(lambda: get_reusable_executor(max_workers=1))
    try:
        # Made and warmed in a request that then ends: its threads now run outside any request.
        assert in_ended_request(lambda: executor.submit(os.getpid).result(30)) > 0
        run = a_run(service)
        assert plain(lambda: executor.submit(len, "abc").result(30)) == 3
        assert lines(run) == []
        service.unregister(run)
        listed = a_run(service, background=("process:multiprocessing",))
        assert outside(lambda: executor.submit(len, "abcd").result(30)) == 4
        assert not any(x.get("reason") == "orphan" for x in sink_of(listed).lines)
    finally:  # never waits on a stuck task, and ends its workers
        plain(lambda: executor.shutdown(wait=False, kill_workers=True))


@pytest.mark.design("service.pool_teardown")
@pytest.mark.filterwarnings("ignore:unclosed running multiprocessing pool:ResourceWarning")
def test_a_send_a_team_hook_makes_inside_a_pools_teardown_is_still_decided(
    service: state.ProcessState, tmp_path: Path
) -> None:
    """The teardown sends on the pool's own queues only; a team's warnings hook that sends
    from inside it is a send like any other."""
    import gc
    import warnings

    context = multiprocessing.get_context("spawn")
    mine, end = context.Pipe()
    target = tmp_path / "written"
    child = context.Process(target=consumer, args=("send", end, str(target)))
    plain(child.start)
    pool = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    workers = list(pool._pool)  # type: ignore[attr-defined]
    sent: list[str] = []
    shown = warnings.showwarning

    def hook(*args: Any, **kwargs: Any) -> None:
        try:
            mine.send("from the hook")
            sent.append("ran")
        except ScrimmageRefused:
            sent.append("refused")

    run = a_run(service)
    held = [pool]
    del pool
    warnings.showwarning = hook
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always", ResourceWarning)
            warnings.showwarning = hook
            under(run, lambda: (held.clear(), gc.collect()))
        assert sent == ["refused"], sent
        for worker in workers:
            worker.join(10)
            assert not worker.is_alive()  # the pool's own teardown still ran
        child.join(1)
        assert not target.exists()
    finally:
        warnings.showwarning = shown
        child.kill()
        child.join()


@pytest.mark.design("service.interpreters_refused")
@pytest.mark.skipif(not ON_314, reason="subinterpreters are new in Python 3.14")
def test_a_put_on_a_subinterpreters_queue_in_a_run_is_refused(
    service: state.ProcessState,
) -> None:
    from concurrent import interpreters  # type: ignore[attr-defined]

    queue = interpreters.create_queue()  # type: ignore[attr-defined]
    run = a_run(service)
    assert "inside a test run" in refused(run, lambda: queue.put("run's job"))
    assert "inside a test run" in refused(run, lambda: queue.put_nowait("run's job"))
    assert queue.empty()


@pytest.mark.design("service.interpreters_refused")
@pytest.mark.skipif(not ON_314, reason="subinterpreters are new in Python 3.14")
def test_a_thread_pool_submit_on_an_interpreter_pool_in_a_run_is_refused(
    service: state.ProcessState,
) -> None:
    """ThreadPoolExecutor.submit called on an InterpreterPoolExecutor skips its own submit:
    it is decided all the same."""
    from concurrent.futures import InterpreterPoolExecutor  # type: ignore[attr-defined]

    with InterpreterPoolExecutor(max_workers=1) as pool:
        run = a_run(service)
        assert "inside a test run" in refused(
            run, lambda: ThreadPoolExecutor.submit(pool, len, "x")
        )
        service.unregister(run)
        assert plain(lambda: pool.submit(len, "xy").result(10)) == 2  # decided once, not twice


@pytest.mark.design("service.resource_tracker")
@pytest.mark.parametrize("method", ["spawn", "forkserver"])
def test_the_shared_tracker_started_with_the_service_lets_a_run_make_a_lock_with_no_start(
    service: state.ProcessState, method: str
) -> None:
    """The service started the tracker before any request, so a lock or a queue a run makes
    registers with it and starts nothing."""
    from multiprocessing import resource_tracker

    assert resource_tracker._resource_tracker._pid is not None  # type: ignore[attr-defined]
    run = a_run(service)
    context = multiprocessing.get_context(method)
    made = under(run, lambda: (context.Lock(), context.Queue(), context.SimpleQueue()))
    assert len(made) == 3 and lines(run) == []


@pytest.mark.design("service.resource_tracker")
def test_a_tracker_start_or_relaunch_under_a_run_or_outside_is_decided(
    service: state.ProcessState, tmp_path: Path
) -> None:
    """Any later start of a tracker, such as a relaunch after it was killed, is a start of
    this Python: refused under a run, whatever folder or environment the run set."""
    from multiprocessing import resource_tracker

    target = tmp_path / "written"
    planted = tmp_path / "multiprocessing"
    planted.mkdir()
    (planted / "__init__.py").write_text(f"open({str(target)!r}, 'w').write('x')\n")
    python = os.path.basename(sys.executable)
    run = a_run(service)
    here = os.getcwd()
    tracker = resource_tracker.ResourceTracker()
    try:
        os.chdir(planted.parent)  # the run's folder holds a multiprocessing of its own
        assert f'Add "process:{python}"' in refused(run, tracker.ensure_running)
    finally:
        os.chdir(here)
    assert not target.exists()
    service.unregister(run)
    a_run(service)  # outside any request, with a run open: the outside rules
    assert "outside any request" in refused_outside(tracker.ensure_running)
    assert tracker._pid is None  # type: ignore[attr-defined]  # nothing was started


@pytest.mark.design("service.feeder_own_send")
def test_a_runs_send_on_a_queues_pipe_is_refused_whatever_its_feeder_dropped(
    service: state.ProcessState,
) -> None:
    """The feeder sends with its own send, made for that queue; the pipe's send stays
    decided, so an item the feeder could not pickle leaves nothing a run can use."""
    from multiprocessing.reduction import ForkingPickler

    queue: Any = multiprocessing.get_context("spawn").Queue()
    try:
        plain(lambda: queue.put(lambda: None))  # cannot be pickled: the feeder drops it
        plain(lambda: queue.put("plain"))
        assert queue.get(timeout=10) == "plain"
        run = a_run(service)
        data = bytes(ForkingPickler.dumps("run's data"))
        assert "inside a test run" in refused(run, lambda: queue._writer.send_bytes(data))
        # From a thread of the run's, at once beside a plain put: still refused.
        got: list[str] = []

        def raw() -> None:
            try:
                queue._writer.send("run's data")
                got.append("ran")
            except ScrimmageRefused:
                got.append("refused")

        worker = threading.Thread(target=raw)
        under(run, worker.start)
        plain(lambda: queue.put("plain again"))
        worker.join(10)
        assert got == ["refused"] and queue.get(timeout=10) == "plain again"
    finally:
        queue.close()
        queue.join_thread()


class SlowWhenPickled:
    """An item whose pickling waits, so a run can try a send meanwhile."""

    def __init__(self, started: Any, go: Any) -> None:
        self.started, self.go = started, go

    def __reduce__(self) -> tuple[type, tuple[str]]:
        self.started.set()
        self.go.wait(10)
        return (str, ("slow",))


@pytest.mark.design("service.feeder_own_send")
def test_a_runs_send_while_a_decided_item_is_pickled_is_refused(
    service: state.ProcessState,
) -> None:
    from multiprocessing.reduction import ForkingPickler

    queue: Any = multiprocessing.get_context("spawn").Queue()
    started, go = threading.Event(), threading.Event()
    try:
        listed = a_run(service, background=("process:multiprocessing",))
        outside(lambda: queue.put(SlowWhenPickled(started, go)))
        assert started.wait(10)  # the feeder is pickling the decided item
        service.unregister(listed)
        run = a_run(service)
        data = bytes(ForkingPickler.dumps("run's data"))
        assert "inside a test run" in refused(run, lambda: queue._writer.send_bytes(data))
        go.set()
        assert queue.get(timeout=10) == "slow"
    finally:
        go.set()
        queue.close()
        queue.join_thread()


@pytest.mark.design("service.pool_teardown")
def test_a_pools_own_queues_are_its_own_only_while_it_tears_down_and_only_its_own(
    service: state.ProcessState,
) -> None:
    """Outside its teardown, a send on a pool's own queue is decided; inside one pool's
    teardown, a send on another pool's queue is decided too."""
    first = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    second = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    try:
        run = a_run(service)
        assert "inside a test run" in refused(run, lambda: first._inqueue._writer.send(None))
        assert "inside a test run" in refused(run, lambda: first._change_notifier.put(None))
        torn: list[str] = []

        def inside_first(self: Any) -> None:
            try:
                second._outqueue._writer.send(None)
                torn.append("ran")
            except ScrimmageRefused:
                torn.append("refused")

        wrapped = process_gate.teardown(inside_first)
        under(run, lambda: wrapped(first))
        assert torn == ["refused"]
    finally:
        for each in (first, second):
            plain(each.terminate)
            stopped(each)


class TeamContext:
    """A context of the team's whose SimpleQueue hands back a queue the team made before."""

    def __init__(self, queue: Any) -> None:
        self.real = multiprocessing.get_context("spawn")
        self.queue = queue

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)

    def SimpleQueue(self) -> Any:  # multiprocessing's own name
        return self.queue


@pytest.mark.design("service.thread_pool_in_a_run")
def test_a_thread_pools_notifier_handed_in_by_a_context_is_never_its_own(
    service: state.ProcessState,
) -> None:
    team_queue: Any = multiprocessing.get_context("spawn").SimpleQueue()

    class Threads(multiprocessing.pool.ThreadPool):
        def __init__(self) -> None:
            multiprocessing.pool.Pool.__init__(self, 1, context=TeamContext(team_queue))

    pool = plain(Threads)
    try:
        run = a_run(service)
        assert pool._change_notifier is team_queue  # type: ignore[attr-defined]
        assert "inside a test run" in refused(run, lambda: team_queue.put("run's data"))
    finally:
        plain(pool.terminate)
        stopped(pool)


@pytest.mark.design("service.no_inherited_exemption")
def test_a_subclass_of_a_pools_thread_type_is_never_the_pools_own(
    service: state.ProcessState,
) -> None:
    from concurrent.futures import process

    kind: Any = process._ExecutorManagerThread  # type: ignore[attr-defined]

    class Team(kind):  # type: ignore[misc, valid-type]
        pass

    with state.internal_call():
        assert state.internal_thread(kind.__new__(kind))
        assert not state.internal_thread(Team.__new__(Team))


@pytest.mark.design("service.no_inherited_exemption")
def test_a_process_pools_notifier_is_no_pipe_within_this_process(
    service: state.ProcessState,
) -> None:
    pool = plain(lambda: multiprocessing.get_context("fork").Pool(1))
    try:
        assert not any(
            end in process_gate.IN_PROCESS_QUEUES
            for end in (
                pool._change_notifier,
                pool._change_notifier._writer,  # type: ignore[attr-defined]
            )
        )
    finally:
        plain(pool.terminate)
        stopped(pool)
