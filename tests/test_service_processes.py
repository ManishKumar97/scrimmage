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
import subprocess
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from scrimmage_core import process_gate, state, threadctx
from scrimmage_core.errors import ScrimmageRefused
from scrimmage_core.plan import Plan
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


def a_run(process: state.ProcessState, real: tuple[str, ...] = ()) -> state.RunState:
    sink = Sink()
    plan = Plan("run-0123456789ab", "t", "baseline", (), (), (), real, 60.0, service=True)
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
            plain(lambda c=call, t=target: c(t))
            assert (tmp_path / "plain").exists(), name
            (tmp_path / "plain").unlink()
        # A refusal after the work was queued would let the pool run it once woken: by now
        # the plain calls woke it many times, and the run's work must still never have run.
        assert not (tmp_path / "written").exists()
    finally:
        executor.shutdown()
        pool.terminate()
        pool.join()


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


def consumer(kind: str, end: Any, path: str) -> None:
    """A process made before the run: it writes whatever it is sent to the file."""
    got = (
        end.get()
        if kind in ("queue", "simple")
        else (end.recv() if kind == "send" else end.recv_bytes())
    )
    Path(path).write_text(str(got))


@pytest.mark.design("service.send_points")
@pytest.mark.parametrize("kind", ["queue", "simple", "send", "send_bytes"])
def test_multiprocessing_send_points_refuse_under_a_run(
    service: state.ProcessState, tmp_path: Path, kind: str
) -> None:
    context = multiprocessing.get_context("spawn")
    mine: Any
    end: Any
    if kind == "queue":
        end = mine = context.Queue()
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
        pool.join()
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
