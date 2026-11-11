"""scrimmage record end to end: real runs of a small bot against a local service."""

import contextlib
import difflib
import json
import signal
import threading
import tomllib
import tracemalloc
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

import scrimmage_core
from conftest import Project, TimeLimit
from fakes.openai_server import FakeOpenAI
from scrimmage_core import capture, toml_write
from scrimmage_core import record as recording
from scrimmage_core.cli import main
from scrimmage_core.control import ControlServer
from test_cli_json import validate

pytestmark = pytest.mark.slow

SK = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
Route = tuple[int, dict[str, str], bytes]


class Service:
    """A local web service with one scripted reply per path, that counts what it receives."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.dynamic: dict[str, Callable[[str, bytes], Route]] = {}
        self.requests: list[tuple[str, str, str]] = []  # method, path, query
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def serve(self) -> None:
                path, _, query = self.path.partition("?")
                outer.requests.append((self.command, path, query))
                size = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(size) if size else b""
                maker = outer.dynamic.get(path)
                status, headers, data = (
                    maker(query, body)
                    if maker is not None
                    else outer.routes.get(path, (404, {}, b'{"error": "no route"}'))
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    with contextlib.suppress(OSError):  # a client that stopped reading
                        self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            do_GET = serve
            do_HEAD = serve
            do_POST = serve
            do_PUT = serve

        self.server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.url = f"http://localhost:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def json(self, path: str, value: object, status: int = 200) -> None:
        body = json.dumps(value).encode()
        self.routes[path] = (status, {"content-type": "application/json"}, body)

    def text(self, path: str, value: str, status: int = 200, kind: str = "text/plain") -> None:
        self.routes[path] = (status, {"content-type": kind}, value.encode())

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


BOT_HEAD = """
import asyncio, json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
import httpx

SERVICE = os.environ.get("SERVICE", "")

def http_get(path, **kwargs):
    return httpx.get(SERVICE + path, timeout=30, **kwargs)
"""


@dataclass
class Done:
    code: int
    doc: dict[str, Any]
    out: str
    err: str
    team: "Team"

    @property
    def test(self) -> dict[str, Any]:
        return cast(list[dict[str, Any]], self.doc["tests"])[0]

    def manual(self, code: str | None = None) -> list[dict[str, Any]]:
        return [m for m in self.test["manual"] if code is None or m["code"] == code]

    def skipped(self, code: str | None = None) -> list[dict[str, Any]]:
        return [s for s in self.test["skipped"] if code is None or s["code"] == code]

    def written_calls(self) -> list[str]:
        return [str(w["call"]) for w in self.test["written"]]

    @property
    def output(self) -> dict[str, Any]:
        return tomllib.loads(self.team.out_path.read_text())

    def mocks(self, test: int = 0) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.output["test"][test].get("mock", []))


@dataclass
class Team:
    project: Project
    service: Service
    env: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def root(self) -> Path:
        return self.project.root

    @property
    def toml(self) -> Path:
        return self.project.toml

    @property
    def out_path(self) -> Path:
        return self.root / "scrimmage.recorded.toml"

    def write(
        self,
        code: str,
        tools: list[str] | None = None,
        real: tuple[str, ...] = (),
        tests: str | None = None,
        agent_extra: str = "",
    ) -> None:
        (self.root / "bot.py").write_text(BOT_HEAD + code)
        tool_lines = ", ".join(f'"{t}"' for t in tools or [])
        real_lines = ", ".join(f'"{r}"' for r in real)
        body = tests if tests is not None else ONE_TEST
        # Most bots here make no model call, which with a budget on is a mistake of its own.
        budget = "" if "max_cost_usd" in agent_extra else 'max_cost_usd = "none"\n'
        self.toml.write_text(
            f'[agent]\nfunction = "bot:answer"\ntools    = [{tool_lines}]\n'
            f"real     = [{real_lines}]\n{budget}{agent_extra}\n{body}"
        )

    def record(self, *flags: str) -> Done:
        self.project.monkeypatch.setenv("SERVICE", self.service.url)
        self.project.capsys.readouterr()
        code = main(["record", str(self.toml), "--json", *flags])
        captured = self.project.capsys.readouterr()
        doc = json.loads(captured.out)  # standard output is one document
        assert captured.err == "", captured.err
        assert doc["command"] == "record" and doc["exit"] == code
        validate(doc)  # the shipped schema and the tests' closed copy of it
        if doc["out"] is not None:
            text = self.out_path.read_text()
            for entry in (m for found in doc["tests"] for m in found["manual"]):
                assert f'# call    = "{entry["call"]}"' in text, entry  # a skeleton per listing
        else:
            assert doc["numbers"]["calls_written"] == 0
        return Done(code, doc, captured.out, captured.err, self)

    def record_text(self, *flags: str) -> tuple[int, str]:
        self.project.monkeypatch.setenv("SERVICE", self.service.url)
        self.project.capsys.readouterr()
        code = main(["record", str(self.toml), *flags])
        captured = self.project.capsys.readouterr()
        return code, captured.out

    def test_output(self) -> tuple[int, str]:
        """scrimmage test on the file record wrote, with the service's counter reset."""
        self.service.requests.clear()
        self.project.monkeypatch.setenv("SERVICE", self.service.url)
        self.project.capsys.readouterr()
        code = main(["test", str(self.out_path)])
        return code, self.project.capsys.readouterr().out


ONE_TEST = '[[test]]\nname     = "t"\nquestion = "q"\nexpect   = ["ok"]\n'


@pytest.fixture
def team(project: Project) -> Iterator[Team]:
    service = Service()
    yield Team(project, service)
    service.stop()


def lines_of(done: Done) -> list[dict[str, Any]]:
    (found,) = done.team.root.glob("scrimmage-runs/*/*.jsonl")
    return [json.loads(text) for text in found.read_text().splitlines()]


# Runs and the gates.


@pytest.mark.design("record.baseline_once")
@pytest.mark.design("record.team_file_unchanged")
@pytest.mark.design("record.copy_plus_mocks")
@pytest.mark.design("record.keeps_comments")
@pytest.mark.design("record.customer_data_warning")
@pytest.mark.design("json.record_codes")
@pytest.mark.design("schema.record_fields")
def test_one_baseline_run_and_a_copy_of_the_file_with_the_mocks_added(team: Team) -> None:
    team.write(
        """
def get_order(order_id):
    return {"order_id": order_id, "status": "shipped"}

def answer(question):
    get_order("1")
    return "ok"
""",
        ["bot:get_order"],
        ("tool:get_order",),
        tests=(
            '# a comment that stays\n[[test]]\nname     = "t"\nquestion = "q"\nexpect   = ["ok"]\n'
            '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\nwhen = "first 1"\n'
            '[[test.fault]]\ncall = "tool:get_order"\ndo   = "error"\nwhen = "first 1"\n'
            "# a trailing comment that stays\n"
        ),
        agent_extra="runs     = 3\n",
    )
    team_bytes = team.toml.read_bytes()
    team_time = team.toml.stat().st_mtime_ns
    done = team.record()
    assert done.code == 0, done.out
    assert team.toml.read_bytes() == team_bytes and team.toml.stat().st_mtime_ns == team_time
    # one run, the baseline, with no fault fired.
    runs = list(team.root.glob("scrimmage-runs/*/*.jsonl"))
    assert len(runs) == 1
    lines = lines_of(done)
    assert all(line.get("row", "baseline") == "baseline" for line in lines)
    call = next(line for line in lines if "call" in line)
    assert "recorded" in call and call["action"] == "real" and "fault" not in call
    assert done.doc["status"] == "complete" and done.doc["out"] == str(team.out_path)
    assert done.doc["numbers"] == {"calls_seen": 1, "calls_written": 1}
    assert any("customer data" in w["message"] for w in done.doc["warnings"])
    # the output is the team's file plus exactly this mock, its comments kept in order.
    output = tomllib.loads(team.out_path.read_text())
    team_doc = tomllib.loads(team_bytes.decode())
    mocks = output["test"][0].pop("mock")
    assert output == team_doc
    assert mocks == [
        {
            "call": "tool:get_order",
            "recorded": True,
            "args": {"order_id": "1"},
            "returns": {"order_id": "1", "status": "shipped"},
        }
    ]
    new = team.out_path.read_text().splitlines()
    position = -1
    for line in team_bytes.decode().splitlines():
        position = new.index(line, position + 1)
    assert "\n".join(new).count("Written by scrimmage record") == 1
    # the record file's lines fit the schema, and a field of the wrong type does not.
    schema = json.loads(
        (Path(scrimmage_core.__file__).parent / "schemas/record.v1.json").read_text()
    )
    validator = jsonschema.Draft202012Validator(schema)
    bad = dict(call, call_id="one")
    for line in lines:
        validator.validate(line)
    assert not validator.is_valid(bad)


