"""A web request sent around the gate leaves a notice: library, host and method, nothing more."""

import http.client
import http.server
import json
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scrimmage import ungated
from scrimmage.client import ControlClient
from scrimmage.control import ControlServer
from scrimmage.records import RecordStore
from test_bootstrap import TOKEN, joined, plan, python

SECRET = "tok_5ecret_value"
pytestmark = pytest.mark.design("ungated.notice")


class Echo(http.server.BaseHTTPRequestHandler):
    """Answers with the method and the body it got, so a result can be compared byte for byte."""

    def reply(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size) if size else b""
        data = json.dumps({"method": self.command, "body": body.decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = reply

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@pytest.fixture
def web() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlServer]:
    server = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    server.start()
    yield server
    server.stop()


def notices(control: ControlServer) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], line)
        for line in control.store.lines("run-1")
        if line.get("notice") == "ungated"
    ]


def run_agent(control: ControlServer, code: str, **env: str) -> list[dict[str, Any]]:
    """Run the agent code under a run. Its values come by the environment, because the
    program's command line is in the record."""
    control.open_run("run-1", "baseline", plan())
    done = python(code, joined(control, **env))
    assert done.returncode == 0, done.stderr
    return notices(control)


ENV = "import os; URL = os.environ['U']; PATHQ = os.environ['Q']\n"
CLIENTS = {
    "requests": (
        "import requests; print(requests.post(URL + PATHQ, data='x').text)",
        "POST",
    ),
    "urllib3": (
        "import urllib3; print(urllib3.PoolManager().request('PUT', URL + PATHQ, body=b'x').data)",
        "PUT",
    ),
    "urllib.request": (
        "import urllib.request as u\n"
        "print(u.urlopen(u.Request(URL + PATHQ, data=b'x', method='DELETE')).read())",
        "DELETE",
    ),
    "aiohttp": (
        "import aiohttp, asyncio\n"
        "async def go():\n"
        "    async with aiohttp.ClientSession() as s:\n"
        "        async with s.post(URL + PATHQ, data='x') as r:\n"
        "            print(await r.text())\n"
        "asyncio.run(go())",
        "POST",
    ),
}


@pytest.mark.slow
@pytest.mark.parametrize("library", sorted(CLIENTS))
def test_a_request_through_an_ungated_client_writes_one_notice(
    control: ControlServer, web: str, library: str
) -> None:
    code, method = CLIENTS[library]
    [notice] = run_agent(control, ENV + code, U=web, Q=f"/pay?token={SECRET}")
    assert (notice["library"], notice["host"], notice["method"]) == (library, "127.0.0.1", method)
    assert "ran for real" in notice["message"]


@pytest.mark.slow
@pytest.mark.parametrize("library", sorted(CLIENTS))
def test_no_part_of_the_path_or_query_reaches_the_record(
    control: ControlServer, web: str, library: str
) -> None:
    code, _ = CLIENTS[library]
    run_agent(control, ENV + code, U=web, Q=f"/pay?token={SECRET}")
    record = json.dumps(control.store.lines("run-1"))
    assert SECRET not in record and "/pay" not in record


@pytest.mark.slow
def test_a_get_says_it_was_not_gated(control: ControlServer, web: str) -> None:
    code = f"import requests; requests.get({web!r} + '/a')"
    [notice] = run_agent(control, code)
    assert notice["method"] == "GET"
    assert "it was not gated" in notice["message"] and "ran for real" not in notice["message"]


@pytest.mark.slow
def test_a_repeated_request_is_one_notice_and_a_new_method_is_another(
    control: ControlServer, web: str
) -> None:
    code = f"import requests\nfor _ in range(5): requests.get({web!r})\nrequests.post({web!r})\n"
    found = run_agent(control, code)
    assert [n["method"] for n in found] == ["GET", "POST"]


@pytest.mark.slow
def test_an_httpx_request_writes_no_notice_because_it_is_gated(
    control: ControlServer, web: str
) -> None:
    code = f"import httpx\ntry:\n    httpx.get({web!r})\nexcept Exception:\n    pass\n"
    assert run_agent(control, code) == []


@pytest.mark.slow
def test_importing_the_clients_without_a_request_writes_no_notice(control: ControlServer) -> None:
    code = "import requests, urllib3, urllib.request, aiohttp, http.client"
    assert run_agent(control, code) == []


@pytest.mark.slow
def test_the_observer_is_installed_inside_a_run_only(control: ControlServer) -> None:
    code = (
        "import http.client; "
        "print(hasattr(http.client.HTTPConnection.putrequest, '__scrimmage_ungated__'))"
    )
    outside = python(code, {"PYTHONPATH": joined(control)["PYTHONPATH"]})
    assert outside.stdout.strip() == "False"
    control.open_run("run-1", "baseline", plan())
    assert python(code, joined(control)).stdout.strip() == "True"


class Run:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str, dict[str, object] | None]] = []

    def notice(
        self, kind: str, library: str, message: str, extra: dict[str, object] | None = None
    ) -> None:
        self.lines.append((kind, library, message, extra))


class Process:
    def __init__(self, run: Run | None = None) -> None:
        self.run = run

    def current(self) -> Run | None:
        return self.run

    def notice(self, kind: str, library: str, message: str) -> None:
        raise AssertionError("a run is open")


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> Run:
    run = Run()
    observer = ungated.Observer(cast(Any, Process(run)))
    monkeypatch.setattr(
        http.client.HTTPConnection, "putrequest", http.client.HTTPConnection.putrequest
    )
    ungated.patch_http_client(
        cast(Any, SimpleNamespace(HTTPConnection=http.client.HTTPConnection)), observer
    )
    return run


def test_the_control_channel_is_never_reported_and_the_agents_next_request_is(
    control: ControlServer, web: str, watched: Run
) -> None:
    client = ControlClient(control.url, TOKEN)
    client.request("POST", "/v1/ping")
    assert watched.lines == []
    urllib.request.urlopen(web).read()  # the agent, in the same thread, right after
    [(kind, library, _, extra)] = watched.lines
    assert (kind, library) == ("ungated", "urllib.request")
    assert extra is not None and extra["host"] == "127.0.0.1"


def test_a_failing_observer_lets_the_request_through(
    web: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def current(self) -> None:
            raise RuntimeError("the observer failed")

    monkeypatch.setattr(
        http.client.HTTPConnection, "putrequest", http.client.HTTPConnection.putrequest
    )
    observer = ungated.Observer(cast(Any, Broken()))
    module = cast(Any, SimpleNamespace(HTTPConnection=http.client.HTTPConnection))
    ungated.patch_http_client(module, observer)
    assert json.loads(urllib.request.urlopen(web).read())["method"] == "GET"


def test_the_result_is_byte_for_byte_the_same_with_and_without_the_observer(
    web: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def call() -> bytes:
        request = urllib.request.Request(web, data=b"payload", method="POST")
        return urllib.request.urlopen(request).read()

    without = call()
    run = Run()
    monkeypatch.setattr(
        http.client.HTTPConnection, "putrequest", http.client.HTTPConnection.putrequest
    )
    observer = ungated.Observer(cast(Any, Process(run)))
    ungated.patch_http_client(
        cast(Any, SimpleNamespace(HTTPConnection=http.client.HTTPConnection)), observer
    )
    assert call() == without and len(run.lines) == 1
