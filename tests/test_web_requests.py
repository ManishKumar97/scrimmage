"""Web requests inside a tool, through the real command and in one process."""

import asyncio
import json
import re
import ssl
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import httpx2
import jsonschema
import pytest

from conftest import Project, Result
from faultline_sdk import config, http_gate, labels, plan, state
from faultline_sdk.config import Fault, Mock
from faultline_sdk.errors import ConfigError, FaultlineRefused
from faultline_sdk.plan import Plan

HOST = "inventory.acme.internal"
URL = f"https://{HOST}/stock/SKU-42"
LABEL = f"http:{HOST}"


def toml(function: str, body: str, extra: str = "") -> str:
    return (
        f'[agent]\nfunction = "{function}"\nruns = 1\nmax_cost_usd = "none"\n{extra}\n'
        '[[test]]\nname = "stock"\nquestion = "How many?"\nexpect = ["12"]\n' + body
    )


MOCK = f'[[test.mock]]\ncall = "{LABEL}"\nmethod = "GET"\npath = "/stock/SKU-42"\n'
JSON_MOCK = MOCK + "json = {sku = 'SKU-42', count = 12}\n"
OUTAGE = f'[[test.fault]]\ncall = "{LABEL}"\ndo = "status 503"\n'
TIMEOUT = f'[[test.fault]]\ncall = "{LABEL}"\ndo = "timeout"\n'


def run(project: Project, function: str, body: str, extra: str = "") -> Result:
    project.toml.write_text(toml(function, body, extra))
    return project.test()


def fits_the_schema(result: Result) -> None:
    schema = (Path(plan.__file__).parent / "schemas" / "record.v1.json").read_text()
    validator = jsonschema.Draft202012Validator(json.loads(schema))
    for lines in result.records():
        for line in lines:
            validator.validate(line)


def row(result: Result, name: str) -> list[dict[str, Any]]:
    return next(lines for lines in result.records() if lines[-1]["row"] == name)  # type: ignore[misc]


def test_every_notice_the_product_writes_fits_the_schema() -> None:
    schema = json.loads((Path(plan.__file__).parent / "schemas" / "record.v1.json").read_text())
    allowed = set(schema["$defs"]["notice"]["properties"]["notice"]["enum"])
    source = "".join(p.read_text() for p in Path(plan.__file__).parent.glob("*.py"))
    written = set(re.findall(r'notice\(\s*"(\w+)"', source))
    assert written and written <= allowed, sorted(written - allowed)


# The use case, through the real command.


@pytest.mark.design("web.use_case")
def test_a_mocked_reply_a_503_and_a_timeout_reach_the_tools_own_handling(project: Project) -> None:
    result = run(project, "acme.web:answer", JSON_MOCK + OUTAGE + TIMEOUT)
    assert result.code == 0, result.out + result.err
    assert row(result, "baseline")[-1]["summary"]["answer"] == "12 in stock"
    assert row(result, "baseline")[0]["action"] == "mock"
    outage = row(result, "fault 1")
    assert outage[-1]["summary"]["answer"] == "inventory unavailable: HTTPStatusError"
    assert (outage[0]["call"], outage[0]["action"]) == (LABEL, "fault")
    slow = row(result, "fault 2")[-1]["summary"]
    assert slow["answer"] == "inventory unavailable: ReadTimeout"
    fits_the_schema(result)


@pytest.mark.design("web.post_refused_e2e")
def test_a_post_to_a_payments_host_is_refused(project: Project) -> None:
    result = run(project, "acme.web:answer_refund", "")
    assert result.code == 2
    assert [s["outcome"] for s in result.summaries()] == ["refused call"]
    line = result.records()[0][0]
    assert line["call"] == "http:payments.acme.internal" and line["reason"] == "web_write"
    assert 'add "http:payments.acme.internal POST /refunds" to real' in result.out
    fits_the_schema(result)


@pytest.mark.design("web.two_levels_mixed")
def test_a_tool_mock_and_a_web_fault_on_the_same_tool_is_a_test_mistake(project: Project) -> None:
    tools = 'tools = ["acme.web:check_inventory"]'
    body = '[[test.mock]]\ncall = "tool:check_inventory"\nreturns = "12 in stock"\n' + OUTAGE
    result = run(project, "acme.web:answer", body, tools)
    assert result.code == 2
    assert "the fault never fired" in result.out
    assert "mock on tool:check_inventory replaces the whole tool" in result.out


