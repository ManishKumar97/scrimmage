"""In a service the request is told to the runs it could belong to, once each."""

import contextvars
import threading
from collections.abc import Callable

import pytest

from scrimmage import state, ungated
from test_service_units import sink_of
from test_threadctx import a_run, joined, service  # noqa: F401 - a fixture

pytestmark = pytest.mark.design("ungated.service")


def told(run: state.RunState) -> list[str]:
    return [str(x["host"]) for x in sink_of(run).lines if x.get("notice") == "ungated"]


def send(observer: ungated.Observer, times: int = 1) -> None:
    for _ in range(times):
        observer.observe("requests", "api.acme.com", "POST")


def test_a_request_from_code_outside_any_request_is_told_to_every_open_run(
    service: state.ProcessState,  # noqa: F811
) -> None:
    runs = [a_run(service, "run-a"), a_run(service, "run-b")]
    observer = ungated.Observer(service)
    contextvars.Context().run(send, observer)
    assert [told(r) for r in runs] == [["api.acme.com"], ["api.acme.com"]]


def test_a_thread_whose_context_was_lost_tells_its_run_once(
    service: state.ProcessState,  # noqa: F811
) -> None:
    run = a_run(service, "run-a")
    observer = ungated.Observer(service)

    def lost() -> None:
        thread = threading.Thread(target=lambda: contextvars.Context().run(send, observer, 5))
        thread.start()
        thread.join()

    joined(run).run(lost)
    assert told(run) == ["api.acme.com"]


def test_a_request_in_a_run_is_told_once_however_often_it_is_sent(
    service: state.ProcessState,  # noqa: F811
) -> None:
    run = a_run(service, "run-a")
    other = a_run(service, "run-b")
    observer = ungated.Observer(service)
    joined(run).run(send, observer, 4)
    assert (told(run), told(other)) == (["api.acme.com"], [])


@pytest.mark.parametrize("times", [1, 3])
def test_a_request_with_no_run_open_is_told_once_to_the_process(
    service: state.ProcessState,  # noqa: F811
    times: int,
) -> None:
    observer = ungated.Observer(service)
    context: Callable[..., None] = contextvars.Context().run  # type: ignore[assignment]
    context(send, observer, times)
    assert [x["notice"] for x in service.report] == ["ungated"]


def test_a_lost_thread_of_a_closed_run_prints_the_request_and_no_refusal(
    service: state.ProcessState,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = a_run(service, "run-a")
    observer = ungated.Observer(service)
    run.refused = state.CLOSED

    def lost() -> None:
        thread = threading.Thread(
            target=lambda: contextvars.Context().run(
                observer.observe, "requests", "api.acme.com", "POST"
            )
        )
        thread.start()
        thread.join()

    joined(run).run(lost)
    err = capsys.readouterr().err
    assert "requests sent a POST request to api.acme.com around the gate" in err
    assert "a write there is not stopped by Scrimmage" in err
    assert "is refused" not in err
    assert [x["notice"] for x in service.report] == ["ungated"]


@pytest.mark.parametrize("how", ["closed", "gone", "open"])
def test_a_notice_from_a_lost_thread_of_a_closed_run_prints_no_refusal(
    service: state.ProcessState,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    how: str,
) -> None:
    """A library first imported in such a thread, as http_gate.py does, calls this directly."""
    run = a_run(service, "run-a")
    closed = how != "open"
    if how == "closed":
        run.refused = state.CLOSED
    if how == "gone":  # the thread is tagged, but its run is no longer registered
        service.unregister(run)

    def lost() -> None:
        thread = threading.Thread(
            target=lambda: contextvars.Context().run(
                service.notice, "newer_version", "httpx", "httpx 9.0 is newer than tested"
            )
        )
        thread.start()
        thread.join()

    joined(run).run(lost)
    err = capsys.readouterr().err
    assert "is refused" not in err
    if closed:
        assert "scrimmage: httpx: httpx 9.0 is newer than tested" in err
        assert [x["notice"] for x in service.report] == ["newer_version"]
    else:
        assert [x["notice"] for x in told_lines(run)] == ["newer_version"]
        assert service.report == []


def told_lines(run: state.RunState) -> list[dict[str, object]]:
    return [x for x in sink_of(run).lines if "notice" in x]


def test_a_close_between_the_checks_does_not_print_a_refusal(
    service: state.ProcessState,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run reads open once, then closed: as when a timer thread closes it mid-notice. The
    open reading decides, so the line goes to the run and nothing is called refused."""
    run = a_run(service, "run-a")
    reads = iter([None])  # the first read sees the run open, every later one sees it closed

    class Closing(type(run)):  # type: ignore[misc]
        @property
        def refused(self) -> str | None:
            return None if next(reads, state.CLOSED) is None else state.CLOSED

        @refused.setter
        def refused(self, value: str | None) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
            pass

    object.__setattr__(run, "__class__", Closing)
    got: list[str] = []
    run.notice = lambda kind, library, message, **_: got.append(message)  # type: ignore[method-assign]

    def lost() -> None:
        thread = threading.Thread(
            target=lambda: contextvars.Context().run(service.notice, "newer_version", "httpx", "m")
        )
        thread.start()
        thread.join()

    joined(run).run(lost)
    assert "is refused" not in capsys.readouterr().err
    assert got == ["m"]