@pytest.mark.design("record.unknown_test")
@pytest.mark.design("record.refresh_refuses_writes")
def test_a_flag_that_names_nothing_stops_before_any_run(team: Team) -> None:
    team.write(
        "def refund(o):\n    return 'x'\n\ndef answer(q):\n    return refund('1')\n",
        ["bot:refund"],
        tests=ONE_TEST + '[[test.mock]]\ncall = "tool:refund"\nreturns = "done"\n',
    )
    unknown = team.record("--test", "nope")
    assert unknown.code == 2 and not (team.root / "scrimmage-runs").exists()
    assert "nope" in unknown.doc["mistakes"][0]["message"]
    assert unknown.doc["out"] is None
    write = team.record("--refresh", "tool:refund")
    assert write.code == 2 and not (team.root / "scrimmage-runs").exists()
    assert "tool:refund" in write.doc["mistakes"][0]["message"]


@pytest.mark.design("record.web_inside_tool")
def test_a_tool_that_calls_a_service_gets_a_web_mock_and_its_code_still_runs(team: Team) -> None:
    team.service.json("/stock/1", {"count": 3})
    team.write(
        """
def stock(sku):
    return http_get(f"/stock/{sku}").json()["count"]

def answer(question):
    return f"ok {stock('1')}"
""",
        ["bot:stock"],
        ("tool:stock",),
    )
    done = team.record()
    assert done.code == 0, done.out
    assert done.written_calls() == ["http:localhost"]
    (mock,) = done.mocks()
    assert mock["call"] == "http:localhost" and mock["path"] == "/stock/1"
    assert mock["json"] == {"count": 3} and mock["status"] == 200 and mock["recorded"] is True
    code, out = team.test_output()
    assert code == 0, out
    assert team.service.requests == []  # the tool ran, its GET mocked; nothing reached the service


@pytest.mark.design("record.async_inside_tool")
@pytest.mark.design("record.gather_keeps_parents")
def test_async_tools_each_keep_their_own_requests(team: Team) -> None:
    team.service.json("/a", {"a": 1})
    team.service.json("/b", {"b": 2})
    team.write(
        """
async def fetch_a(key):
    async with httpx.AsyncClient() as client:
        await asyncio.sleep(0.2)
        return (await client.get(SERVICE + "/a")).json()

async def fetch_b(key):
    async with httpx.AsyncClient() as client:
        reply = await client.get(SERVICE + "/b")
        await asyncio.sleep(0.2)
        return reply.json()

def answer(question):
    async def go():
        return await asyncio.gather(fetch_a("x"), fetch_b("y"))
    asyncio.run(go())
    return "ok"
""",
        ["bot:fetch_a", "bot:fetch_b"],
        ("tool:fetch_a", "tool:fetch_b"),
    )
    done = team.record()
    assert done.code == 0, done.out
    assert sorted(m["path"] for m in done.mocks()) == ["/a", "/b"]
    lines = [x for x in lines_of(done) if "call" in x]
    tools = {x["call"]: x["call_id"] for x in lines if x["kind"] == "tool"}
    parents = {x["path"]: x["parent"] for x in lines if x["kind"] == "http"}
    assert parents == {"/a": tools["tool:fetch_a"], "/b": tools["tool:fetch_b"]}
    code, out = team.test_output()
    assert code == 0 and team.service.requests == [], out