@pytest.mark.design("web.near_mock_warning")
def test_a_real_request_to_a_mocked_host_is_a_warning(
    project: Project, web: "Web", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVENTORY_URL", web.url(""))
    body = '[[test.mock]]\ncall = "http:127.0.0.1"\npath = "/stock/SKU-42"\ntext = "12"\n'
    result = run(project, "acme.web:answer_two_paths", body)
    assert result.code == 0, result.out
    assert "warning: http:127.0.0.1 GET /stock/SKU-99 ran for real" in result.out
    fits_the_schema(result)


@pytest.mark.design("web.unused_narrow_mock")
def test_a_web_mock_nothing_used_is_a_test_mistake(project: Project) -> None:
    other = f'[[test.mock]]\ncall = "{LABEL}"\npath = "/never"\ntext = "x"\n'
    result = run(project, "acme.web:answer", JSON_MOCK + other)
    assert result.code == 2, result.out
    assert f"no request matched the mock on {LABEL}" in result.out
    fits_the_schema(result)


def test_a_misspelled_host_in_a_web_mock_is_a_test_mistake(project: Project) -> None:
    typo = '[[test.mock]]\ncall = "http:inventory.acme.intenral"\ntext = "x"\n'
    result = run(project, "acme.web:answer", JSON_MOCK + typo)
    assert result.code == 2
    assert "no request matched the mock on http:inventory.acme.intenral" in result.out


def test_a_web_mock_shadowed_by_an_earlier_one_is_a_test_mistake(project: Project) -> None:
    second = f'[[test.mock]]\ncall = "{LABEL}"\ntext = "x"\n'
    result = run(project, "acme.web:answer", JSON_MOCK + second)
    assert result.code == 2
    assert "an earlier mock answered it first" in result.out


@pytest.mark.design("web.record_clean")
def test_no_header_cookie_or_query_value_reaches_the_record(project: Project) -> None:
    result = run(project, "acme.web:answer_report", JSON_MOCK)
    assert result.code == 0, result.out + result.err
    found = list((project.root / "faultline-runs").rglob("*"))
    text = "".join(f.read_text(errors="replace") for f in found if f.is_file())
    assert "query-secret-value" not in text
    assert "header-secret-value" not in text
    assert "cookie-secret-value" not in text
    assert result.records()[0][0]["path"] == "/stock/SKU-42"


def recorded(project: Project) -> str:
    found = (project.root / "faultline-runs").rglob("*")
    return "".join(f.read_text(errors="replace") for f in found if f.is_file())


@pytest.mark.design("web.error_text_clean")
def test_a_query_in_an_error_reaches_no_record_through_the_answer(project: Project) -> None:
    result = run(project, "acme.web:answer_with_its_error", OUTAGE)
    assert result.code == 0, result.out + result.err
    assert "/stock/SKU-42" in row(result, "fault 1")[-1]["summary"]["answer"]
    assert "query-secret-value" not in recorded(project)


@pytest.mark.design("web.error_text_clean")
def test_a_query_in_an_error_reaches_no_record_through_a_tool_line(project: Project) -> None:
    tools = 'tools = ["acme.web:strict_lookup"]\nreal = ["tool:strict_lookup"]'
    result = run(project, "acme.web:answer_through_a_strict_tool", JSON_MOCK + OUTAGE, tools)
    assert result.code == 0, result.out + result.err
    tool_line = next(x for x in row(result, "fault 1") if x.get("kind") == "tool")
    assert tool_line["error"].startswith("HTTPStatusError") and "/stock" in tool_line["error"]
    assert "query-secret-value" not in recorded(project)


@pytest.mark.design("web.error_text_clean")
def test_a_bad_header_reaches_no_record(
    project: Project, web: "Web", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVENTORY_URL", web.url(""))
    result = run(project, "acme.web:answer_with_a_bad_header", "")
    assert result.code == 0, result.out + result.err
    assert result.records()[0][0]["error"] == "LocalProtocolError"
    assert "header-secret-value" not in recorded(project)


# The gate in one process, on both libraries, against a real local server.


class Web:
    """A small real server that counts requests and answers by path."""

    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        web = self
        self.hits: list[tuple[str, str]] = []
        self.release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                web.hits.append((self.command, self.path))
                path = self.path.split("?")[0]
                if path == "/hang":
                    web.release.wait(30)
                    return
                if path.startswith("/to/"):
                    self.send_response(307)
                    self.send_header("Location", web.url("/" + path.removeprefix("/to/")))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 503 if path == "/down" else 200
                body = b"Service Unavailable" if status == 503 else b'{"count": 12}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "session=reply-secret-value")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_HEAD = do_PUT = answer

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.scheme = "http"
        if tls is not None:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
            self.scheme = "https"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"{self.scheme}://127.0.0.1:{self.server.server_address[1]}{path}"

    def stop(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def web() -> Iterator[Web]:
    server = Web()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    folder = tmp_path_factory.mktemp("tls")
    key, cert = folder / "key.pem", folder / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return key, cert


@pytest.fixture
def tls_web(certificate: tuple[Path, Path]) -> Iterator[tuple[Web, ssl.SSLContext]]:
    key, cert = certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = Web(context)
    yield server, ssl.create_default_context(cafile=str(cert))
    server.stop()


class Sink:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.blobs: dict[str, bytes] = {}

    def put(self, line: dict[str, Any], *, keep: bool = False) -> None:
        self.lines.append(line)

    def put_blob(self, sha: str, data: bytes) -> bool:
        self.blobs[sha] = data
        return True

    def put_settle(self, reservation: int, usd: float) -> None:
        del reservation, usd


SENDS = {
    module: (module.Client._send_single_request, module.AsyncClient._send_single_request)
    for module in (httpx, httpx2)
}


def unpatch(module: Any) -> None:
    module.Client._send_single_request, module.AsyncClient._send_single_request = SENDS[module]


@pytest.fixture(params=[httpx, httpx2], ids=["httpx", "httpx2"])
def lib(request: pytest.FixtureRequest) -> Iterator[Any]:
    module: ModuleType = request.param
    yield module
    unpatch(module)
    state.install(None)


def gate(
    module: Any,
    mocks: tuple[Mock, ...] = (),
    faults: tuple[Fault, ...] = (),
    real: tuple[str, ...] = (),
) -> Sink:
    unpatch(module)
    sink = Sink()
    run_state = state.RunState(Plan("r", "t", "row", mocks, faults, (), real, 60.0), sink, [])
    state.install(run_state)
    ctx = http_gate.Context(run_state, None, sink, lambda message: None)  # type: ignore[arg-type]
    http_gate.patch(module, ctx, module.__name__)
    return sink


def web_mock(**kw: Any) -> Mock:
    return Mock(LABEL, None, None, **kw)


def on_local(do: str, when: str = "always") -> Fault:
    return Fault("http:127.0.0.1", do, when)


def outcome(call: Any) -> tuple[Any, ...]:
    """What the agent's code sees: the error's type, and its status if it has one."""
    try:
        reply = call()
    except Exception as exc:
        response = getattr(exc, "response", None)
        return type(exc), None if response is None else response.status_code
    return None, reply.status_code


def raise_for_status(response: Any) -> None:
    response.raise_for_status()


async def async_raise_for_status(response: Any) -> None:
    response.raise_for_status()


@pytest.mark.design("web.real_against_injected_503")
def test_a_503_over_https_with_a_response_hook_is_the_same_real_or_injected(
    lib: Any, tls_web: tuple[Web, ssl.SSLContext]
) -> None:
    server, trust = tls_web
    hooks = {"response": [raise_for_status]}
    with lib.Client(verify=trust, event_hooks=hooks) as client:
        real = outcome(lambda: client.get(server.url("/down")))
    gate(lib, faults=(on_local("status 503"),))
    with lib.Client(verify=trust, event_hooks=hooks) as client:
        injected = outcome(lambda: client.get(server.url("/fine")))
    assert real == injected == (lib.HTTPStatusError, 503)
    assert [hit for hit in server.hits if hit[1] == "/fine"] == []


def test_an_injected_503_with_an_async_hook_matches_a_real_one(
    lib: Any, tls_web: tuple[Web, ssl.SSLContext]
) -> None:
    server, trust = tls_web
    hooks = {"response": [async_raise_for_status]}

    async def get(path: str) -> Any:
        async with lib.AsyncClient(verify=trust, event_hooks=hooks) as client:
            return await client.get(server.url(path))

    real = outcome(lambda: asyncio.run(get("/down")))
    gate(lib, faults=(on_local("status 503"),))
    injected = outcome(lambda: asyncio.run(get("/fine")))
    assert real == injected == (lib.HTTPStatusError, 503)


@pytest.mark.design("web.real_against_injected_timeout")
def test_a_read_timeout_over_https_is_the_same_real_or_injected(
    lib: Any, tls_web: tuple[Web, ssl.SSLContext]
) -> None:
    server, trust = tls_web
    with lib.Client(verify=trust, timeout=0.2) as client:
        real = outcome(lambda: client.get(server.url("/hang")))
    sink = gate(lib, faults=(on_local("timeout"),))
    with lib.Client(verify=trust) as client:
        injected = outcome(lambda: client.get(server.url("/hang")))
    assert real == injected == (lib.ReadTimeout, None)
    assert sink.lines[0]["error"].startswith("ReadTimeout")


def test_an_injected_status_fails_like_a_real_one_at_a_retry_loop(lib: Any, web: Web) -> None:
    """A team's own retry loop sees the fault end and the real reply come through."""
    gate(lib, faults=(on_local("status 503", "first 2"),))
    with lib.Client() as client:
        statuses = [client.get(web.url("/fine")).status_code for _ in range(3)]
    assert statuses == [503, 503, 200]
    assert len(web.hits) == 1


@pytest.mark.design("web.mock_reply_shape")
def test_a_json_mock_is_a_real_reply_and_nothing_is_sent(lib: Any) -> None:
    sink = gate(lib, mocks=(web_mock(json={"count": 12}),))
    with lib.Client() as client:  # the host does not exist, so sending would fail
        reply = client.get(URL)
    assert reply.status_code == 200 and reply.json() == {"count": 12}
    assert reply.headers["content-type"] == "application/json"
    assert reply.headers["content-length"] == str(len(reply.content))
    assert sink.lines[0]["action"] == "mock" and sink.lines[0]["status"] == 200


def test_a_text_mock_an_empty_mock_and_its_headers(lib: Any) -> None:
    text = web_mock(text="héllo", status=201, headers=(("x-trace", "1"),), path="/text")
    empty = web_mock(status=204, path="/empty")
    gate(lib, mocks=(text, empty))
    with lib.Client() as client:
        one, two = (
            client.post(URL.replace("/stock/SKU-42", "/text")),
            client.get(URL.replace("/stock/SKU-42", "/empty")),
        )
    assert (one.status_code, one.text, one.headers["x-trace"]) == (201, "héllo", "1")
    assert one.headers["content-type"].startswith("text/plain")
    assert one.headers["content-length"] == str(len("héllo".encode()))
    assert (two.status_code, two.content, two.headers.get("content-type")) == (204, b"", None)


@pytest.mark.design("web.mock_header_wins")
def test_a_mock_header_wins_whatever_its_case(lib: Any) -> None:
    gate(lib, mocks=(web_mock(json={"a": 1}, headers=(("Content-Type", "text/csv"),)),))
    with lib.Client() as client:
        reply = client.get(URL)
    assert reply.headers.get_list("content-type") == ["text/csv"]


def test_a_status_with_no_standard_name_still_has_a_body(lib: Any) -> None:
    gate(lib, faults=(Fault(LABEL, "status 599", "always"),))
    with lib.Client() as client:
        assert client.get(URL).text == "Error 599"


@pytest.mark.design("web.no_budget_on_web")
def test_a_web_request_reserves_nothing_from_the_budget(lib: Any, web: Web) -> None:
    class Nothing:
        def reserve(self, run: str, usd: float) -> int | None:
            raise AssertionError(f"reserved {usd} for {run}")

    unpatch(lib)
    sink = Sink()
    limits = Plan("r", "t", "row", (), (), (), (), 60.0, budget_usd=1.0)
    run_state = state.RunState(limits, sink, [])
    state.install(run_state)
    ctx = http_gate.Context(run_state, Nothing(), sink, lambda message: None)  # type: ignore[arg-type]
    http_gate.patch(lib, ctx, lib.__name__)
    with lib.Client() as client:
        assert client.get(web.url("/fine")).status_code == 200


def test_a_mock_works_on_an_async_client_and_through_a_stream(lib: Any) -> None:
    gate(lib, mocks=(web_mock(text="fixed"),))

    async def go() -> tuple[str, bytes]:
        async with lib.AsyncClient() as client:
            first = (await client.get(URL)).text
            async with client.stream("GET", URL) as reply:
                return first, b"".join([chunk async for chunk in reply.aiter_bytes()])

    assert asyncio.run(go()) == ("fixed", b"fixed")
    with lib.Client() as client, client.stream("GET", URL) as reply:
        assert b"".join(reply.iter_bytes()) == b"fixed"


def test_an_injected_reply_can_be_read_raw_from_a_stream_like_a_real_one(lib: Any) -> None:
    gate(
        lib,
        mocks=(web_mock(text="fixed", path="/a"),),
        faults=(Fault(LABEL, "status 503", "always"),),
    )
    with lib.Client() as client, client.stream("GET", f"https://{HOST}/b") as reply:
        assert b"".join(reply.iter_raw()) == b"Service Unavailable"
    gate(lib, mocks=(web_mock(text="fixed"),))
    with lib.Client() as client, client.stream("GET", URL) as reply:
        assert reply.headers["content-length"] == "5"
        assert b"".join(reply.iter_raw()) == b"fixed"


def test_a_mocked_cookie_reaches_the_clients_cookie_jar(lib: Any) -> None:
    gate(lib, mocks=(web_mock(headers=(("set-cookie", "a=1; Path=/"),)),))
    with lib.Client() as client:
        client.get(URL)
        assert client.cookies.get("a") == "1"


@pytest.mark.design("web.narrowing")
def test_a_mock_and_a_fault_match_only_their_method_and_path(lib: Any, web: Web) -> None:
    host = "http:127.0.0.1"
    fault = Fault(host, "status 500", "first 1", None, "GET", "/one")
    mock = Mock(host, None, None, "GET", "/two", text="mocked")
    sink = gate(lib, (mock,), (fault,))
    with lib.Client() as client:
        assert client.get(web.url("/three")).status_code == 200  # counts for neither
        assert client.get(web.url("/two?page=3")).text == "mocked"  # no query in a path
        assert client.get(web.url("/one")).status_code == 500  # its first matching call
        assert client.get(web.url("/one")).status_code == 200
    assert [line["action"] for line in sink.lines] == ["real", "mock", "fault", "real"]


@pytest.mark.design("web.get_rule")
def test_get_and_head_run_for_real(lib: Any, web: Web) -> None:
    sink = gate(lib)
    with lib.Client() as client:
        assert client.get(web.url("/fine")).json() == {"count": 12}
        assert client.head(web.url("/fine")).status_code == 200
    assert [hit[0] for hit in web.hits] == ["GET", "HEAD"]
    assert [line["action"] for line in sink.lines] == ["real", "real"]
    assert sink.lines[0]["status"] == 200


@pytest.mark.design("web.writes_refused")
@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE", "PURGE", "post", "Options"])
def test_every_method_but_get_and_head_is_refused(lib: Any, web: Web, verb: str) -> None:
    sink = gate(lib)
    with lib.Client() as client, pytest.raises(FaultlineRefused, match="neither mocks it"):
        client.request(verb, web.url("/fine"), content=b"x")
    assert web.hits == []
    assert sink.lines[0]["action"] == "refused" and sink.lines[0]["reason"] == "web_write"


def test_a_write_is_refused_on_an_async_client(lib: Any, web: Web) -> None:
    gate(lib)

    async def go() -> None:
        async with lib.AsyncClient() as client:
            await client.post(web.url("/fine"))

    with pytest.raises(FaultlineRefused):
        asyncio.run(go())
    assert web.hits == []


@pytest.mark.design("web.real_forms")
@pytest.mark.parametrize(
    ("entry", "allowed"),
    [
        ("http:127.0.0.1", True),
        ("http:127.0.0.1 POST", True),
        ("http:127.0.0.1 POST /fine", True),
        ("http:127.0.0.1 POST /other", False),
        ("http:127.0.0.1 PUT", False),
        ("http:other.host", False),
    ],
)
def test_real_can_list_a_host_a_method_or_one_path(
    lib: Any, web: Web, entry: str, allowed: bool
) -> None:
    gate(lib, real=(entry,))
    with lib.Client() as client:
        if allowed:
            assert client.post(web.url("/fine?x=1")).status_code == 200
        else:
            with pytest.raises(FaultlineRefused):
                client.post(web.url("/fine"))
    assert len(web.hits) == int(allowed)


def test_a_mocked_write_is_answered_and_never_sent(lib: Any, web: Web) -> None:
    gate(lib, mocks=(Mock("http:127.0.0.1", None, None, "POST", text="done"),))
    with lib.Client() as client:
        assert client.post(web.url("/fine")).text == "done"
    assert web.hits == []


def test_a_refusal_poisons_the_run_for_later_reads(lib: Any, web: Web) -> None:
    gate(lib)
    with lib.Client() as client:
        with pytest.raises(FaultlineRefused):
            client.post(web.url("/fine"))
        with pytest.raises(FaultlineRefused, match="already refused"):
            client.get(web.url("/fine"))
    assert web.hits == []


@pytest.mark.design("web.raw_path")
def test_a_path_is_matched_as_it_was_sent(lib: Any) -> None:
    mocks = (Mock(LABEL, None, None, path="/a%20b", text="spaced"),)
    gate(lib, mocks=mocks, real=())
    with lib.Client() as client:
        assert client.get(f"https://{HOST}/a%20b").text == "spaced"
        assert client.get(f"https://{HOST}/a b").text == "spaced"
    gate(lib, mocks=(Mock(LABEL, None, None, path="/a/b", text="x"),))
    transport = lib.MockTransport(lambda request: lib.Response(200, text="real"))
    with lib.Client(transport=transport) as client:
        assert client.get(f"https://{HOST}/a%2Fb").text == "real"


@pytest.mark.design("web.refusal_suggests_a_loadable_entry")
@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/a%20b/c%2Fd?token=x",
        "https://bücher.example/kaufen",
        "http://[::1]:9/x",
    ],
)
def test_a_refusal_suggests_an_entry_that_loads_and_allows_the_request(lib: Any, url: str) -> None:
    gate(lib)
    transport = lib.MockTransport(lambda request: lib.Response(200, text="sent"))
    with lib.Client(transport=transport) as client, pytest.raises(FaultlineRefused) as found:
        client.post(url)
    entry = re.findall(r'add "([^"]+)" to real', str(found.value))[0]
    allowed = labels.check(entry, "real", pattern=True)
    gate(lib, real=(allowed,))
    with lib.Client(transport=transport) as client:
        assert client.post(url).text == "sent"


@pytest.mark.design("web.xn_host")
def test_an_international_host_is_named_by_its_xn_form(lib: Any) -> None:
    gate(lib, mocks=(Mock("http:xn--bcher-kva.example", None, None, text="m"),))
    with lib.Client() as client:
        assert client.get("https://bücher.example/x").text == "m"


@pytest.mark.design("web.host_from_request")
def test_a_request_through_a_proxy_is_labelled_by_its_own_host(lib: Any, web: Web) -> None:
    sink = gate(lib)
    with lib.Client(proxy=web.url("")) as client:
        assert client.get("http://orders.acme.internal/one").status_code == 200
    assert web.hits[0][1].endswith("orders.acme.internal/one")
    assert sink.lines[0]["call"] == "http:orders.acme.internal"
    gate(lib, mocks=(Mock("http:orders.acme.internal", None, None, text="m"),))
    with lib.Client(proxy=web.url("")) as client:
        assert client.get("http://orders.acme.internal/two").text == "m"
    assert len(web.hits) == 1


def test_a_mounted_transport_a_subclass_and_a_built_request_all_meet_the_gate(lib: Any) -> None:
    hits: list[str] = []

    def answer(request: Any) -> Any:
        hits.append(str(request.url))
        return lib.Response(200, text="ok")

    class Own(lib.Client):
        def send(self, request: Any, **kw: Any) -> Any:
            return super().send(request, **kw)

    gate(lib)
    transport = lib.MockTransport(answer)
    target = "https://orders.acme.internal/x"
    with Own(mounts={"all://": transport}) as client, pytest.raises(FaultlineRefused):
        client.post(target)
    gate(lib)
    with lib.Client(transport=transport) as client, pytest.raises(FaultlineRefused):
        client.send(client.build_request("POST", target))
    assert hits == []


def test_credentials_in_a_url_are_not_the_host_and_not_recorded(lib: Any) -> None:
    sink = gate(lib, mocks=(web_mock(text="m"),))
    with lib.Client() as client:
        assert client.get(f"https://user:url-secret@{HOST}/stock").text == "m"
    assert sink.lines[0]["call"] == LABEL
    assert "url-secret" not in json.dumps(sink.lines)


def test_a_host_in_capitals_matches_its_label(lib: Any) -> None:
    gate(lib, mocks=(web_mock(text="m"),))
    with lib.Client() as client:
        assert client.get("https://INVENTORY.Acme.Internal/stock").text == "m"


@pytest.mark.design("web.redirect_hops")
def test_each_hop_of_a_redirect_is_decided_on_its_own(lib: Any, web: Web) -> None:
    sink = gate(lib, real=("http:127.0.0.1 POST /to/fine",))
    # A 307 keeps the POST, and /fine is not listed.
    with lib.Client(follow_redirects=True) as client, pytest.raises(FaultlineRefused):
        client.post(web.url("/to/fine"))
    assert web.hits == [("POST", "/to/fine")]
    assert [line["action"] for line in sink.lines] == ["real", "refused"]


def test_a_redirected_get_stays_a_get_and_is_recorded_per_hop(lib: Any, web: Web) -> None:
    sink = gate(lib)
    with lib.Client(follow_redirects=True) as client:
        assert client.get(web.url("/to/fine")).status_code == 200
    assert [line["path"] for line in sink.lines] == ["/to/fine", "/fine"]


def test_a_real_stream_passes_unchanged_and_is_recorded_when_read(lib: Any, web: Web) -> None:
    sink = gate(lib)
    with lib.Client() as client, client.stream("GET", web.url("/fine")) as reply:
        assert b"".join(reply.iter_bytes()) == b'{"count": 12}'
    assert sink.lines[0]["result"]["preview"] == '{"count": 12}'

    async def go() -> bytes:
        async with lib.AsyncClient() as client, client.stream("GET", web.url("/fine")) as reply:
            return b"".join([chunk async for chunk in reply.aiter_bytes()])

    assert asyncio.run(go()) == b'{"count": 12}'
    assert len(sink.lines) == 2


def test_a_failed_real_request_is_recorded_and_raised_as_it_was(lib: Any) -> None:
    sink = gate(lib)
    with lib.Client() as client, pytest.raises(lib.ConnectError):
        client.get("http://127.0.0.1:9/down")
    assert sink.lines[0]["error"].startswith("ConnectError")
    assert sink.lines[0]["action"] == "real"

    async def go() -> None:
        async with lib.AsyncClient() as client:
            await client.get("http://127.0.0.1:9/down")

    with pytest.raises(lib.ConnectError):
        asyncio.run(go())


@pytest.mark.design("web.secrets_in_body")
def test_secrets_are_removed_from_a_body_and_a_path(lib: Any, web: Web) -> None:
    secret = "sk-proj-" + "a" * 30
    sink = gate(lib, real=("http:127.0.0.1",))
    with lib.Client() as client:
        client.post(web.url(f"/fine/{secret}"), json={"key": secret})
    assert secret not in json.dumps(sink.lines)
    assert all(secret.encode() not in data for data in sink.blobs.values())


def test_a_secret_in_a_refused_path_does_not_reach_the_record(lib: Any, web: Web) -> None:
    secret = "sk-proj-" + "a" * 30
    sink = gate(lib)
    with lib.Client() as client, pytest.raises(FaultlineRefused):
        client.post(web.url(f"/fine/{secret}?token=query-value"))
    assert secret not in json.dumps(sink.lines) and "query-value" not in json.dumps(sink.lines)


def test_a_model_call_through_httpx_is_a_model_call(lib: Any) -> None:
    faults = (Fault("model", "status 429", "always"),)
    sink = gate(lib, faults=faults)
    with lib.Client() as client:
        reply = client.post("https://api.openai.com/v1/chat/completions", json={"model": "m"})
    assert reply.status_code == 429 and sink.lines[0]["call"] == "model:openai"


def test_cancelling_a_real_request_is_recorded_as_it_was(lib: Any, web: Web) -> None:
    sink = gate(lib)

    async def go() -> None:
        async with lib.AsyncClient() as client:
            task = asyncio.create_task(client.get(web.url("/hang")))
            while not web.hits:
                await asyncio.sleep(0.01)
            task.cancel()
            await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert sink.lines[0]["error"].startswith("CancelledError")


@pytest.mark.design("web.error_text_clean")
def test_a_web_line_keeps_only_the_type_of_a_transport_error(lib: Any) -> None:
    def fails(request: Any) -> Any:
        raise lib.ConnectError(f"cannot reach {request.url}")

    sink = gate(lib)
    with lib.Client(transport=lib.MockTransport(fails)) as client, pytest.raises(lib.ConnectError):
        client.get(URL, params={"api_key": "query-value"})
    assert sink.lines[0]["error"] == "ConnectError"


@pytest.mark.design("web.error_text_clean")
def test_a_web_line_keeps_only_the_type_of_an_error_in_a_stream(lib: Any) -> None:
    class Broken(lib.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"half"
            raise lib.ReadError("lost https://h/p?token=query-value")

    def half(request: Any) -> Any:
        return lib.Response(200, stream=Broken())

    sink = gate(lib)
    with lib.Client(transport=lib.MockTransport(half)) as client, pytest.raises(lib.ReadError):
        client.get(URL)
    assert sink.lines[0]["error"] == "ReadError"


@pytest.mark.design("web.no_headers_recorded")
def test_no_header_or_cookie_of_a_real_call_is_recorded(lib: Any, web: Web) -> None:
    sink = gate(lib)
    with lib.Client(cookies={"session": "cookie-value"}) as client:
        client.get(web.url("/fine?token=query-value"), headers={"Authorization": "header-value"})
    text = json.dumps(sink.lines) + repr(sink.blobs)
    for value in ("cookie-value", "query-value", "header-value", "reply-secret-value"):
        assert value not in text


def test_a_library_without_response_drops_mocks_and_keeps_refusing(lib: Any, web: Web) -> None:
    class Bare:
        __version__ = lib.__version__
        Client = lib.Client
        AsyncClient = lib.AsyncClient
        SyncByteStream = lib.SyncByteStream
        AsyncByteStream = lib.AsyncByteStream

    sink = Sink()
    mocks = (
        Mock("http:127.0.0.1", None, None, "GET", text="m"),
        Mock("http:127.0.0.1", None, None, "POST", text="m"),
    )
    run_state = state.RunState(Plan("r", "t", "row", mocks, (), (), (), 60.0), sink, [])
    state.install(run_state)
    ctx = http_gate.Context(run_state, None, sink, lambda m: None)  # type: ignore[arg-type]
    http_gate.patch(Bare, ctx, lib.__name__)  # type: ignore[arg-type]
    with lib.Client() as client:
        assert client.get(web.url("/fine")).json() == {"count": 12}  # a read runs for real
        with pytest.raises(FaultlineRefused):
            client.post(web.url("/fine"))  # a write is never let through
    assert web.hits == [("GET", "/fine")]


# The file.


def load_mock(tmp_path: Path, body: str, agent: str = "") -> config.Config:
    path = tmp_path / "faultline.toml"
    path.write_text(toml("acme.web:answer", body, agent))
    return config.load(path)


@pytest.mark.design("web.config")
def test_a_web_mock_and_fault_are_read(tmp_path: Path) -> None:
    text = (
        f'[[test.mock]]\ncall = "HTTP:{HOST}"\n'.replace("HTTP", "http")
        + 'method = "get"\npath = "/a"\nstatus = 201\njson = {a = 1}\n'
        + 'headers = {"x-a" = "b"}\n'
        + f'[[test.fault]]\ncall = "{LABEL}"\nmethod = "post"\npath = "/a"\ndo = "status 503"\n'
        + 'when = "first 2"\n'
    )
    test = load_mock(tmp_path, text).tests[0]
    assert test.mocks[0] == Mock(LABEL, None, None, "GET", "/a", 201, (("x-a", "b"),), {"a": 1})
    assert test.faults[0] == Fault(LABEL, "status 503", "first 2", None, "POST", "/a")


def test_hosts_are_lowercase_and_real_forms_are_normalized(tmp_path: Path) -> None:
    body = "[[test.mock]]\ncall = 'http:Inventory.Acme.Internal'\ntext = 'x'\n"
    entries = 'real = ["http:A.B", "http:a.b post", "http:a.b post /p"]'
    loaded = load_mock(tmp_path, body, entries)
    assert loaded.tests[0].mocks[0].call == LABEL
    assert loaded.agent.real == ("http:a.b", "http:a.b POST", "http:a.b POST /p")


@pytest.mark.parametrize(
    ("body", "agent", "message"),
    [
        (f"{MOCK}returns = 'x'\n", "", "returns: unknown setting"),
        (f"{MOCK}json = 1\ntext = 'x'\n", "", "not both"),
        (f"{MOCK}status = 1000\n", "", "expected an HTTP status"),
        (f"{MOCK}status = true\n", "", "expected an HTTP status"),
        (f"{MOCK}headers = {{content-length = '3'}}\n", "", "written from the body"),
        (f"{MOCK}headers = {{content-encoding = 'gzip'}}\n", "", "written from the body"),
        (f"{MOCK}headers = {{a = 1}}\n", "", "headers a: expected text"),
        (f"[[test.mock]]\ncall = '{LABEL}'\nmethod = 'G T'\n", "", "not an HTTP method"),
        (f"[[test.mock]]\ncall = '{LABEL}'\npath = 'a'\n", "", "is not a path"),
        (f"[[test.mock]]\ncall = '{LABEL}'\npath = '/a?b=1'\n", "", "no query string"),
        (f"[[test.mock]]\ncall = '{LABEL}'\npath = '/a b'\n", "", "percent-encoded"),
        (f"[[test.mock]]\ncall = '{LABEL}'\npath = '/é'\n", "", "percent-encoded"),
        (f"[[test.mock]]\ncall = '{LABEL} GET /a'\n", "", "in their own settings"),
        ("[[test.mock]]\ncall = 'http:'\n", "", "needs a host"),
        ("[[test.mock]]\ncall = 'http:localhost:8000'\n", "", "has a port"),
        ("[[test.fault]]\ncall = 'http:a.b:8000'\ndo = 'timeout'\n", "", "has a port"),
        ("", 'real = ["http:a.b:8000 POST"]', "has a port"),
        ("[[test.mock]]\ncall = 'http:*'\n", "", "needs a host"),
        (f"[[test.fault]]\ncall = '{LABEL}'\ndo = 'error'\n", "", "'error' works only"),
        (
            f"[[test.fault]]\ncall = '{LABEL}'\ndo = 'timeout'\nraises = 'x'\n",
            "",
            "raises: unknown",
        ),
        (f"[[test.fault]]\ncall = '{LABEL}'\ndo = 'timeout'\nwhen = 'requests 1'\n", "", "first N"),
        ("[[test.fault]]\ncall = 'tool:a'\ndo = 'status 503'\n", "", "web request"),
        (
            "[[test.fault]]\ncall = 'tool:a'\ndo = 'timeout'\nmethod = 'GET'\n",
            "",
            "method: unknown",
        ),
        ("", 'real = ["http:*"]', "needs a host"),
        ("", 'real = ["http:a GET /b?c=1"]', "no query string"),
        ("", 'real = ["http:a G3T"]', "METHOD /path"),
        ("", 'real = ["http:a GET /b c"]', "METHOD /path"),
    ],
)
def test_bad_web_settings_are_rejected(tmp_path: Path, body: str, agent: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_mock(tmp_path, body, agent)


@pytest.mark.design("web.ipv6_host")
@pytest.mark.parametrize("written", ["http:[::1]", "http:::1"])
def test_an_ipv6_host_can_be_named_and_matches_its_request(lib: Any, written: str) -> None:
    label = labels.check(written, "x")
    assert label == "http:::1"
    doc = plan.mock_json(Mock(label, None, None, text="m"))
    schema = json.loads((Path(plan.__file__).parent / "schemas" / "plan.v1.json").read_text())
    jsonschema.Draft202012Validator(schema["$defs"]["web_label"]).validate(doc["call"])
    gate(lib, mocks=(Mock(label, None, None, text="m"),))
    with lib.Client() as client:
        assert client.get("http://[::1]:9/x").text == "m"


@pytest.mark.design("web.plan_schema")
def test_a_plan_with_web_mocks_and_faults_fits_the_schema_and_round_trips(tmp_path: Path) -> None:
    body = (
        JSON_MOCK.replace(
            'path = "/stock/SKU-42"\n', 'path = "/s"\nstatus = 201\nheaders = {a = "b"}\n'
        )
        + f'[[test.fault]]\ncall = "{LABEL}"\nmethod = "GET"\npath = "/s"\ndo = "timeout"\n'
    )
    loaded = load_mock(tmp_path, body, 'real = ["http:a POST /b", "http:[::1] POST /b"]')
    row_ = plan.rows(loaded)[1]
    doc = plan.plan_for(loaded, row_, "r").to_json()
    schema = json.loads((Path(plan.__file__).parent / "schemas" / "plan.v1.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(doc)
    assert Plan.from_json(doc).to_json() == doc
    assert Plan.from_json(doc).mocks[0].json == {"sku": "SKU-42", "count": 12}
