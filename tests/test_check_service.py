"""scrimmage check --service, the clock gap, and the /openapi.json template check.

Real uvicorn processes under scrimmage-run, as test_service.py starts them. Where a test needs
a document the framework would not write, or a service clock that is off, it starts a small
FastAPI app of its own (LOOSE) that answers whatever document the test gives it, logs every
request it receives, and moves the gate's wall clock when told to.
"""

import asyncio
import dataclasses
import json
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from ports import free_port
from scrimmage_core import asgi_gate, cli, config, protocol, service_client, versions
from service_harness import Service, drive
from test_cli_json import validate
from test_service import FILE, TOOLS, Bench, bench

pytestmark = pytest.mark.slow
__all__ = ["bench"]  # a fixture, used by name

LOOSE = '''
"""A service that takes any JSON body at /chat, whatever its document says."""

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request

from acme.agent import answer

LOG = os.environ["REQUEST_LOG"]
TOKEN = os.environ.get("GUARD_TOKEN")
DOC = json.loads(os.environ["OPENAPI_DOC"]) if os.environ.get("OPENAPI_DOC") else None
SHIFT = float(os.environ.get("GATE_CLOCK_SHIFT", "0"))

if SHIFT:
    from scrimmage_core import asgi_gate

    class Shifted:
        """The gate's view of the time module, with the wall clock moved."""

        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return time.time() + SHIFT

    asgi_gate.time = Shifted()


async def chat(request: Request) -> dict[str, str]:
    body = await request.json()
    reply = await asyncio.get_running_loop().run_in_executor(
        None, answer, str(body.get("message") or "hello")
    )
    return {"reply": reply}


def build() -> FastAPI:
    api = FastAPI(
        openapi_url=None if os.environ.get("NO_DOC") else "/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    api.post("/chat")(chat)
    if DOC is not None:
        api.openapi = lambda: DOC
    return api


if os.environ.get("MOUNT"):
    outer = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    outer.mount("/api", build())
    inner = outer
else:
    inner = build()


class Logged:
    """Logs each request after it is served, and demands the bearer token when one is set."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks = []

        async def spy():
            message = await receive()
            chunks.append(message.get("body", b""))
            return message

        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        try:
            if TOKEN and headers.get("authorization") != f"Bearer {TOKEN}":
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"bad token"})
                return None
            return await self.app(scope, spy, send)
        finally:
            line = {
                "method": scope["method"],
                "path": scope["path"],
                "headers": headers,
                "body": b"".join(chunks).decode(),
            }
            with open(LOG, "a") as handle:
                handle.write(json.dumps(line) + "\\n")


app = Logged(inner)
'''

LABELS = ("service", "version", "python", "tools", "control", "clock", "template", "notices")
TOKEN = "bearer-token-for-the-openapi-get"


