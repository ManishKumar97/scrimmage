import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fake_jaeger import fake_jaeger, hexed, shifted
from spans import write_trace

from faultline import cli
from faultline.config import Config
from faultline.serve import FindingsServer

SECOND = 1_000_000_000
FAILED = {"name": "get_logs", "arguments": "{}", "error": "ConnectionError"}


@contextmanager
def serving(*specs: str, service: str | None = None) -> Iterator[str]:
    server = FindingsServer(("127.0.0.1", 0), specs, service, Config())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def get(url: str, method: str = "GET") -> tuple[int, str, dict]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (
                response.status,
                response.headers["Content-Type"],
                json.load(response),
            )
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], json.load(error)


def find_json(capsys, *args: str) -> dict:
    assert cli.main(["find", "--json", *args]) == 0
    return json.loads(capsys.readouterr().out)


def two_windows(path: Path) -> Path:
    """A run two hours old that hid a tool error, and a run a minute old."""
    hours_ago = time.time_ns() - 2 * 3600 * SECOND
    write_trace(path, {"calls": [FAILED]}, {"text": "fine"}, trace="a1", base=hours_ago)
    minute_ago = time.time_ns() - 60 * SECOND
    write_trace(path, {"calls": [FAILED]}, {"text": "?"}, trace="b2", base=minute_ago)
    return path


def test_findings_for_a_window_are_what_find_prints_for_it(tmp_path, capsys):
    path = two_windows(tmp_path / "spans.jsonl")

    with serving(f"file:{path}") as url:
        status, kind, hour = get(f"{url}/api/findings?since=1h")
        _, _, every = get(f"{url}/api/findings")

    assert (status, kind) == (200, "application/json")
    assert hour == find_json(capsys, "--since", "1h", str(path))
    assert every == find_json(capsys, str(path))
    assert [run["trace_id"] for run in hour["runs"]] == ["b2"]
    assert hour["summary"]["faults"]["tool_error"] == 1
    assert every["summary"]["runs"] == 2


def test_findings_from_jaeger_are_what_find_prints_for_jaeger(tmp_path, capsys):
    path = write_trace(tmp_path / "s.jsonl", {"calls": [FAILED]}, {"text": "ok"})
    spans = shifted(hexed(path), time.time_ns() - 60 * SECOND)
    with fake_jaeger() as jaeger:
        jaeger.add(spans)
        spec = f"jaeger:{jaeger.url}"
        with serving(spec, service="agent") as url:
            status, _, served = get(f"{url}/api/findings?since=30m")
            missing, _, error = get(f"{url}/api/findings")
        printed = find_json(
            capsys, "--source", spec, "--service", "agent", "--since", "30m"
        )

    assert status == 200
    assert served == printed
    assert served["summary"]["faults"]["tool_error"] == 1
    assert missing == 400
    assert "needs a window: add ?since=" in error["error"]


def test_health_answers_ok(tmp_path):
    with serving(f"file:{tmp_path}") as url:
        assert get(f"{url}/api/health") == (200, "application/json", {"status": "ok"})


@pytest.mark.parametrize(
    ("path", "method", "status"),
    [
        ("/", "GET", 404),
        ("/api/finding", "GET", 404),
        ("/api/findings?since=soon", "GET", 400),
        ("/api/findings?since=", "GET", 400),
        ("/api/findings?since=99999d", "GET", 400),
        ("/api/findings", "POST", 501),
        ("/api/findings", "DELETE", 501),
    ],
)
def test_anything_else_is_an_error_in_json(tmp_path, path, method, status):
    with serving(f"file:{tmp_path}") as url:
        code, kind, body = get(f"{url}{path}", method)

    assert (code, kind) == (status, "application/json")
    assert body["status"] == status
    assert body["error"]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("since=", "since= is empty; give a window, for example ?since=1h"),
        ("since", "since= is empty"),
        ("since=soon", "query parameter since: bad duration 'soon'"),
    ],
)
def test_a_bad_since_says_what_to_send(tmp_path, query, message):
    with serving(f"file:{tmp_path}") as url:
        status, _, body = get(f"{url}/api/findings?{query}")

    assert status == 400
    assert message in body["error"]


def test_head_answers_the_status_and_headers_without_a_body(tmp_path):
    with serving(f"file:{tmp_path}") as url:
        host, port = url.removeprefix("http://").split(":")
        answers = []
        for path in ("/api/health", "/nope"):
            connection = http.client.HTTPConnection(host, int(port), timeout=10)
            connection.request("HEAD", path)
            response = connection.getresponse()
            answers.append((response.status, response.read()))
            assert int(response.headers["Content-Length"]) > 0
            connection.close()

    assert answers == [(200, b""), (404, b"")]


def test_an_unexpected_error_is_a_json_500_and_logged(tmp_path, monkeypatch, capsys):
    def broken(self, since):
        raise RuntimeError("a bug in the engine")

    monkeypatch.setattr(FindingsServer, "report", broken)
    with serving(f"file:{tmp_path}") as url:
        status, kind, body = get(f"{url}/api/findings")
        health = get(f"{url}/api/health")[0]

    assert (status, kind) == (500, "application/json")
    assert body == {"error": "internal error", "status": 500}
    assert "RuntimeError: a bug in the engine" in capsys.readouterr().err
    assert health == 200  # the server keeps answering


def test_a_source_that_cannot_be_read_is_a_bad_gateway(tmp_path):
    with serving(f"file:{tmp_path / 'missing.jsonl'}") as url:
        status, _, body = get(f"{url}/api/findings")

    assert status == 502
    assert "missing.jsonl" in body["error"]


def test_serve_checks_its_sources_before_it_listens(capsys):
    assert cli.main(["serve", "--source", "jaeger"]) == 2
    assert "needs --service" in capsys.readouterr().err
    assert cli.main(["serve", "--source", "kafka:x"]) == 2
    assert "known schemes" in capsys.readouterr().err