@pytest.mark.design("record.falls_back_to_tool")
@pytest.mark.design("record.web_binary")
def test_a_binary_reply_falls_back_to_a_mock_of_the_tool(team: Team) -> None:
    team.service.routes["/png"] = (200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\n\xff\xfe")
    team.service.routes["/br"] = (200, {"content-encoding": "br"}, b"\x8b\x02\x80compressed")
    team.write(
        """
def picture(name):
    http_get("/png")
    return "a picture"

def pressed(name):
    http_get("/br")
    return "pressed"

def answer(question):
    picture("x")
    pressed("y")
    return "ok"
""",
        ["bot:picture", "bot:pressed"],
        ("tool:picture", "tool:pressed"),
    )
    done = team.record()
    assert done.code == 1, done.out  # a call is listed under manual, as C.J3.19 says
    assert [m["call"] for m in done.mocks()] == ["tool:picture", "tool:pressed"]
    assert {m["path"] for m in done.manual("binary")} == {"/png", "/br"}
    assert done.doc["numbers"] == {"calls_seen": 2, "calls_written": 2}
    text = team.out_path.read_text()
    assert "A mock of tool:picture" in text
    code, out = team.test_output()
    assert code == 0 and team.service.requests == [], out


@pytest.mark.design("record.model_inside")
@pytest.mark.design("record.never_models")
def test_a_tool_that_calls_the_model_is_never_mocked_whole(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    fake = fake_openai(["chat_answer"], embedding="embedding", response="response_text")
    team.service.json("/data", {"x": 1})
    team.write(
        """
import openai

def summarize(text):
    client = openai.OpenAI()
    messages = [{"role": "user", "content": text}]
    client.chat.completions.create(model="gpt-4o-mini", messages=messages)
    return http_get("/data").json()

def answer(question):
    client = openai.OpenAI()
    client.embeddings.create(model="text-embedding-3-small", input=question)
    client.responses.create(model="gpt-4o-mini", input=question)
    summarize("hello")
    return "ok"
""",
        ["bot:summarize"],
        ("tool:summarize",),
        agent_extra='model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n',
    )
    team.project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    team.project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    done = team.record()
    assert done.code == 1, done.out
    assert [m["code"] for m in done.manual()] == ["model_inside"]
    assert done.written_calls() == ["http:localhost"]  # its other calls' mocks
    for mock in done.mocks():
        assert not mock["call"].startswith("model") and mock["call"] != "tool:summarize"
    lines = lines_of(done)
    assert not any("recorded" in line for line in lines if line.get("kind") == "model")
    assert not any(m["call"].startswith("http:127.0.0.1") for m in done.mocks()), (
        "the model host is never written"
    )


@pytest.mark.design("record.outside_tool")
@pytest.mark.design("record.thread_is_outside")
@pytest.mark.design("record.credential_outside")
def test_a_request_outside_any_tool_is_skipped_and_never_changes_the_exit_code(
    team: Team,
) -> None:
    team.service.json("/a", {"a": 1})
    team.service.text("/secret", "shh")
    team.service.routes["/v1/secret/data/db"] = (200, {}, b'{"value": "x"}')
    team.write(
        """
def in_pool(key):
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: http_get("/a").json()).result()

def answer(question):
    in_pool("x")
    http_get("/v1/secret/data/db")
    http_get("/secret")
    return "ok"
""",
        ["bot:in_pool"],
        ("tool:in_pool",),
    )
    done = team.record()
    assert done.code == 0, done.out
    assert [m["call"] for m in done.mocks()] == ["tool:in_pool"]
    assert done.manual() == []
    outside = {s["path"] for s in done.skipped("outside_tool")}
    assert outside == {"/a", "/secret"}
    assert [s["path"] for s in done.skipped("credential")] == ["/v1/secret/data/db"]
    # The credential request does not count. The tool, /a and /secret do; only the tool is written.
    assert done.doc["numbers"] == {"calls_seen": 3, "calls_written": 1}
    code, out = team.test_output()
    assert code == 0, out
    assert ("GET", "/a", "") not in team.service.requests  # the tool's mock answers it


@pytest.mark.design("record.credential_skipped")
@pytest.mark.design("record.vault_path")
def test_a_credential_request_inside_a_tool_is_not_written_and_keeps_its_level_whole(
    team: Team,
) -> None:
    team.service.routes["/v1/secret/data/db"] = (200, {}, b'{"password": "hunter2"}')
    team.service.json("/orders/1", {"id": 1})
    team.write(
        """
def order(order_id):
    token = httpx.post(SERVICE + "/oauth2/token", timeout=30, json={}).text
    http_get("/v1/secret/data/db")
    return http_get(f"/orders/{order_id}").json()

def answer(question):
    order("1")
    return "ok"
""",
        ["bot:order"],
        ("tool:order", "http:localhost POST /oauth2/token"),
    )
    team.service.routes["/oauth2/token"] = (200, {}, b'{"access_token": "live"}')
    done = team.record()
    assert done.code == 0, done.out
    assert done.written_calls() == ["http:localhost"]
    assert [m["path"] for m in done.mocks()] == ["/orders/1"]
    assert {s["path"] for s in done.skipped("credential")} == {
        "/oauth2/token",
        "/v1/secret/data/db",
    }
    assert "hunter2" not in team.out_path.read_text()


@pytest.mark.design("record.web_json")
@pytest.mark.design("record.web_null_as_text")
@pytest.mark.design("record.web_bad_json_as_text")
@pytest.mark.design("record.web_key_rule_first")
@pytest.mark.design("record.web_text_pattern")
@pytest.mark.design("record.no_headers")
@pytest.mark.design("record.head")
def test_the_reply_forms_a_web_mock_can_take(team: Team) -> None:
    service = team.service
    service.routes["/json"] = (
        200,
        {"content-type": "application/json", "set-cookie": "a=b", "x-request-id": "1"},
        json.dumps({"ok": True, "n": 1}).encode(),
    )
    service.routes["/null"] = (
        200,
        {"content-type": "application/json"},
        b'{"v": null}',
    )
    service.routes["/badjson"] = (200, {"content-type": "application/json"}, b"not json{")
    service.routes["/keyrule"] = (
        200,
        {"content-type": "application/json"},
        b'{"password": "hunter2", "v": 1}',
    )
    service.routes["/keyrule_null"] = (
        200,
        {"content-type": "application/json"},
        b'{"password": "hunter2", "v": null}',
    )
    service.routes["/text"] = (200, {"content-type": "text/plain"}, f"key {SK}".encode())
    service.routes["/head"] = (200, {"content-type": "application/json"}, b"{}")
    team.write(
        """
def call_all(name):
    results = {}
    for path in ("/json", "/null", "/badjson", "/keyrule", "/text"):
        reply = http_get(path)
        try:
            results[path] = reply.json()
        except ValueError:
            results[path] = reply.text
    httpx.head(SERVICE + "/head", timeout=30)
    return results

def unkeyable(name):
    return http_get("/keyrule_null").text

def answer(question):
    call_all("x")
    unkeyable("y")
    return "ok"
""",
        ["bot:call_all", "bot:unkeyable"],
        ("tool:call_all", "tool:unkeyable"),
    )
    done = team.record()
    assert done.code == 1, done.out  # /keyrule_null is listed
    mocks = {m["path"]: m for m in done.mocks()}
    assert mocks["/json"]["json"] == {"ok": True, "n": 1} and "headers" not in mocks["/json"]
    assert mocks["/null"]["text"] == '{"v": null}'
    assert mocks["/null"]["headers"] == {"content-type": "application/json"}
    assert mocks["/badjson"]["text"] == "not json{"
    assert mocks["/keyrule"]["json"] == {"password": "[secret removed]", "v": 1}
    assert mocks["/text"]["text"] == "key [secret removed]"
    assert mocks["/head"]["status"] == 200 and "json" not in mocks["/head"]
    assert "text" not in mocks["/head"] and mocks["/head"]["method"] == "HEAD"
    assert [m["path"] for m in done.manual("secret_changed_shape")] == ["/keyrule_null"]
    assert "hunter2" not in team.out_path.read_text() and SK not in team.out_path.read_text()
    written = {w["path"]: w for w in done.test["written"]}
    assert written["/keyrule"]["removed"] == [{"key": "password", "rule": "key_name"}]
    assert written["/text"]["removed"] == [{"key": None, "rule": "pattern"}]
    code, out = team.test_output()
    assert code == 0, out


@pytest.mark.design("record.web_path_secret")
@pytest.mark.design("record.status_rule")
@pytest.mark.design("record.status_auth_and_timeout")
@pytest.mark.design("record.raised")
def test_replies_a_mock_must_not_copy_stay_real(team: Team) -> None:
    for path, status in (
        ("/200", 200),
        ("/404", 404),
        ("/401", 401),
        ("/429", 429),
        ("/503", 503),
        ("/301", 301),
        ("/403", 403),
        ("/407", 407),
        ("/408", 408),
        ("/409", 409),
        ("/422", 422),
    ):
        team.service.json(path, {"status": status}, status=status)
    team.service.json(f"/users/{SK}", {"id": 1})
    team.write(
        f"""
def scan(name):
    out = []
    codes = (200, 404, 401, 429, 503, 301, 403, 407, 408, 409, 422)
    for path in [f"/{{code}}" for code in codes]:
        out.append(httpx.get(SERVICE + path, timeout=30, follow_redirects=False).status_code)
    return out

def with_secret(name):
    return http_get("/users/{SK}").json()

def down(name):
    return httpx.get("http://localhost:1/x", timeout=0.5).json()

def answer(question):
    scan("x")
    with_secret("y")
    try:
        down("z")
    except httpx.ConnectError:
        pass
    return "ok"
""",
        ["bot:scan", "bot:with_secret", "bot:down"],
        ("tool:scan", "tool:with_secret", "tool:down"),
    )
    done = team.record()
    assert done.code == 1, done.out
    written_paths = {w["path"] for w in done.test["written"]}
    assert {"/200", "/404", "/409", "/422"} <= written_paths
    by_code = {m["path"]: m["code"] for m in done.manual()}
    for path in ("/401", "/429", "/503", "/301", "/403", "/407", "/408"):
        assert by_code[path] == "status", path
    assert not any(m["call"] == "tool:scan" for m in done.mocks())
    assert not any(m.get("status") == 503 for m in done.mocks())
    assert any(m["code"] == "secret_changed_shape" and SK not in m["path"] for m in done.manual())
    raised = done.manual("raised")
    assert {m["call"] for m in raised} == {"tool:down", "http:localhost"}
    assert any("ConnectError" in m["message"] for m in raised)


@pytest.mark.design("record.same_reply_once")
@pytest.mark.design("record.args_apart")
@pytest.mark.design("record.nondeterministic")
def test_tool_calls_are_grouped_by_their_arguments(team: Team) -> None:
    team.write(
        """
COUNT = [0]

def get_order(order_id):
    return {"id": order_id, "status": "shipped"}

def clock(name):
    COUNT[0] += 1
    return COUNT[0]

def answer(question):
    get_order("1")
    get_order("1")
    get_order("2")
    clock("a")
    clock("a")
    return "ok"
""",
        ["bot:get_order", "bot:clock"],
        ("tool:get_order", "tool:clock"),
    )
    done = team.record()
    assert done.code == 1, done.out
    mocks = done.mocks()
    assert [m["args"] for m in mocks] == [{"order_id": "1"}, {"order_id": "2"}]
    counts = {w["call"] + str(i): w["calls"] for i, w in enumerate(done.test["written"])}
    assert list(counts.values()) == [2, 1]
    assert [m["call"] for m in done.manual("nondeterministic")] == ["tool:clock"]
    assert all(m["call"] == "tool:get_order" for m in mocks)
    code, out = team.test_output()
    assert code == 0, out  # clock has no mock, and runs for real as it is under real


@pytest.mark.design("record.paths_apart")
@pytest.mark.design("record.query_differs")
@pytest.mark.design("record.body_differs")
def test_web_calls_are_grouped_by_method_and_path_and_told_apart_by_query_and_body(
    team: Team,
) -> None:
    def search(query: str, body: bytes) -> Route:
        del body
        return (200, {"content-type": "application/json"}, json.dumps({"q": query}).encode())

    answers = iter([b'{"n": 1}', b'{"n": 2}'])

    def once(query: str, body: bytes) -> Route:
        del query, body
        return (200, {"content-type": "application/json"}, next(answers))

    def graphql(query: str, body: bytes) -> Route:
        del query
        return (200, {"content-type": "application/json"}, json.dumps({"got": len(body)}).encode())

    team.service.dynamic["/search"] = search
    team.service.dynamic["/once"] = once
    team.service.dynamic["/graphql"] = graphql
    team.service.json("/orders/1", {"id": 1})
    team.service.json("/orders/2", {"id": 2})
    team.write(
        """
def t1(q):
    return http_get("/search?q=a").json()

def t2(q):
    return http_get("/search?q=b").json()

def t3(q):
    return http_get("/once?q=same").json()

def t4(q):
    return http_get("/once?q=same").json()

def g1(q):
    return httpx.post(SERVICE + "/graphql", json={"query": "a"}, timeout=30).json()

def g2(q):
    return httpx.post(SERVICE + "/graphql", json={"query": "bb"}, timeout=30).json()

def p1(q):
    return http_get("/orders/1").json()

def p2(q):
    return http_get("/orders/2").json()

def answer(question):
    for tool in (t1, t2, t3, t4, g1, g2):
        tool("x")
    p1("o")
    p2("o2")
    return "ok"
""",
        [f"bot:{n}" for n in ("t1", "t2", "t3", "t4", "g1", "g2", "p1", "p2")],
        (*(f"tool:{n}" for n in ("t1", "t2", "t3", "t4", "g1", "g2", "p1", "p2")),
         "http:localhost POST /graphql"),
    )  # fmt: skip
    done = team.record()
    assert done.code == 1, done.out
    assert {m["path"] for m in done.mocks() if "path" in m} == {"/orders/1", "/orders/2"}
    by_path = {m["path"]: m["code"] for m in done.manual()}
    assert by_path["/search"] == "differs" and by_path["/graphql"] == "differs"
    assert by_path["/once"] == "nondeterministic"
    for tool in ("tool:t1", "tool:t2", "tool:t3", "tool:t4", "tool:g1", "tool:g2"):
        assert any(m["call"] == tool for m in done.mocks()), tool


@pytest.mark.design("record.team_mock_wins")
@pytest.mark.design("record.refresh")
def test_the_teams_mock_wins_and_refresh_records_a_label_again(team: Team) -> None:
    team.write(
        """
def refund(order_id):
    raise RuntimeError("a real refund must never run")

def get_order(order_id):
    return {"id": order_id, "real": True}

def answer(question):
    refund("1")
    get_order("1")
    return "ok"
""",
        ["bot:refund", "bot:get_order"],
        ("tool:get_order",),
        tests=(
            ONE_TEST + '[[test.mock]]\ncall = "tool:refund"\nreturns = "refunded"\n\n'
            '[[test.mock]]\ncall = "tool:get_order"\nreturns = {id = "old"}\n'
        ),
    )
    first = team.record()
    assert first.code == 0, first.out
    assert first.test["kept"] == [
        {"call": "tool:refund", "mock": 1},
        {"call": "tool:get_order", "mock": 2},
    ]
    assert first.test["written"] == []
    done = team.record("--refresh", "tool:get_order")
    assert done.code == 0, done.out
    assert done.test["kept"] == [{"call": "tool:refund", "mock": 1}]
    (written,) = done.test["written"]
    assert written["call"] == "tool:get_order"
    mocks = done.mocks()
    assert [m["call"] for m in mocks] == ["tool:refund", "tool:get_order"]
    assert mocks[1]["returns"] == {"id": "1", "real": True}
    assert "old" not in team.out_path.read_text()
    assert any("replaced" in n["message"] for n in done.doc["notes"])


@pytest.mark.design("record.schema_line_first")
@pytest.mark.design("record.provenance")
@pytest.mark.design("record.tool_star_warns")
def test_comments_header_and_provenance(team: Team) -> None:
    team.write(
        f"""
def get_order(order_id):
    return {{"id": order_id}}

def with_key(key):
    return {{"seen": True}}

def with_password(password):
    return {{"seen": True}}

def answer(question):
    get_order("1")
    with_key("{SK}")
    with_password("hunter2")
    return "ok"
""",
        ["bot:get_order", "bot:with_key", "bot:with_password"],
        ("tool:*",),
    )
    team.toml.write_text("#:schema ./scrimmage.schema.json\n" + team.toml.read_text())
    code, out = team.record_text()
    assert code == 1, out
    assert "warning: every tool that is not mocked runs for real" in out
    text = team.out_path.read_text()
    lines = text.splitlines()
    assert lines[0] == "#:schema ./scrimmage.schema.json"
    assert lines[1].startswith("# Written by scrimmage record from scrimmage.toml on ")
    assert any(
        line.startswith("# scrimmage record, ") and 'tool:get_order with {"order_id": "1"}' in line
        for line in lines
    )
    assert "run-" in next(line for line in lines if line.startswith("# scrimmage record, "))
    assert SK not in text and "hunter2" not in text
    assert any(
        line.startswith("# tool:with_key with") and "[secret removed]" in line for line in lines
    )
    assert any(
        line.startswith("# tool:with_password with") and "[secret removed]" in line
        for line in lines
    )


@pytest.mark.design("record.answer_note")
@pytest.mark.design("record.fake_marker")
@pytest.mark.design("record.placeholder")
@pytest.mark.design("record.null_value")
@pytest.mark.design("record.skeleton")
def test_what_a_mock_cannot_hold_is_listed_with_a_commented_skeleton(team: Team) -> None:
    fake = "req_scrimmage_injected Error executing tool x [[test.fault]] [secret removed]"
    team.write(
        f"""
def marker(key):
    return "{fake}"

def dollars(key):
    return "cost ${{HOME}}"

def nothing(key):
    return {{"note": None}}

def pic(key):
    return b"\\x89PNG"

def plain(key):
    return "a"

def answer(question):
    marker("m")
    dollars("d")
    nothing("n")
    pic("p")
    plain("x")
    return "no answer word"
""",
        ["bot:marker", "bot:dollars", "bot:nothing", "bot:pic", "bot:plain"],
        ("tool:marker", "tool:dollars", "tool:nothing", "tool:pic", "tool:plain"),
    )
    done = team.record()
    assert done.code == 1, done.out
    manual = {m["call"]: m["code"] for m in done.manual()}
    assert manual == {
        "tool:dollars": "placeholder",
        "tool:nothing": "null",
        "tool:pic": "unsavable",
    }
    mocks = {m["call"]: m for m in done.mocks()}
    assert mocks["tool:marker"]["returns"] == fake
    assert "fault" not in done.output["test"][0]
    assert any("broke a rule" in n["message"] for n in done.doc["notes"])
    assert done.doc["numbers"] == {"calls_seen": 5, "calls_written": 2}
    text = team.out_path.read_text()
    for call in ("tool:dollars", "tool:nothing", "tool:pic"):
        assert f'# call    = "{call}"' in text
    assert "# [[test.mock]]" in text
    code, out = team.test_output()
    assert code == 1, out  # the bot's answer has no "ok", so its one row fails its expect


@pytest.mark.design("record.refused_partway")
def test_a_run_refused_partway_keeps_what_it_recorded(team: Team) -> None:
    team.write(
        """
def read_stock(sku):
    return {"count": 3}

def refund_order(order_id):
    raise RuntimeError("must never run")

def answer(question):
    read_stock("a")
    refund_order("1")
    read_stock("b")
    return "ok"
""",
        ["bot:read_stock", "bot:refund_order"],
        ("tool:read_stock",),
    )
    done = team.record()
    assert done.code == 2, done.out
    assert [m["args"] for m in done.mocks()] == [{"sku": "a"}]
    assert [m["call"] for m in done.manual("refused")] == ["tool:refund_order"]
    assert "Mock it in the test" in done.manual("refused")[0]["message"]
    assert any("calls after it were not seen" in n["message"] for n in done.doc["notes"])


@pytest.mark.design("record.crashed_run")
@pytest.mark.design("record.raised")
def test_a_crashed_run_writes_what_ended_and_exits_1(team: Team) -> None:
    team.write(
        """
def read_stock(sku):
    return {"count": 3}

def boom(x):
    raise ValueError("no")

def answer(question):
    read_stock("a")
    try:
        boom("x")
    except ValueError:
        pass
    raise SystemExit(3)
""",
        ["bot:read_stock", "bot:boom"],
        ("tool:read_stock", "tool:boom"),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert done.test["outcome"] == "crashed"
    assert [m["call"] for m in done.mocks()] == ["tool:read_stock"]
    (raised,) = done.manual("raised")
    assert raised["call"] == "tool:boom" and 'do = "error"' in raised["message"]


@pytest.mark.design("record.not_finished")
def test_a_tool_still_going_at_the_time_limit_is_not_finished(
    team: Team, time_limit: TimeLimit
) -> None:
    marker = team.root / "in-tool"
    time_limit.at(marker.exists)
    team.write(
        f"""
def slow(key):
    open({str(marker)!r}, "w").close()
    import time
    time.sleep(60)
    return "x"

def answer(question):
    return slow("x")
""",
        ["bot:slow"],
        ("tool:slow",),
        agent_extra="run_timeout = 60\n",
    )
    done = team.record()
    assert done.code == 1, done.out
    assert done.test["outcome"] == "timed out"
    assert [m["code"] for m in done.manual()] == ["not_finished"]


@pytest.mark.design("record.incomplete_writes_nothing")
def test_a_run_with_a_dropped_line_writes_no_mock_for_its_test(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    team.write(
        """
def read_stock(sku):
    return {"count": 3}

def answer(question):
    read_stock("a")
    return "ok"
""",
        ["bot:read_stock"],
        ("tool:read_stock",),
    )
    real_close = ControlServer.close_run

    def drop_a_line(self: Any, run: str) -> Any:
        state = real_close(self, run)
        state.dropped = 1
        return state

    monkeypatch.setattr(ControlServer, "close_run", drop_a_line)
    done = team.record()
    assert done.code == 3, done.out
    assert done.doc["out"] is None and not team.out_path.exists()
    assert done.test["outcome"] == "infrastructure error"
    assert done.test["written"] == []
    assert any("nothing was written" in i["message"] for i in done.doc["infra"])


@pytest.mark.design("record.budget_stop")
def test_a_budget_stop_exits_2_and_keeps_the_mocks_of_runs_that_ended(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    fake = fake_openai([])  # refused before it is sent, so the fake gets no request
    team.write(
        """
import openai

def read_stock(sku):
    return {"count": 3}

def answer(question):
    read_stock("a")
    openai.OpenAI().chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "a very long " * 2000}]
    )
    return "ok"
""",
        ["bot:read_stock"],
        ("tool:read_stock",),
        agent_extra=(
            'max_cost_usd = 0.000001\nmodel_hosts = [{host = "127.0.0.1", provider = "openai"}]\n'
        ),
    )
    team.project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    team.project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    done = team.record()
    assert done.code == 2, done.out
    assert done.test["outcome"] == "budget reached" or done.test["outcome"] == "not reserved"
    assert done.written_calls() == ["tool:read_stock"]
    assert done.doc["out"] == str(team.out_path)


@pytest.mark.design("record.huge_reply")
def test_the_size_cap_with_the_runner_opening_nothing_above_it(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    team.service.routes["/big"] = (
        200,
        {"content-type": "application/json"},
        json.dumps({"data": "x" * (70 * 1024)}).encode(),
    )
    team.service.routes["/huge"] = (
        200,
        {"content-type": "application/json"},
        json.dumps({"data": "y" * (12 * 1024 * 1024)}).encode(),
    )
    team.service.routes["/fits"] = (
        200,
        {"content-type": "application/json"},
        json.dumps({"data": "z" * (60 * 1024)}).encode(),
    )
    team.write(
        """
def big(k):
    return http_get("/big").json()["data"][:1]

def huge(k):
    return http_get("/huge").json()["data"][:1]

def fits(k):
    return http_get("/fits").json()["data"][:1]

def answer(question):
    big("x")
    huge("y")
    fits("z")
    return "ok"
""",
        ["bot:big", "bot:huge", "bot:fits"],
        ("tool:big", "tool:huge", "tool:fits"),
    )
    peak: list[int] = []
    real_finish = recording.finish

    def measured(*args: Any, **kwargs: Any) -> Any:
        tracemalloc.start()
        result = real_finish(*args, **kwargs)
        peak.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        return result

    monkeypatch.setattr(recording, "finish", measured)
    done = team.record()
    assert done.code == 1, done.out
    too_large = {m["path"]: m["message"] for m in done.manual("too_large")}
    assert set(too_large) == {"/big", "/huge"}
    assert "bytes" in too_large["/big"]
    assert any(m["path"] == "/fits" for m in done.mocks() if "path" in m)
    assert peak and peak[0] < 2 * 1024 * 1024, peak


@pytest.mark.design("record.interrupted")
def test_a_stop_signal_writes_nothing_and_exits_3(team: Team, signals_kept: None) -> None:
    del signals_kept
    marker = team.root / "agent-started"
    team.write(
        f"""
import time

def slow(key):
    open({str(marker)!r}, "w").close()
    time.sleep(120)

def answer(question):
    slow("x")
    return "ok"
""",
        ["bot:slow"],
        ("tool:slow",),
        agent_extra="run_timeout = 120\n",
    )
    previous = signal.getsignal(signal.SIGTERM)
    fired = threading.Event()

    def later() -> None:
        import os
        import time

        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)
        fired.set()

    threading.Thread(target=later, daemon=True).start()
    done = team.record()
    assert fired.is_set()
    assert done.code == 3 and done.doc["out"] is None, done.out
    assert not team.out_path.exists()
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.design("record.big_diff_warns")
def test_a_big_diff_warns_to_record_in_batches(team: Team) -> None:
    team.write(
        """
def lookup(n):
    return list(range(n))

def answer(question):
    lookup(1100)
    return "ok"
""",
        ["bot:lookup"],
        ("tool:lookup",),
    )
    done = team.record()
    assert done.code == 0, done.out
    warnings = [w["message"] for w in done.doc["warnings"]]
    assert any("too long to review at once" in w for w in warnings), warnings
    text = team.out_path.read_text()
    added = len(text.splitlines()) - len(team.toml.read_text().splitlines())
    assert added > 1000


@pytest.mark.design("record.every_gate")
@pytest.mark.design("record.child_web")
def test_every_gate_writes_a_mock(team: Team, tmp_path: Path) -> None:
    log = tmp_path / "docs.log"
    team.project.monkeypatch.setenv(
        "DOCS_SERVER", str(Path(__file__).parent / "fakes/docs_server.py")
    )
    team.project.monkeypatch.setenv("DOCS_LOG", str(log))
    team.project.monkeypatch.setenv("DOCS_MODE", "ok")
    team.project.monkeypatch.setenv("DOCS_TRANSPORT", "stdio")
    team.service.json("/stock", {"n": 1})
    team.write(
        """
from acme.docs_bot import connected

def plain(key):
    return {"plain": key}

class Box:
    def lookup(self, key):
        return {"box": key}

def with_get(key):
    return http_get("/stock").json()

def spawning(key):
    code = "import httpx, os; httpx.get(os.environ['SERVICE'] + '/stock', timeout=30)"
    subprocess.run([sys.executable, "-c", code], check=True)
    return "child done"

def answer(question):
    plain("x")
    Box().lookup("y")
    with_get("w")
    spawning("v")
    async def go():
        async with connected() as session:
            await session.list_tools()
            await session.call_tool("search", {"q": "refunds"})
    asyncio.run(go())
    return "ok"
""",
        ["bot:plain", "bot:Box.lookup", "bot:with_get", "bot:spawning"],
        (
            "tool:plain",
            "tool:lookup",
            "tool:with_get",
            "tool:spawning",
            "mcp:docs/search",
        ),
    )  # fmt: skip
    done = team.record()
    assert done.code == 0, done.out
    calls = {m["call"] for m in done.mocks()}
    assert calls == {
        "tool:plain",
        "tool:lookup",
        "http:localhost",
        "tool:spawning",
        "mcp:docs/search",
    }
    assert [s["code"] for s in done.skipped("outside_tool")] == ["outside_tool"]
    code, out = team.test_output()
    assert code == 0, out
    assert team.service.requests == [], team.service.requests
    assert log.read_text().count("search") == 1  # the mock answered in the test


@pytest.mark.design("record.layout_refused")
def test_a_layout_record_cannot_extend_writes_nothing_and_prints_the_mocks(team: Team) -> None:
    team.write(
        """
def get_order(order_id):
    return {"id": order_id}

def other(x):
    return "x"

def answer(question):
    get_order("1")
    return "ok"
""",
        ["bot:get_order", "bot:other"],
        ("tool:get_order",),
        tests=(
            '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["ok"]\n'
            'mock = [{call = "tool:other", returns = "x"}]\n'
        ),
    )
    done = team.record()
    assert done.code == 2, done.out
    assert done.doc["out"] is None and not team.out_path.exists()
    assert "tool:get_order" in done.doc["mocks_text"]
    assert any("inline array" in m["message"] for m in done.doc["mistakes"])
    code, out = team.record_text()
    assert code == 2 and "tool:get_order" in out and "[[test.mock]]" in out


@pytest.mark.design("record.incomplete_writes_nothing")
def test_a_scrimmage_error_while_recording_exits_3(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    team.write(
        """
def get_order(order_id):
    return {"id": order_id}

def answer(question):
    get_order("1")
    return "ok"
""",
        ["bot:get_order"],
        ("tool:get_order",),
    )

    real = recording.tool_outcome

    def buggy(line: dict[str, object], folder: Path) -> recording.Outcome:
        found = real(line, folder)
        del found
        return recording.Listed(capture.SCRIMMAGE_ERROR, "Scrimmage failed while recording it: X")

    monkeypatch.setattr(recording, "tool_outcome", buggy)
    done = team.record()
    assert done.code == 3, done.out
    assert done.doc["out"] is None and not team.out_path.exists()
    assert any("a bug in Scrimmage" in i["message"] for i in done.doc["infra"])


def test_a_failure_while_the_file_is_written_is_exit_3_and_writes_nothing(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    team.write(
        """
def get_order(order_id):
    return {"id": order_id}

def answer(question):
    get_order("1")
    return "ok"
""",
        ["bot:get_order"],
        ("tool:get_order",),
    )

    def broken(*args: object) -> None:
        del args
        raise toml_write.WriterBug("no read back")

    monkeypatch.setattr(recording, "write_atomically", broken)
    done = team.record()
    assert done.code == 3, done.out
    assert done.doc["out"] is None and not team.out_path.exists()
    assert any("nothing was written" in i["message"] for i in done.doc["infra"])


@pytest.mark.design("record.numbers")
def test_five_top_level_calls_one_unsavable_and_one_credential_count_3_of_4(team: Team) -> None:
    team.service.routes["/v1/secret/data/db"] = (200, {}, b'{"v": "x"}')
    team.write(
        """
def a(k):
    return "a"

def b(k):
    return "b"

def c(k):
    return "c"

def pic(k):
    return b"png"

def answer(question):
    a("1")
    b("1")
    c("1")
    pic("1")
    http_get("/v1/secret/data/db")
    return "ok"
""",
        ["bot:a", "bot:b", "bot:c", "bot:pic"],
        ("tool:a", "tool:b", "tool:c", "tool:pic"),
    )
    done = team.record()
    assert done.doc["numbers"] == {"calls_seen": 4, "calls_written": 3}, done.out
    assert done.code == 1
    code, out = team.record_text()
    assert "recorded 3 of 4 calls that needed a mock" in out and code == 1


@pytest.mark.design("record.every_gate")
def test_a_langchain_tool_is_recorded_with_no_tools_list(team: Team) -> None:
    team.write(
        """
from langchain_core.tools import tool as lc_tool

@lc_tool
def chained(key: str) -> str:
    \"\"\"A LangChain tool.\"\"\"
    return "chain " + key

def answer(question):
    chained.invoke({"key": "z"})
    return "ok"
""",
        None,
        ("tool:chained",),
    )
    team.toml.write_text(team.toml.read_text().replace("tools    = []\n", ""))
    done = team.record()
    assert done.code == 0, done.out
    (mock,) = done.mocks()
    assert mock["call"] == "tool:chained" and mock["returns"] == "chain z"
    code, out = team.test_output()
    assert code == 0, out


@pytest.mark.design("record.incomplete_reply")
def test_a_reply_the_agent_stopped_reading_falls_back_to_a_tool_mock(team: Team) -> None:
    team.service.routes["/big"] = (200, {"content-type": "text/plain"}, b"x" * (1024 * 1024))
    team.write(
        """
def first_chunk(key):
    with httpx.Client() as client:
        with client.stream("GET", SERVICE + "/big", timeout=30) as reply:
            for chunk in reply.iter_bytes(chunk_size=1024):
                return len(chunk)

def answer(question):
    first_chunk("x")
    return "ok"
""",
        ["bot:first_chunk"],
        ("tool:first_chunk",),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert [m["call"] for m in done.mocks()] == ["tool:first_chunk"]
    assert [m["path"] for m in done.manual("incomplete_reply")] == ["/big"]
    code, out = team.test_output()
    assert code == 0 and team.service.requests == [], out


# A tool mock is written only if nothing forbidden is beneath the tool.


def test_a_credential_reply_never_reaches_the_file_through_a_tool_mock(team: Team) -> None:
    team.service.routes["/oauth/token"] = (200, {}, b'{"access_token": "live-token-value"}')
    team.service.routes["/png"] = (200, {"content-type": "image/png"}, b"\x89PNG\r\n\xff\xfe")
    team.write(
        """
def connect(name):
    token = httpx.post(SERVICE + "/oauth/token", timeout=30, json={}).json()["access_token"]
    http_get("/png")
    return {"db_pw": token}

def answer(question):
    connect("x")
    return "ok"
""",
        ["bot:connect"],
        ("tool:connect", "http:localhost POST /oauth/token"),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert not any(m["call"] == "tool:connect" for m in done.mocks())
    assert "live-token-value" not in team.out_path.read_text()
    assert [s["path"] for s in done.skipped("credential")] == ["/oauth/token"]
    assert [m["path"] for m in done.manual("binary")] == ["/png"]
    assert done.doc["numbers"] == {"calls_seen": 1, "calls_written": 0}


@pytest.mark.parametrize(
    ("route", "extra"),
    [
        ("/down", "status"),
        ("/secret", "secret_changed_shape"),
        ("/model", "model_inside"),
    ],
    ids=["a-503", "a-password-body", "the-model"],
)
def test_nothing_forbidden_two_levels_down_gets_a_tool_mock(
    team: Team, fake_openai: Callable[..., FakeOpenAI], route: str, extra: str
) -> None:
    team.service.json("/down", {"error": "down"}, status=503)
    team.service.routes["/secret"] = (
        200,
        {"content-type": "application/json"},
        b'{"password": "hunter2", "v": null}',
    )
    team.write(
        f"""
import openai

def inner(name):
    if {route!r} == "/model":
        openai.OpenAI().chat.completions.create(
            model="gpt-4o-mini", messages=[{{"role": "user", "content": name}}]
        )
    else:
        http_get({route!r})
    return {{"inner": "done"}}

def outer(name):
    return inner(name)

def answer(question):
    outer("x")
    return "ok"
""",
        ["bot:inner", "bot:outer"],
        ("tool:inner", "tool:outer"),
        agent_extra='model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n',
    )
    if route == "/model":
        fake = fake_openai(["chat_answer"])
        team.project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
        team.project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    done = team.record()
    assert done.code == 1, done.out
    calls = {m["call"] for m in done.mocks()}
    assert "tool:outer" not in calls and "tool:inner" not in calls
    assert done.manual(extra)
    assert "hunter2" not in team.out_path.read_text()


def test_a_refusal_a_tool_swallows_still_stops_its_mock(team: Team) -> None:
    team.write(
        """
def careful(name):
    try:
        httpx.post(SERVICE + "/write", timeout=30, json={})
    except Exception:
        pass
    return {"fine": True}

def answer(question):
    careful("x")
    return "ok"
""",
        ["bot:careful"],
        ("tool:careful",),
    )
    done = team.record()
    assert done.code == 2, done.out
    assert not any(m["call"] == "tool:careful" for m in done.mocks())
    assert [m["path"] for m in done.manual("refused")] == ["/write"]


def test_a_web_request_body_never_goes_in_a_comment(team: Team) -> None:
    team.service.routes["/save"] = (503, {}, b"down")
    team.write(
        """
def save(name):
    httpx.post(SERVICE + "/save", json={"password": "hunter2", "note": "n"}, timeout=30)
    return "x"

def answer(question):
    save("x")
    return "ok"
""",
        ["bot:save"],
        ("tool:save", "http:localhost POST /save"),
    )
    done = team.record()
    assert done.code == 1, done.out
    text = team.out_path.read_text()
    assert "hunter2" not in text and "note" not in text


def test_json_inside_a_string_is_checked_by_its_keys(team: Team) -> None:
    team.write(
        """
def as_text(name):
    return json.dumps({"password": "hunter2", "v": 1})

def clean_text(name):
    return json.dumps({"v": 1})

def answer(question):
    as_text("x")
    clean_text("y")
    return "ok"
""",
        ["bot:as_text", "bot:clean_text"],
        ("tool:as_text", "tool:clean_text"),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert [m["call"] for m in done.manual("secret_changed_shape")] == ["tool:as_text"]
    assert [m["call"] for m in done.mocks()] == ["tool:clean_text"]
    assert "hunter2" not in team.out_path.read_text()


def test_an_existing_output_is_replaced_only_if_record_wrote_it(team: Team) -> None:
    team.write(
        """
def get_order(order_id):
    return {"id": order_id}

def answer(question):
    get_order("1")
    return "ok"
""",
        ["bot:get_order"],
        ("tool:get_order",),
    )
    team.out_path.write_text("# my own notes, kept by hand\n")
    refused = team.record()
    assert refused.code == 2 and refused.doc["out"] is None, refused.out
    assert team.out_path.read_text() == "# my own notes, kept by hand\n"
    assert any("did not write it" in m["message"] for m in refused.doc["mistakes"])
    team.out_path.unlink()
    first = team.record()
    assert first.code == 0 and team.out_path.exists(), first.out
    earlier = team.out_path.read_text()
    (team.root / "bot.py").write_text(
        (team.root / "bot.py").read_text().replace('get_order("1")', "pass")
    )
    empty = team.record()
    assert empty.doc["out"] is None and team.out_path.read_text() == earlier, empty.out
    assert any("kept as it was" in n["message"] for n in empty.doc["notes"])


def test_a_success_and_a_failure_on_the_same_arguments_are_nondeterministic(team: Team) -> None:
    team.write(
        """
COUNT = [0]

def flaky(key):
    COUNT[0] += 1
    if COUNT[0] == 1:
        return {"ok": True}
    raise ValueError("second call")

def nulled(key):
    COUNT[0] += 1
    return {"v": None} if COUNT[0] > 3 else {"v": 1}

def answer(question):
    flaky("a")
    try:
        flaky("a")
    except ValueError:
        pass
    nulled("b")
    nulled("b")
    return "ok"
""",
        ["bot:flaky", "bot:nulled"],
        ("tool:flaky", "tool:nulled"),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert done.mocks() == []
    assert sorted(m["call"] for m in done.manual("nondeterministic")) == [
        "tool:flaky",
        "tool:nulled",
    ]


def test_a_redirect_falls_back_to_the_tool_and_says_so(team: Team) -> None:
    team.service.routes["/old"] = (301, {"location": "/new"}, b"")
    team.service.json("/new", {"v": 1})
    team.write(
        """
def moved(name):
    return httpx.get(SERVICE + "/old", timeout=30, follow_redirects=True).json()

def answer(question):
    moved("x")
    return "ok"
""",
        ["bot:moved"],
        ("tool:moved",),
    )
    done = team.record()
    assert done.code == 1, done.out
    (mock,) = done.mocks()
    assert mock["call"] == "tool:moved"
    (entry,) = done.manual("status")
    assert entry["path"] == "/old" and "tool:moved" in entry["instead"]
    code, out = team.record_text()
    assert code == 1
    assert "instead:" in out and "by hand" not in out.split("records in", 1)[1]


def test_a_tight_loop_of_350_calls_completes(team: Team) -> None:
    team.write(
        """
def lookup(n):
    return {"n": n}

def answer(question):
    for i in range(350):
        lookup(i)
    return "ok"
""",
        ["bot:lookup"],
        ("tool:lookup",),
    )
    done = team.record()
    assert done.code == 0, done.out
    assert done.doc["numbers"] == {"calls_seen": 350, "calls_written": 350}


def test_a_body_of_newlines_is_measured_as_toml(team: Team) -> None:
    team.service.routes["/nl"] = (200, {"content-type": "text/plain"}, b"\n" * 40000)
    team.write(
        """
def lines(name):
    http_get("/nl")
    return "lines"

def answer(question):
    lines("x")
    return "ok"
""",
        ["bot:lines"],
        ("tool:lines",),
    )
    done = team.record()
    assert done.code == 1, done.out
    (entry,) = done.manual("too_large")
    assert "80002 bytes" in entry["message"]


def answers(team: Team) -> list[str]:
    files = sorted(team.root.glob("scrimmage-runs/*/*.jsonl"), key=lambda f: f.stat().st_mtime_ns)
    return [str(json.loads(f.read_text().splitlines()[-1])["summary"]["answer"]) for f in files]


TYPED = """
from dataclasses import dataclass
from typing import NamedTuple
import datetime
import pydantic

@dataclass
class Stock:
    sku: str
    count: int

class Point(NamedTuple):
    x: int
    y: float

class Order(pydantic.BaseModel):
    order_id: str
    total: float

class Rounded(pydantic.BaseModel):
    value: float | int

    @pydantic.field_serializer("value")
    def whole(self, value):
        return int(value)

def get_stock(sku):
    return Stock(sku, 12)

def get_point(n):
    return Point(1, 2.5)

def get_order(order_id):
    return Order(order_id=order_id, total=9.0)

def rounded(n):
    return Rounded(value=1.0)

def when(n):
    return datetime.datetime(2026, 1, 1)

def answer(question):
    values = [get_stock("a"), get_point("p"), get_order("o")]
    rounded("r")
    when("w")
    return "ok " + repr(values) + " " + " ".join(type(v).__name__ for v in values)
"""


def test_typed_values_round_trip_and_the_rest_are_listed_through_the_command(team: Team) -> None:
    team.write(
        TYPED,
        ["bot:get_stock", "bot:get_point", "bot:get_order", "bot:rounded", "bot:when"],
        ("tool:get_stock", "tool:get_point", "tool:get_order", "tool:rounded", "tool:when"),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert {m["call"] for m in done.mocks()} == {
        "tool:get_stock",
        "tool:get_point",
        "tool:get_order",
    }
    unsavable = {m["call"]: m["message"] for m in done.manual("unsavable")}
    assert set(unsavable) == {"tool:rounded", "tool:when"}
    assert "datetime" in unsavable["tool:when"]
    assert "value" in unsavable["tool:rounded"]
    code, out = team.test_output()
    assert code == 0, out
    assert len(set(answers(team))) == 1  # the recorded run and every test run gave one answer


def test_an_mcp_result_the_mock_cannot_give_is_listed_through_the_command(
    team: Team, tmp_path: Path
) -> None:
    team.project.monkeypatch.setenv(
        "DOCS_SERVER", str(Path(__file__).parent / "fakes/docs_server.py")
    )
    team.project.monkeypatch.setenv("DOCS_LOG", str(tmp_path / "docs.log"))
    team.project.monkeypatch.setenv("DOCS_MODE", "two")
    team.project.monkeypatch.setenv("DOCS_TRANSPORT", "stdio")
    team.write(
        """
from acme.docs_bot import connected

def answer(question):
    async def go():
        async with connected() as session:
            await session.list_tools()
            await session.call_tool("note", {"q": "x"})
    asyncio.run(go())
    return "ok"
""",
        [],
        ("mcp:docs/note",),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert done.mocks() == []
    assert [m["call"] for m in done.manual("unsavable")] == ["mcp:docs/note"]


def test_a_failure_before_the_file_lands_leaves_no_file(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    team.write(
        """
def get_order(order_id):
    return {"id": order_id}

def answer(question):
    get_order("1")
    return "ok"
""",
        ["bot:get_order"],
        ("tool:get_order",),
    )

    def broken(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("no diff")

    monkeypatch.setattr(difflib, "unified_diff", broken)
    done = team.record()
    assert done.code == 3 and not team.out_path.exists(), done.out
    assert any("nothing was written" in i["message"] for i in done.doc["infra"])


def test_a_json_text_secret_in_an_argument_is_in_no_comment_and_no_output(team: Team) -> None:
    team.write(
        """
def a8(a):
    return "seen"

def answer(question):
    a8(json.dumps({"password": "hunterK"}))
    return "ok"
""",
        ["bot:a8"],
        ("tool:a8",),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert [m["call"] for m in done.manual("secret_changed_shape")] == ["tool:a8"]
    assert "hunterK" not in team.out_path.read_text() and "hunterK" not in done.out
    assert "# tool:a8 with" not in team.out_path.read_text()
    code, out = team.record_text()
    assert code == 1 and "hunterK" not in out


@pytest.mark.parametrize(
    "reply",
    [
        {"body": json.dumps({"password": "hunterW"})},
        [json.dumps({"api_key": "hunterW"}), "plain"],
    ],
    ids=["dict", "list"],
)
def test_a_json_web_reply_is_checked_for_json_text_inside_it(team: Team, reply: object) -> None:
    team.service.json("/wrapped", reply)
    team.write(
        """
def wrapped(name):
    http_get("/wrapped")
    return "seen"

def answer(question):
    wrapped("x")
    return "ok"
""",
        ["bot:wrapped"],
        ("tool:wrapped",),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert [m["path"] for m in done.manual("secret_changed_shape")] == ["/wrapped"]
    assert "hunterW" not in team.out_path.read_text()
    assert not any(m.get("path") == "/wrapped" for m in done.mocks())


def test_a_json_reply_holding_a_form_field_in_a_string_is_written_as_valid_json(
    team: Team,
) -> None:
    """The body is redacted as text before it is read as JSON. A field cut at a backslash
    would leave the quote that ends the string, and the mock would not parse."""
    team.service.json("/cmd", {"cmd": 'curl -d "token=abc" https://a.b', "n": 1})
    team.write(
        """
def cmd(name):
    return http_get("/cmd").json()["n"]

def answer(question):
    cmd("x")
    return "ok"
""",
        ["bot:cmd"],
        ("tool:cmd",),
    )
    done = team.record()
    assert done.code == 0, done.out
    text = team.out_path.read_text()
    assert "abc" not in text
    mock = next(m for m in done.mocks() if m.get("path") == "/cmd")
    assert mock["json"] == {"cmd": 'curl -d "[secret removed]" https://a.b', "n": 1}


def test_a_basic_header_in_a_web_reply_or_a_tool_value_is_not_written(team: Team) -> None:
    """The name and the value are two strings, so only the pair rule sees them. A HAR header
    list, a [name, value] list and a name/value object are all judged."""
    secret = "dXNlcjpodW50ZXIy"
    team.service.json(
        "/har", {"headers": [{"name": "Authorization", "value": f"Basic {secret}"}], "n": 1}
    )
    team.write(
        f"""
def har(name):
    return http_get("/har").json()["n"]

def pairs(name):
    return [["Authorization", "Basic {secret}"], ["Accept", "*/*"]]

def named(name):
    return {{"name": "Proxy-Authorization", "value": "Basic {secret}"}}

def answer(question):
    har("x")
    pairs("x")
    named("x")
    return "ok"
""",
        ["bot:har", "bot:pairs", "bot:named"],
        ("tool:har", "tool:pairs", "tool:named"),
    )
    done = team.record()
    assert done.code == 0, done.out
    assert secret not in team.out_path.read_text()
    mocks = {m.get("path") or m["call"]: m for m in done.mocks()}
    assert mocks["/har"]["json"]["headers"][0]["value"] == "[secret removed]"
    assert mocks["tool:pairs"]["returns"] == [
        ["Authorization", "[secret removed]"],
        ["Accept", "*/*"],
    ]
    assert mocks["tool:named"]["returns"] == {
        "name": "Proxy-Authorization",
        "value": "[secret removed]",
    }


def test_a_json_reply_ending_a_string_on_a_field_name_is_written_as_valid_json(
    team: Team,
) -> None:
    """A field name at the end of a string must not take the quote that closes it."""
    team.service.json("/note", {"note": "send token=", "n": 1})
    team.write(
        """
def note(name):
    return http_get("/note").json()["n"]

def answer(question):
    note("x")
    return "ok"
""",
        ["bot:note"],
        ("tool:note",),
    )
    done = team.record()
    assert done.code == 0, done.out
    mock = next(m for m in done.mocks() if m.get("path") == "/note")
    assert mock["json"] == {"note": "send token=", "n": 1}


# The arguments of a call a mock cannot be written for, and the labels a miss now covers.

DOLLARS = """
def dollars(key):
    return "cost ${HOME}"
"""
RECORDED_ORDER = (
    '[[test]]\nname     = "t"\nquestion = "q"\nexpect   = ["ok"]\n'
    '[[test.mock]]\ncall = "tool:get_order"\nrecorded = true\nargs = {order_id = "5678"}\n'
    'returns = "shipped"\n'
)
ORDER_BOT = """
def get_order(order_id):
    return "shipped"

def answer(question):
    get_order("1")
    return "ok"
"""


def skeleton_of(team: Team, call: str, arg: str, defs: str = DOLLARS) -> tuple[str, Done]:
    team.write(
        f"{defs}\ndef answer(question):\n    {call}({arg})\n    return 'ok'\n",
        ["bot:dollars"],
        ("tool:dollars",),
    )
    team.out_path.unlink(missing_ok=True)  # an earlier output is kept, not rewritten
    done = team.record()
    assert [m["call"] for m in done.manual()] == ["tool:dollars"], done.out
    return team.out_path.read_text(), done


@pytest.mark.design("record.skeleton")
def test_a_skeleton_carries_exact_args_for_a_tool_call(team: Team) -> None:
    text, _ = skeleton_of(team, "dollars", '"d"')
    assert '# call    = "tool:dollars"\n# args    = {key = "d"}\n# returns = ...' in text
    assert tomllib.loads(text)["test"][0].get("mock") is None  # a comment: the file reads the same


@pytest.mark.design("record.skeleton")
def test_a_skeleton_has_no_args_line_when_the_arguments_held_a_secret(team: Team) -> None:
    both = DOLLARS.replace("(key)", "(key, password='x')")
    text, _ = skeleton_of(team, "dollars", f'"{SK}"', both)
    assert "# args" not in text and SK not in text
    text, _ = skeleton_of(team, "dollars", '"d", password="hunter2-plain"', both)
    assert "# args" not in text and "hunter2-plain" not in text


@pytest.mark.design("record.skeleton")
def test_a_skeleton_has_no_args_line_when_the_preview_was_cut(team: Team) -> None:
    text, _ = skeleton_of(team, "dollars", '"x" * 300')
    assert "# args" not in text


@pytest.mark.design("record.skeleton")
@pytest.mark.parametrize("value", ["https://x.acme/p?token=abc123", JWT])
def test_a_skeleton_has_no_args_line_when_the_preview_was_redacted(team: Team, value: str) -> None:
    text, _ = skeleton_of(team, "dollars", f'"{value}"')
    assert "# args" not in text and "abc123" not in text and JWT not in text


@pytest.mark.design("record.skeleton")
def test_a_skeleton_has_no_args_line_when_a_value_was_not_plain(team: Team) -> None:
    text, done = skeleton_of(team, "dollars", "{1, 2}")
    assert "# args" not in text
    real = [x for x in lines_of(done) if x.get("call") == "tool:dollars"]
    assert real and real[0]["args_not_plain"] is True


@pytest.mark.design("record.skeleton")
def test_a_refused_call_whose_value_was_not_plain_gets_no_args_line(team: Team) -> None:
    defs = DOLLARS + "import enum\nclass C(enum.StrEnum):\n    RED = 'red'\n"
    code = f"{defs}\ndef answer(question):\n    dollars(C.RED)\n    return 'ok'\n"
    team.write(code, ["bot:dollars"], ())
    team.out_path.unlink(missing_ok=True)  # an earlier output is kept, not rewritten
    done = team.record()
    assert [m["call"] for m in done.manual()] == ["tool:dollars"], done.out
    assert "# args" not in team.out_path.read_text()
    refused = [x for x in lines_of(done) if x.get("call") == "tool:dollars"]
    assert refused and refused[0]["args_not_plain"] is True


@pytest.mark.design("record.skeleton")
def test_record_names_real_labels_that_now_have_args_mocks(team: Team) -> None:
    team.write(ORDER_BOT, ["bot:get_order"], ("tool:get_order",))
    done = team.record()
    named = "tool:get_order is under real and now has a mock with args"
    assert any(named in n["message"] for n in done.doc["notes"])
    team.write(ORDER_BOT, ["bot:get_order"], ("tool:*",))
    done = team.record()
    assert not any(named in n["message"] for n in done.doc["notes"])


@pytest.mark.design("record.skeleton")
def test_record_under_a_wildcard_refuses_a_miss_and_writes_nothing_for_it(team: Team) -> None:
    team.write(ORDER_BOT, ["bot:get_order"], ("tool:*",), tests=RECORDED_ORDER)
    done = team.record()
    assert done.written_calls() == []
    assert [m["code"] for m in done.manual()] == ["refused"]
    assert "5678" not in done.manual()[0]["message"]
    assert "mock_misses" not in done.doc


@pytest.mark.design("record.skeleton")
def test_record_on_a_recorded_file_writes_a_missed_call_under_real_by_name(team: Team) -> None:
    team.write(ORDER_BOT, ["bot:get_order"], ("tool:get_order",), tests=RECORDED_ORDER)
    done = team.record()
    assert done.code == 0, done.out
    assert done.written_calls() == ["tool:get_order"]
    args = [m["args"] for m in done.mocks()]
    assert args == [{"order_id": "5678"}, {"order_id": "1"}]