def document(schema: dict[str, Any], components: dict[str, Any] | None = None) -> dict[str, Any]:
    """An OpenAPI document with one operation, POST /chat, taking the given JSON schema."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/chat": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema}},
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {"schemas": components or {}},
    }


NEEDS_USER = {
    "type": "object",
    "required": ["message", "user"],
    "properties": {"message": {"type": "string"}, "user": {"type": "string"}},
}


NEEDS_USER_WARNING = (
    '[agent] body: /openapi.json says POST /chat requires "user"; the template has no such field'
)


class Lab:
    """A bench, the loose service's files, and the file scrimmage reads."""

    def __init__(self, bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bench = bench
        self.root = bench.root
        self.log = self.root / "requests.jsonl"
        (self.root / "loose.py").write_text(LOOSE)
        monkeypatch.setenv("FILE_TOKEN", TOKEN)  # what the file's header is expanded from

    def start(self, doc: dict[str, Any] | None = None, *, guard: bool = False, **env: str) -> int:
        """The loose service, answering the given document. guard: it demands the bearer
        token on every request. Its port."""
        more = dict(env, REQUEST_LOG=str(self.log))
        if guard:
            more["GUARD_TOKEN"] = TOKEN
        if doc is not None:
            more["OPENAPI_DOC"] = json.dumps(doc)
        return self.bench.start(app="loose:app", **cast(dict[str, Any], more)).port

    def file(
        self,
        port: int,
        path: str = "/chat",
        body: str = 'body    = {message = "{question}", session = "{session}"}',
        extra: str = 'headers = {Authorization = "Bearer ${FILE_TOKEN}"}',
    ) -> Path:
        text = FILE.replace("PORT", str(port)).replace("PATH", path)
        text = text.replace("TOOL_LIST", json.dumps(TOOLS.split(",")))
        text = text.replace("RUNS", "1").replace("ANSWER", "reply").replace("TIMEOUT", "30")
        text = text.replace("WAIT", "10").replace("GRACE", "0.2").replace("EXTRA", extra)
        text = text.replace('body    = {message = "{question}", session = "{session}"}', body)
        target = self.root / "service.toml"
        target.write_text(text)
        return target

    def check(self, path: Path, *flags: str) -> tuple[int, str]:
        self.bench.capsys.readouterr()
        code = cli.main(["check", str(path), "--service", *flags])
        out = self.bench.capsys.readouterr()
        return code, out.out + out.err

    def requests(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def lab(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return Lab(bench, monkeypatch)


def line_of(out: str, label: str) -> str:
    """The text of the report line with this label (the line's first row only)."""
    found = [x for x in out.splitlines() if x.startswith(f"{label:<12}")]
    assert len(found) == 1, out
    return found[0][12:]


# --- check --service -----------------------------------------------------------------------


def test_check_service_prints_the_startup_report_and_the_clock_gap(lab: Lab) -> None:
    service = lab.bench.start()  # the example service: real FastAPI, real uvicorn
    code, out = lab.check(lab.file(service.port))
    assert code == 0, out
    order = [x[:12].strip() for x in out.splitlines() if x[:12].strip() in LABELS]
    assert order == list(LABELS), out
    bench = lab.bench
    assert re.fullmatch(
        rf"POST http://127\.0\.0\.1:{service.port}/chat   gated \(.*asgi.*\), pid \d+",
        line_of(out, "service"),
    ), out
    ours = versions.installed()
    assert line_of(out, "version") == f"scrimmage {ours} in the service and here"
    assert line_of(out, "python").startswith(sys.version.split()[0]) and " at /" in out
    names = "check_inventory, get_order, get_stock, refund_order"
    assert line_of(out, "tools") == f"{names}   (match the file)"
    assert re.fullmatch(
        rf"{re.escape(bench.control)} reached, no TLS, on this machine, this runner, \d+ ms",
        line_of(out, "control"),
    ), out
    assert re.fullmatch(
        r"the service is \d+\.\d s (ahead of|behind) this machine", line_of(out, "clock")
    ), out
    assert line_of(out, "template") == "fits POST /chat in /openapi.json"
    assert line_of(out, "notices") == "none"


def test_check_service_prints_one_version_line(lab: Lab) -> None:
    service = lab.bench.start()
    theirs = versions.installed()
    code, out = lab.check(lab.file(service.port))
    assert code == 0, out
    assert line_of(out, "version") == f"scrimmage {theirs} in the service and here"
    # A runner of another version: the one line names both, and nothing else says so.
    lab.bench.monkeypatch.setattr(versions, "installed", lambda: "9.9.9")
    code, out = lab.check(lab.file(service.port))
    assert code == 2, out
    said = (
        f"the service runs scrimmage {theirs} and this runner is 9.9.9; "
        "install scrimmage 9.9.9 in the service"
    )
    assert line_of(out, "version") == said, out
    assert out.count(f"scrimmage {theirs} and this runner") == 1, out  # said once, on its line
    assert "versions" not in out, out


def test_check_without_service_still_reads_the_file_only(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.5)
    try:
        path = lab.file(listener.getsockname()[1])
        capsys.readouterr()
        assert cli.main(["check", str(path)]) == 0
        assert "is valid" in capsys.readouterr().out
        with pytest.raises(TimeoutError):  # nothing connected: not a probe, not a GET
            listener.accept()
        with pytest.raises(SystemExit) as stopped:
            cli.main(["check", str(path), "--strict"])
        assert stopped.value.code == 2
        assert "--strict needs --service" in capsys.readouterr().err
    finally:
        listener.close()
    assert not (lab.root / "scrimmage-runs").exists()


def test_check_service_spends_nothing_and_sends_no_run(lab: Lab) -> None:
    port = lab.start()
    code, out = lab.check(lab.file(port))
    assert code == 0, out
    assert lab.bench.fake.requests == []  # no model call
    assert not (lab.root / "scrimmage-runs").exists()  # no records folder, no setup record
    seen = lab.requests()
    assert [(r["method"], r["path"]) for r in seen] == [("GET", "/openapi.json")]  # no POST


def test_check_service_json_matches_the_schema(lab: Lab) -> None:
    port = lab.start(document(NEEDS_USER))
    path = lab.file(port)
    lab.bench.capsys.readouterr()
    code = cli.main(["check", str(path), "--service", "--json"])
    doc = json.loads(lab.bench.capsys.readouterr().out)
    assert code == 0 and doc["exit"] == 0 and doc["status"] == "valid", doc
    validate(doc)
    service = doc["service"]
    assert service["tools_match"] is True and service["sdk"] == service["runner_sdk"]
    assert service["template"]["warnings"] == [NEEDS_USER_WARNING]
    assert [m["message"] for m in doc["warnings"]] == service["template"]["warnings"]
    assert abs(service["clock_gap_s"]) < 5


def test_check_service_prints_every_line_when_one_is_wrong(lab: Lab) -> None:
    port = lab.start(SCRIMMAGE_TOOLS="acme.tools:get_order")
    code, out = lab.check(lab.file(port))
    assert code == 2, out
    assert "the service gates the tools ['get_order']" in line_of(out, "tools")
    for label in ("service", "version", "python", "control", "clock", "template", "notices"):
        line_of(out, label)  # still printed, once


# --- the clock -----------------------------------------------------------------------------


def test_a_clock_gap_of_ten_seconds_warns_and_fifty_stops_the_test(lab: Lab) -> None:
    # The gate's probe clock is moved, not only the report's server_time: a gap of 12 s is
    # inside the 60 s window, so the probe works and the test goes on.
    port = lab.start(GATE_CLOCK_SHIFT="12")
    code, out = lab.bench.test(port)
    assert code == 0, out
    assert re.search(r"warning: the service is 1[12]\.\d s ahead of this machine", out), out
    checked_code, shown = lab.check(lab.file(port))
    assert checked_code == 0, shown
    assert re.fullmatch(
        r"the service is 1[12]\.\d s ahead of this machine", line_of(shown, "clock")
    )
    # At 55 s the probe still passes, and the test stops before the setup request.
    far = lab.start(GATE_CLOCK_SHIFT="55")
    before = len(lab.requests())
    records = set((lab.root / "scrimmage-runs").glob("*/*.jsonl"))
    model_calls = len(lab.bench.fake.requests)
    code, out = lab.bench.test(far)
    assert code == 3, out
    assert re.search(r"the service is (54|55)\.\d s ahead of this machine", out), out
    assert "stops from 50 s" in out
    assert set((lab.root / "scrimmage-runs").glob("*/*.jsonl")) == records  # no setup, no run
    assert len(lab.bench.fake.requests) == model_calls
    assert [r["method"] for r in lab.requests()[before:]] == []  # nothing reached the app
    code, shown = lab.check(lab.file(far))
    assert code == 3 and "stops from 50 s" in shown, shown


def test_a_gap_above_the_window_gets_a_signed_clock_reply_and_exit_3_naming_the_gap(
    lab: Lab,
) -> None:
    port = lab.start(GATE_CLOCK_SHIFT="65")
    code, out = lab.bench.test(port)
    assert code == 3, out
    assert re.search(r"the service is (64|65)\.\d s ahead of this machine", out), out
    assert "x-scrimmage-run does not reach Scrimmage" not in out  # not read as an ungated service
    assert lab.requests() == []  # the app never saw the stale probe
    assert lab.bench.fake.requests == []
    code, shown = lab.check(lab.file(port))
    assert code == 3 and re.search(r"(64|65)\.\d s ahead", shown), shown
    # A service behind this machine says so the other way.
    behind = lab.start(GATE_CLOCK_SHIFT="-70")
    code, out = lab.bench.test(behind)
    assert code == 3 and re.search(r"the service is (69|70)\.\d s behind this machine", out), out


class Shifted:
    """The gate's view of the time module, with the wall clock moved."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def time(self) -> float:
        return time.time() + self.seconds


async def plain_app(scope: Any, receive: Any, send: Any) -> None:
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"x-app", b"1")]})
    await send({"type": "http.response.body", "body": b"app"})


def stale_probe(svc: Service, age: float = 65.0) -> tuple[list[tuple[bytes, bytes]], str]:
    signed = protocol.request_headers(svc.token, "POST", "/chat", b"", time.time() - age)
    headers = [
        (b"x-scrimmage-run", b"probe-0123456789ab"),
        (b"x-scrimmage-probe", b"1"),
        *[(k.lower().encode(), v.encode()) for k, v in signed.items()],
    ]
    return headers, signed[protocol.SIGNATURE]


def test_a_replayed_stale_probe_is_answered_as_documented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = Service(tmp_path / "records")
    try:
        headers, request_signature = stale_probe(svc)
        entry = asgi_gate.Entry(plain_app)

        def send() -> Any:
            return asyncio.run(drive(entry, headers=headers, body=b""))

        def is_clock_reply(reply: Any, expected_time: float) -> None:
            assert reply.status == 409 and reply.headers["x-scrimmage-probe"] == "clock"
            said = json.loads(reply.body)
            assert said["error"] == "clock" and abs(said["server_time"] - expected_time) < 5
            signature = protocol.sign_reply(svc.token, request_signature, 409, reply.body)
            assert protocol.same(reply.headers[protocol.SIGNATURE.lower()], signature)

        is_clock_reply(send(), time.time())  # a stale, genuine probe: the signed clock reply
        replayed = send()  # the same worker, within 120 s, has the nonce: a plain request
        assert replayed.status == 200 and replayed.body == b"app"
        assert "x-scrimmage-probe" not in replayed.headers
        # Another worker keeps its own nonces, so the replay gets the signed reply there too:
        # the leak the docs state.
        first = svc.gate
        other = dataclasses.replace(first, nonces=protocol.NonceStore())
        asgi_gate.GATE = other
        try:
            is_clock_reply(send(), time.time())
        finally:
            asgi_gate.GATE = first
        # The same worker after it has forgotten the nonce (after 120 s) answers it again.
        monkeypatch.setattr(asgi_gate, "time", Shifted(130))
        is_clock_reply(send(), time.time() + 130)
    finally:
        svc.close()


# --- the template --------------------------------------------------------------------------


def template_line(out: str) -> str:
    return line_of(out, "template")


def test_a_template_missing_a_required_field_warns(lab: Lab) -> None:
    port = lab.start(document(NEEDS_USER))
    code, out = lab.bench.test(port)  # the warning never stops the test
    assert code == 0, out
    warning = NEEDS_USER_WARNING
    assert f"warning: {warning}" in out, out
    assert "claims received 2" in out  # the setup request and the run both went ahead


def test_a_template_with_an_extra_field_against_additional_properties_false_warns(
    lab: Lab,
) -> None:
    schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    }
    code, out = lab.check(lab.file(lab.start(document(schema))))
    assert code == 0, out
    assert template_line(out) == (
        '[agent] body: /openapi.json says POST /chat allows no field "session"; the template has it'
    )


def test_a_template_with_a_wrong_type_warns(lab: Lab) -> None:
    schema = {"type": "object", "properties": {"message": {"type": "integer"}}}
    code, out = lab.check(lab.file(lab.start(document(schema))))
    assert code == 0, out
    assert template_line(out) == (
        '[agent] body: /openapi.json says POST /chat wants field "message" as integer; '
        "the template has string"
    )


def test_a_nullable_field_is_checked_through_its_non_null_branch(lab: Lab) -> None:
    wrong = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    right = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    for message, fits in ((wrong, False), (right, True)):
        schema = {"type": "object", "properties": {"message": message}}
        code, out = lab.check(lab.file(lab.start(document(schema))))
        assert code == 0, out
        if fits:
            assert template_line(out) == "fits POST /chat in /openapi.json"
        else:
            assert 'wants field "message" as integer' in template_line(out), out


def test_strict_makes_a_misfit_exit_2(lab: Lab) -> None:
    path = lab.file(lab.start(document(NEEDS_USER)))
    code, out = lab.check(path)
    assert code == 0 and "requires" in template_line(out), out
    code, out = lab.check(path, "--strict")
    assert code == 2 and 'requires "user"' in template_line(out), out
    fits = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    }
    code, out = lab.check(lab.file(lab.start(document(fits))), "--strict")
    assert code == 0, out  # --strict is for a misfit only


def test_no_openapi_document_is_one_note(lab: Lab) -> None:
    port = lab.start(NO_DOC="1")
    code, out = lab.check(lab.file(port), "--strict")  # a note is not a misfit
    assert code == 0, out
    assert template_line(out) == (
        "[agent] body: /openapi.json: no document at GET /openapi.json; not checked"
    )
    assert [(r["method"], r["path"]) for r in lab.requests()] == [("GET", "/openapi.json")]
    code, out = lab.bench.test(port)
    assert code == 0 and "no document at GET /openapi.json" in out, out


def test_a_service_under_a_prefix_is_found_at_the_prefix(lab: Lab) -> None:
    port = lab.start(document(NEEDS_USER), MOUNT="1")
    code, out = lab.check(lab.file(port, path="/api/chat"))
    assert code == 0, out
    # The document lists /chat; the prefix it was found under is taken off the route.
    assert 'says POST /chat requires "user"' in template_line(out), out
    asked = [r["path"] for r in lab.requests() if r["method"] == "GET"]
    assert asked == ["/openapi.json", "/api/openapi.json"]  # the origin first, then the prefix


def test_the_openapi_get_sends_the_files_headers_and_no_body(lab: Lab) -> None:
    schema = {"type": "object", "required": ["message"], "properties": {"message": {}}}
    port = lab.start(document(schema), guard=True)
    code, out = lab.check(lab.file(port))
    # The service demands the bearer token on every request, this one included: it answers
    # only because the file's header, expanded from the environment, was sent.
    assert code == 0 and template_line(out) == "fits POST /chat in /openapi.json", out
    (got,) = lab.requests()
    assert got["method"] == "GET" and got["path"] == "/openapi.json"
    assert got["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert got["body"] == "" and got["headers"].get("content-length", "0") == "0"
    assert "content-type" not in got["headers"]


def test_a_ref_outside_components_is_not_followed(lab: Lab) -> None:
    inside = {"Ask": NEEDS_USER}
    ref = {"$ref": "#/components/schemas/Ask"}
    code, out = lab.check(lab.file(lab.start(document(ref, inside))))
    assert 'requires "user"' in template_line(out), out  # a $ref inside components is followed
    outside = document({"$ref": "#/x-elsewhere/Ask"})
    outside["x-elsewhere"] = {"Ask": NEEDS_USER}
    code, out = lab.check(lab.file(lab.start(outside)), "--strict")
    assert code == 0, out
    assert "requires" not in out
    assert "a $ref outside components/schemas" in out  # said once, as not checked


def test_the_openapi_get_reaches_the_app_untouched(lab: Lab) -> None:
    port = lab.start(document(NEEDS_USER))
    code, out = lab.bench.test(port)
    assert code == 0, out
    seen = lab.requests()
    gets = [r for r in seen if r["method"] == "GET"]
    assert [g["path"] for g in gets] == ["/openapi.json"]
    assert not [h for h in gets[0]["headers"] if h.startswith("x-scrimmage-")]
    # The runs, on the other hand, came through the gate, which removed its headers too.
    assert [r["method"] for r in seen].count("POST") == 2


# --- a document the parser cannot hold -----------------------------------------------------


class Nested(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"[" * 200_000
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_a_deeply_nested_document_is_a_note_not_a_crash(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), Nested)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = tmp_path / "scrimmage.toml"
        path.write_text(
            f'[agent]\nservice = "http://127.0.0.1:{server.server_address[1]}/chat"\n'
            'body = {message = "{question}"}\nanswer = "reply"\n\n'
            '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n'
        )
        found = service_client.template_check(config.load(path).agent)
        assert found.warnings == ()
        assert found.notes == (
            "[agent] body: /openapi.json: GET /openapi.json did not answer a JSON document; "
            "not checked",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_paths_the_document_is_looked_for_at() -> None:
    paths: Callable[[str], list[str]] = service_client.openapi_paths
    assert paths("/chat") == ["/openapi.json"]
    assert paths("/api/chat") == ["/openapi.json", "/api/openapi.json"]
    assert paths("/a/b/c/chat") == ["/openapi.json", "/a/b/c/openapi.json", "/a/b/openapi.json"]


# --- what leaves the runner, and how long it may take -----------------------------------------


class Raw(BaseHTTPRequestHandler):
    """Records the headers and body of each GET it gets; answers 404."""

    seen: ClassVar[list[tuple[dict[str, str], bytes]]] = []

    def do_GET(self) -> None:
        size = int(self.headers.get("content-length") or 0)
        Raw.seen.append(({k.lower(): v for k, v in self.headers.items()}, self.rfile.read(size)))
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class Drip(BaseHTTPRequestHandler):
    """Sends its reply one byte every 0.2 s, for 60 s: no single read ever times out."""

    def do_GET(self) -> None:
        try:
            if self.path == "/headers":  # the headers themselves are dripped
                for byte in b"HTTP/1.1 200 OK\r\ncontent-length: 9999\r\nx-a: " + b"y" * 300:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.2)
            else:
                self.wfile.write(b"HTTP/1.1 200 OK\r\ncontent-length: 9999\r\n\r\n")
                for _ in range(300):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.2)
        except OSError:
            pass  # the runner cut the connection

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def served(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def agent_for(tmp_path: Path, port: int, monkeypatch: pytest.MonkeyPatch) -> config.Agent:
    monkeypatch.setenv("FILE_TOKEN", TOKEN)
    path = tmp_path / "scrimmage.toml"
    path.write_text(
        f'[agent]\nservice = "http://127.0.0.1:{port}/chat"\n'
        'body = {message = "{question}"}\nanswer = "reply"\n'
        'headers = {Authorization = "Bearer ${FILE_TOKEN}"}\n\n'
        '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n'
    )
    return config.load(path).agent


def test_the_openapi_get_leaves_the_runner_with_the_files_headers_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Raw.seen.clear()
    server, port = served(Raw)
    try:
        service_client.template_check(agent_for(tmp_path, port, monkeypatch))
    finally:
        server.shutdown()
        server.server_close()
    ((headers, body),) = Raw.seen
    assert not [name for name in headers if name.startswith("x-scrimmage-")], headers
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert set(headers) == {"host", "accept-encoding", "authorization"}, headers
    assert body == b""


@pytest.mark.parametrize("kind", ["headers", "body"])
def test_a_service_that_drips_its_reply_is_cut_at_the_whole_request_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(service_client, "OPENAPI_TIMEOUT_S", 1.5)
    server, port = served(Drip)
    try:
        agent = agent_for(tmp_path, port, monkeypatch)
        monkeypatch.setattr(service_client, "openapi_paths", lambda route: [f"/{kind}"])
        began = time.monotonic()
        found = service_client.template_check(agent)
        elapsed = time.monotonic() - began
    finally:
        server.shutdown()
        server.server_close()
    assert elapsed < 4, elapsed
    assert found.warnings == ()
    assert found.notes == (
        f"[agent] body: /openapi.json: GET /{kind} got no complete answer in 1.5 s; not checked",
    )


def test_the_connect_runs_inside_the_whole_request_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 10.255.255.1 is not routable: a connect to it hangs until its timeout.
    monkeypatch.setattr(service_client, "target", lambda agent: ("10.255.255.1", 80, "/chat"))
    agent = agent_for(tmp_path, 1, monkeypatch)
    began = time.monotonic()
    reply = service_client.send("GET", agent, "/x", {}, None, 6.0, None, None, began + 0.5)
    elapsed = time.monotonic() - began
    if not reply.timed_out:
        pytest.skip(f"this network answers the address at once: {reply.failed}")
    assert elapsed < 2.0, elapsed
    assert not reply.connected


def test_a_deadline_already_passed_makes_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = agent_for(tmp_path, free_port(), monkeypatch)  # nothing listens: a connect is refused
    reply = service_client.send(
        "GET", agent, "/body", {}, None, 6.0, None, None, time.monotonic() - 1
    )
    assert reply.timed_out
    assert not reply.connected


def test_a_stop_ends_the_openapi_read_between_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, port = served(Drip)
    try:
        agent = agent_for(tmp_path, port, monkeypatch)
        monkeypatch.setattr(service_client, "openapi_paths", lambda route: ["/body"])
        began = time.monotonic()
        found = service_client.template_check(agent, stop=lambda: True)
        elapsed = time.monotonic() - began
    finally:
        server.shutdown()
        server.server_close()
    assert elapsed < 4, elapsed
    assert "got stopped" in found.notes[0]


@pytest.fixture
def empty_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """TMPDIR is an empty folder, so anything a command leaves behind shows."""
    folder = tmp_path / "scratch"
    folder.mkdir()
    monkeypatch.setenv("TMPDIR", str(folder))
    monkeypatch.setattr("tempfile.tempdir", str(folder))
    return folder


def test_check_service_leaves_nothing_in_the_temp_folder(lab: Lab, empty_tmp: Path) -> None:
    port = lab.start()
    code, out = lab.check(lab.file(port))
    assert code == 0, out
    assert list(empty_tmp.iterdir()) == []


@pytest.mark.parametrize("unset", ["SCRIMMAGE_TOKEN_FILE", "SCRIMMAGE_CONTROL_URL", "held"])
def test_check_service_that_stops_before_it_probes_leaves_nothing_behind(
    lab: Lab, empty_tmp: Path, monkeypatch: pytest.MonkeyPatch, unset: str
) -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    if unset == "held":  # the control address is taken by another process
        monkeypatch.setenv("SCRIMMAGE_CONTROL_URL", f"http://127.0.0.1:{holder.getsockname()[1]}")
    else:
        monkeypatch.delenv(unset)
    try:
        code, out = lab.check(lab.file(free_port()))
    finally:
        holder.close()
    assert code in (2, 3), out
    assert list(empty_tmp.iterdir()) == []
