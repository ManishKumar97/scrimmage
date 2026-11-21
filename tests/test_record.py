"""scrimmage record end to end: real runs of a small bot against a local service."""

import contextlib
import dataclasses
import difflib
import hashlib
import json
import os
import re
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

import scrimmage
from conftest import Project, TimeLimit
from fakes.openai_server import FakeOpenAI
from scrimmage import capture, toml_write
from scrimmage import config as settings
from scrimmage import record as recording
from scrimmage import runner as running
from scrimmage.cli import main
from scrimmage.control import ControlServer
from test_cli_json import validate
from test_record_units import loaded, result_for

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
    def counts(self) -> dict[str, int]:
        """The numbers the slice began with: calls seen and calls written."""
        numbers = self.doc["numbers"]
        return {"calls_seen": numbers["calls_seen"], "calls_written": numbers["calls_written"]}

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
        # standard error stays empty: a record of several runs says what is real in a note
        assert captured.err == "", captured.err
        assert doc["command"] == "record" and doc["exit"] == code
        validate(doc)  # the shipped schema and the tests' closed copy of it
        if doc["out"] is not None:
            text = self.out_path.read_text()
            for entry in (m for found in doc["tests"] for m in found["manual"]):
                call = re.escape(f'= "{entry["call"]}"')
                assert re.search(f"# call +{call}", text), entry  # a skeleton per listing
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
    assert done.counts == {"calls_seen": 1, "calls_written": 1}
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
    schema = json.loads((Path(scrimmage.__file__).parent / "schemas/record.v1.json").read_text())
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
    team.service.routes["/lzw"] = (200, {"content-encoding": "compress"}, b"\x1f\x9d\x90compressed")
    team.write(
        """
def picture(name):
    http_get("/png")
    return "a picture"

def pressed(name):
    http_get("/lzw")
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
    assert {m["path"] for m in done.manual("binary")} == {"/png", "/lzw"}
    assert done.counts == {"calls_seen": 2, "calls_written": 2}
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
    assert done.counts == {"calls_seen": 3, "calls_written": 1}
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


INVOICE_BOT = """
def pdf():
    return b"%PDF-1.7 built"

def get_invoice(invoice_id):
    return b"%PDF-1.7 real " + invoice_id.encode()

def answer(question):
    get_invoice("INV-7")
    get_invoice("INV-8")
    return "ok"
"""
INVOICE_HAND = (
    '[[test.mock]]\ncall = "tool:get_invoice"\nargs = {invoice_id = "INV-8"}\nreturns = "hand"\n\n'
)
INVOICE_FROM = '[[test.mock]]\ncall = "tool:get_invoice"\n'


REFRESH_REFUSAL = (
    "--refresh tool:get_invoice: a returns_from mock with no args answers every call to the "
    "label, so nothing would run for real and any other mock on it would be dropped for "
    "nothing; give it args, refresh another label, or limit --test to tests with no "
    "returns_from mock without args on this label"
)


def invoice_team(team: Team, mocks: str) -> None:
    team.write(INVOICE_BOT, ["bot:get_invoice"], ("tool:get_invoice",), tests=ONE_TEST + mocks)


@pytest.mark.design("record.refresh_keeps_returns_from")
def test_refresh_refuses_a_returns_from_mock_that_answers_every_call(team: Team) -> None:
    invoice_team(team, INVOICE_HAND + INVOICE_FROM + 'returns_from = "bot:pdf"\n')
    before = team.toml.read_text()
    done = team.record("--refresh", "tool:get_invoice")
    assert done.code == 2, done.out
    assert not (team.root / "scrimmage-runs").exists()
    message = done.doc["mistakes"][0]["message"]
    assert message == REFRESH_REFUSAL
    assert team.toml.read_text() == before and not team.out_path.exists()


@pytest.mark.design("record.refresh_keeps_returns_from")
def test_refresh_refuses_a_label_with_only_a_returns_from_mock(team: Team) -> None:
    invoice_team(team, INVOICE_FROM + 'returns_from = "bot:pdf"\n')
    done = team.record("--refresh", "tool:get_invoice")
    assert done.code == 2, done.out
    assert not (team.root / "scrimmage-runs").exists()
    assert done.doc["mistakes"][0]["message"] == REFRESH_REFUSAL


@pytest.mark.design("record.refresh_keeps_returns_from")
def test_refresh_limited_to_a_test_with_a_plain_returns_mock_is_not_refused(team: Team) -> None:
    other = '\n[[test]]\nname     = "u"\nquestion = "q"\nexpect   = ["ok"]\n' + INVOICE_FROM
    mocks = '[[test.mock]]\ncall = "tool:get_invoice"\nreturns = "hand"\n' + other
    invoice_team(team, mocks + 'returns_from = "bot:pdf"\n')
    refused = team.record("--refresh", "tool:get_invoice")
    assert refused.code == 2 and refused.doc["mistakes"][0]["message"] == REFRESH_REFUSAL
    done = team.record("--refresh", "tool:get_invoice", "--test", "t")
    assert done.code != 2, done.out
    assert not done.doc["mistakes"], done.doc["mistakes"]


@pytest.mark.design("record.refresh_keeps_returns_from")
def test_refresh_keeps_a_returns_from_mock_with_args_and_records_the_rest(team: Team) -> None:
    invoice_team(team, INVOICE_FROM + 'args = {invoice_id = "INV-7"}\nreturns_from = "bot:pdf"\n')
    done = team.record("--refresh", "tool:get_invoice")
    assert done.code == 1, done.out  # incomplete: the bytes of INV-8 cannot be saved
    assert done.test["kept"] == [{"call": "tool:get_invoice", "mock": 1}]
    # Only INV-8 ran for real: seen once. INV-7 stays with the mock.
    assert done.test["written"] == []
    (sets,) = done.test["argument_sets"]
    assert sets["call"] == "tool:get_invoice" and sets["seen"] == 1 and sets["written"] == 0
    (mock,) = tomllib.loads(team.out_path.read_text())["test"][0]["mock"]
    assert mock == {
        "call": "tool:get_invoice",
        "args": {"invoice_id": "INV-7"},
        "returns_from": "bot:pdf",
    }


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
@pytest.mark.design("record.dollar_brace")
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
    assert manual == {"tool:nothing": "null", "tool:pic": "unsavable"}
    mocks = {m["call"]: m for m in done.mocks()}
    assert mocks["tool:marker"]["returns"] == fake
    assert "fault" not in done.output["test"][0]
    assert any("broke a rule" in n["message"] for n in done.doc["notes"])
    assert done.counts == {"calls_seen": 5, "calls_written": 3}
    text = team.out_path.read_text()
    # A recorded ${ is written as the literal $${, so the file never reads it from the
    # environment: the mock gives back the value as it was.
    assert 'returns  = "cost $${HOME}"' in text
    loaded = settings.load(team.out_path)
    dollars = next(m for m in loaded.tests[0].mocks if m.call == "tool:dollars")
    assert dollars.returns == "cost ${HOME}"
    for call in ("tool:nothing", "tool:pic"):
        assert f'# call         = "{call}"' in text  # a value TOML cannot hold: returns_from
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
    assert done.counts == {"calls_seen": 4, "calls_written": 3}, done.out
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
    team.service.routes["/oauth2/token"] = (200, {}, b'{"access_token": "live-token-value"}')
    team.service.routes["/png"] = (200, {"content-type": "image/png"}, b"\x89PNG\r\n\xff\xfe")
    team.write(
        """
def connect(name):
    token = httpx.post(SERVICE + "/oauth2/token", timeout=30, json={}).json()["access_token"]
    http_get("/png")
    return {"db_pw": token}

def answer(question):
    connect("x")
    return "ok"
""",
        ["bot:connect"],
        ("tool:connect", "http:localhost POST /oauth2/token"),
    )
    done = team.record()
    assert done.code == 1, done.out
    assert not any(m["call"] == "tool:connect" for m in done.mocks())
    assert "live-token-value" not in team.out_path.read_text()
    assert [s["path"] for s in done.skipped("credential")] == ["/oauth2/token"]
    assert [m["path"] for m in done.manual("binary")] == ["/png"]
    assert done.counts == {"calls_seen": 1, "calls_written": 0}


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
    assert done.counts == {"calls_seen": 350, "calls_written": 350}


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
    team.service.json("/cmd", {"cmd": 'curl -d "token=s3cr3t-zz" https://a.b', "n": 1})
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
    leaked = "s3cr3t-zz" in text  # not hex, so a random run id cannot hold it
    assert not leaked, "the secret was written"
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

# Its value holds None, which TOML has no form for, so its mock is always a skeleton.
DOLLARS = """
def dollars(key):
    return {"cost": None}
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
    # Its value holds None, which TOML cannot write, so the skeleton names returns_from.
    assert (
        '# call         = "tool:dollars"\n# args         = {key = "d"}\n'
        '# returns_from = "package.module:function"\n'
    ) in text
    assert tomllib.loads(text)["test"][0].get("mock") is None  # a comment: the file reads the same


@pytest.mark.design("record.skeleton_returns_from")
def test_a_value_a_mock_cannot_hold_names_returns_from(team: Team) -> None:
    pdf = "def dollars(key):\n    return b'%PDF-1.7'\n"
    text, done = skeleton_of(team, "dollars", '"d"', pdf)
    assert done.manual()[0]["message"] == "returns: a bytes, which a mock cannot hold"
    assert (
        "# scrimmage record could not write this mock: tool:dollars: returns: a bytes, which a "
        "mock cannot hold.\n# Build the value with a function of no arguments, named with "
        "returns_from, or leave the tool under real.\n"
    ) in text
    assert (
        '# call         = "tool:dollars"\n# args         = {key = "d"}\n'
        '# returns_from = "package.module:function"\n'
    ) in text
    assert "# returns = ..." not in text and "Write it by hand" not in text
    # A None in the value, which TOML has no form for, names it too.
    text, done = skeleton_of(team, "dollars", '"d"')
    assert done.manual()[0]["message"] == "returns holds None, which TOML cannot write"
    assert "returns_from" in text and "Write it by hand" not in text
    # Any other reason, such as a secret in the arguments, keeps the old hint.
    both = DOLLARS.replace("(key)", "(key, password='x')")
    text, _ = skeleton_of(team, "dollars", f'"{SK}"', both)
    assert "returns_from" not in text and "Write it by hand" in text
    assert '# call    = "tool:dollars"\n# returns = ...' in text


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


# Several runs of each test: scrimmage record --runs N.

COUNTER = """
import fcntl

def run_index():
    with open(os.environ["COUNTER"], "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        n = len(f.read().split())
        f.write("x\\n")
    return n
"""
MODEL_CALL = (
    """
import openai

def ask_model():
    openai.OpenAI().chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )
"""
    + COUNTER
)
BUDGET = 'max_cost_usd = 1.0\nmodel_hosts = [{host = "127.0.0.1", provider = "openai"}]\n'
STOCK = """
def get_stock(sku):
    return {"count": 3, "sku": sku}
"""


def many(
    team: Team,
    fake_openai: Callable[..., FakeOpenAI],
    code: str,
    tools: list[str],
    real: tuple[str, ...],
    tests: str | None = None,
    extra: str = "",
) -> FakeOpenAI:
    """A bot that asks the model once a run, so a budget can stand, and tells its runs apart
    with a counter file. The runs go one at a time unless the settings say otherwise."""
    fake = fake_openai(["chat_answer"])
    patch = team.project.monkeypatch
    patch.setenv("OPENAI_BASE_URL", fake.base_url)
    patch.setenv("OPENAI_API_KEY", "test")
    patch.setenv("COUNTER", str(team.root / "count.txt"))
    settings = extra if "parallel" in extra else "parallel = 1\n" + extra
    team.write(
        MODEL_CALL + code,
        tools,
        real,
        tests=tests,
        agent_extra=BUDGET + settings + "run_timeout = 60\n",
    )
    return fake


def answering(*calls: str) -> str:
    """A bot that asks the model once, makes these calls, and answers ok."""
    body = "".join(f"    {call}\n" for call in calls)
    return STOCK + "def answer(q):\n    ask_model()\n" + body + "    return 'ok'\n"


def run_ids(done: Done) -> list[str]:
    return [r["run"] for r in done.test["runs"]]


def stock_args(done: Done) -> list[str]:
    return [m["args"]["sku"] for m in done.mocks() if m["call"] == "tool:get_stock"]


@pytest.mark.design("record.runs_flag")
@pytest.mark.parametrize(
    ("value", "json_form"),
    [(v, True) for v in ["0", "-1", "1.5", "x", "", "1e3", "1_0", " 5", chr(0xFF15)]]
    + [("0", False)],
)
def test_runs_must_be_a_whole_number_of_one_or_more(
    team: Team, value: str, json_form: bool
) -> None:
    # no tool is under real, so a value that wrongly passed would start runs, not be refused
    team.write("def answer(q):\n    return 'ok'\n", [], ())
    if json_form:
        done = team.record("--runs", value)
        assert done.code == 2 and done.doc["out"] is None
        asked = [m["message"] for m in done.doc["mistakes"]]
        assert any("--runs: expected a whole number of 1 or more" in m for m in asked), done.out
    else:
        code, out = team.record_text("--runs", value)
        assert code == 2 and out == ""
    assert not (team.root / "scrimmage-runs").exists()
    assert not team.out_path.exists()


@pytest.mark.design("record.runs_flag")
def test_more_than_one_run_with_real_tools_and_no_budget_is_refused(team: Team) -> None:
    team.write(
        STOCK + "def answer(q):\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "2")
    assert done.code == 2 and done.doc["out"] is None
    (mistake,) = [m["message"] for m in done.doc["mistakes"]]
    assert "tool:get_stock" in mistake and "max_cost_usd" in mistake and "--runs 1" in mistake
    assert not (team.root / "scrimmage-runs").exists()
    one = team.record("--runs", "1")  # one run with no budget is today's command
    assert one.code == 0, one.out


@pytest.mark.design("record.runs_flag")
def test_tools_under_real_are_named_before_the_runs_start(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        STOCK + "def answer(q):\n    ask_model()\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    named = "2 runs of each of 1 test; each calls these for real: tool:get_stock"
    done = team.record("--runs", "2")  # standard output is one document
    assert done.err == "" and done.doc["notes"][0] == {"message": named}
    code, out = team.record_text("--runs", "2")
    assert code == 0, out
    first, second = out.splitlines()[:2]
    assert first == f"scrimmage record: {named}"
    assert second.startswith("scrimmage record: 1 test, 2 runs each, 2 runs;")


@pytest.mark.design("record.runs_flag")
def test_runs_runs_each_baseline_n_times_and_no_fault_rows(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    two = (
        '[[test]]\nname     = "t1"\nquestion = "q"\nexpect   = ["ok"]\n'
        '[[test.fault]]\ncall = "tool:get_stock"\ndo   = "error"\nwhen = "first 1"\n'
        '[[test]]\nname     = "t2"\nquestion = "q"\nexpect   = ["ok"]\n'
    )
    many(
        team,
        fake_openai,
        STOCK + "def answer(q):\n    ask_model()\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
        tests=two,
    )
    done = team.record("--runs", "3")
    assert done.code == 0, done.out
    assert len(list(team.root.glob("scrimmage-runs/*/*.jsonl"))) == 6
    assert done.doc["numbers"]["runs"] == 3 and done.doc["numbers"]["runs_recorded"] == 6
    assert [len(t["runs"]) for t in done.doc["tests"]] == [3, 3]
    for path in team.root.glob("scrimmage-runs/*/*.jsonl"):
        assert all("fault" not in json.loads(text) for text in path.read_text().splitlines())


@pytest.mark.design("record.runs_flag")
def test_record_ignores_the_files_runs_setting(team: Team) -> None:
    team.write(
        STOCK + "def answer(q):\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
        agent_extra="runs     = 7\n",
    )
    done = team.record()
    assert done.code == 0 and done.doc["numbers"]["runs"] == 1
    assert len(list(team.root.glob("scrimmage-runs/*/*.jsonl"))) == 1


@pytest.mark.design("record.runs_flag")
def test_one_run_by_default_gives_todays_output(team: Team) -> None:
    team.service.json("/stock/1", {"count": 3})
    team.write(
        """
def get_order(order_id):
    return {"order_id": order_id, "status": "shipped"}

def stock(sku):
    return http_get(f"/stock/{sku}").json()["count"]

def answer(question):
    get_order("1")
    return f"ok {stock('1')}"
""",
        ["bot:get_order", "bot:stock"],
        ("tool:get_order", "tool:stock"),
    )
    done = team.record()
    assert done.code == 0, done.out
    code, text = team.record_text()
    assert code == 0
    for_json = {k: v for k, v in done.doc.items() if k in ("numbers", "tests", "out")}
    for_json["numbers"] = {k: for_json["numbers"][k] for k in ("calls_seen", "calls_written")}
    for_json["tests"] = [without_new(t) for t in for_json["tests"]]
    got = {
        "json": normal(json.dumps(for_json, indent=1, sort_keys=True), team),
        "text": normal(text, team),
    }
    fixture = json.loads((Path(__file__).parent / "fixtures/record_default.json").read_text())
    assert got == fixture


def without_new(test: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in test.items() if k not in ("runs", "not_started", "argument_sets")} | {
        "written": [{k: v for k, v in w.items() if k != "runs"} for w in test["written"]]
    }


def normal(text: str, team: Team) -> str:
    """A run's text with what changes from one run to the next replaced."""
    text = text.replace(str(team.root), "ROOT")
    text = re.sub(r"run-[0-9a-f]{12}", "RUN", text)
    text = re.sub(r"scrimmage-runs/[0-9TZ]+-[0-9a-f]+", "scrimmage-runs/FOLDER", text)
    return re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", text)


# One Recorder over every run.


@pytest.mark.design("record.runs_merge")
def test_two_runs_with_different_arguments_write_one_exact_mock_each(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    marker = team.root / "real-calls.txt"
    many(
        team,
        fake_openai,
        """
def get_stock(sku):
    with open(os.environ["MARKER"], "a") as f:
        f.write("x")
    return {"count": 3, "sku": sku}

def answer(q):
    ask_model()
    get_stock("a" if run_index() % 2 == 0 else "b")
    return "ok"
""",
        ["bot:get_stock"],
        ("tool:get_stock",),
        extra="runs = 4\n",
    )
    team.project.monkeypatch.setenv("MARKER", str(marker))
    done = team.record("--runs", "2")
    assert done.code == 0, done.out
    assert sorted(stock_args(done)) == ["a", "b"] and done.counts == {
        "calls_seen": 2,
        "calls_written": 2,
    }
    assert done.doc["numbers"]["argument_sets_seen"] == 2
    assert done.doc["numbers"]["argument_sets_written"] == 2
    marker.write_text("")
    code, out = team.test_output()  # four runs, both forms: each hits a mock
    assert code == 0, out
    assert marker.read_text() == "", "a real tool ran in the test"


@pytest.mark.design("record.runs_merge")
def test_the_same_arguments_in_every_run_write_one_mock_with_every_call_counted(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        STOCK + "def answer(q):\n    ask_model()\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "3")
    assert done.code == 0, done.out
    (written,) = done.test["written"]
    assert written["calls"] == 3 and written["runs"] == 3
    assert done.counts == {"calls_seen": 3, "calls_written": 3}


@pytest.mark.design("record.runs_merge")
def test_a_reply_that_differs_for_the_same_arguments_across_runs_is_not_written(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        """
def flaky(k):
    return {"run": RUN}

def steady(k):
    return {"fine": True}

def answer(q):
    global RUN
    ask_model()
    RUN = run_index()
    flaky("x")
    steady("y")
    return "ok"
""",
        ["bot:flaky", "bot:steady"],
        ("tool:flaky", "tool:steady"),
    )
    done = team.record("--runs", "2")
    assert done.code == 1, done.out
    assert done.written_calls() == ["tool:steady"]
    (entry,) = done.manual("nondeterministic")
    assert entry["call"] == "tool:flaky" and "different replies" in entry["message"]
    assert all(run in entry["message"] for run in run_ids(done))
    assert '# args    = {k = "x"}' in team.out_path.read_text()


@pytest.mark.design("record.runs_merge")
def test_a_web_reply_that_differs_across_runs_is_not_written(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    served = iter(range(100))

    def next_reply(query: str, body: bytes) -> Route:
        del query, body
        return 200, {"content-type": "application/json"}, json.dumps({"n": next(served)}).encode()

    team.service.dynamic["/n"] = next_reply
    team.service.json("/fixed", {"same": 1})
    many(
        team,
        fake_openai,
        """
def fetch(k):
    http_get("/n")
    http_get("/fixed")
    return {"fine": True}

def answer(q):
    ask_model()
    fetch("x")
    return "ok"
""",
        ["bot:fetch"],
        ("tool:fetch",),
    )
    done = team.record("--runs", "2")
    assert done.code == 1, done.out
    assert [m["path"] for m in done.manual("nondeterministic")] == ["/n"]
    assert [m["path"] for m in done.mocks() if "path" in m] == []  # no web mock; the tool's stands
    assert done.written_calls() == ["tool:fetch"]


@pytest.mark.design("record.runs_merge")
def test_a_call_seen_in_some_runs_is_written_and_says_in_how_many(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        STOCK
        + """
def answer(q):
    ask_model()
    n = run_index()
    get_stock("a")
    if n == 1:
        get_stock("b")
    return "ok"
""",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "3")
    assert done.code == 0, done.out
    assert stock_args(done) == ["a", "b"]
    assert [w["runs"] for w in done.test["written"]] == [3, 1]
    assert 'tool:get_stock with {"sku": "b"}, 1 call in 1 of 3 runs.' in team.out_path.read_text()


@pytest.mark.design("record.runs_merge")
def test_runs_with_different_tool_orders_write_the_same_mocks(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        STOCK
        + """
def answer(q):
    ask_model()
    order = ["a", "b"] if run_index() % 2 == 0 else ["b", "a"]
    for sku in order:
        get_stock(sku)
    return "ok"
""",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "2")
    assert done.code == 0, done.out
    assert sorted(stock_args(done)) == ["a", "b"] and done.counts["calls_written"] == 4


@pytest.mark.design("record.runs_merge")
def test_a_success_and_a_failure_on_the_same_arguments_in_two_runs_write_no_mock(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        """
def lookup(k):
    if RUN == 1:
        raise RuntimeError("down")
    return {"found": k}

def steady(k):
    return {"fine": k}

def answer(q):
    global RUN
    ask_model()
    RUN = run_index()
    try:
        lookup("x")
    except RuntimeError:
        pass
    steady("y")
    return "ok"
""",
        ["bot:lookup", "bot:steady"],
        ("tool:lookup", "tool:steady"),
    )
    done = team.record("--runs", "2")
    assert done.code == 1, done.out
    assert done.written_calls() == ["tool:steady"]
    assert [m["call"] for m in done.manual("nondeterministic")] == ["tool:lookup"]


def stored(folder: Path, value: object, text: str | None = None) -> dict[str, object]:
    data = (text if text is not None else json.dumps(value, sort_keys=True)).encode()
    sha = hashlib.sha256(data).hexdigest()
    (folder / "blobs").mkdir(exist_ok=True)
    (folder / "blobs" / sha).write_bytes(data)
    return {"sha256": sha, "bytes": len(data), "preview": data.decode()}


def tool_line(
    folder: Path, call_id: int, args_text: str, reply: object, error: str | None = None
) -> dict[str, object]:
    args = stored(folder, None, args_text)
    line: dict[str, object] = {
        "call": "tool:t",
        "kind": "tool",
        "action": "real",
        "call_id": call_id,
        "proc": {"pid": 1},
        "args": args,
    }
    if error is not None:
        line["error"] = error
    else:
        line["recorded"] = {"value": stored(folder, reply), "args": args}
    return line


def two_runs(
    tmp_path: Path, first: dict[str, object], second: dict[str, object]
) -> recording.TestRecord:
    config = loaded(tmp_path)
    test = config.tests[1]
    runs = [result_for(test), result_for(test)]
    runs[1] = dataclasses.replace(runs[1], run="run-2")
    return recording.Recorder(test, runs, tmp_path).run(
        [(runs[0], [first]), (runs[1], [second])], []
    )


@pytest.mark.design("record.runs_merge")
def test_siblings_with_equal_arguments_in_another_key_order_are_one_group(tmp_path: Path) -> None:
    saved = tool_line(tmp_path, 1, '{"a": 1, "b": 2}', {"ok": True})
    failed = tool_line(tmp_path, 1, '{"b": 2, "a": 1}', None, error="RuntimeError: down")
    assert saved["args"] != failed["args"]  # two hashes of one set of arguments
    found = two_runs(tmp_path, saved, failed)
    assert found.written == []
    assert [e.code for e in found.manual.values()] == ["nondeterministic"]


@pytest.mark.design("record.runs_merge")
def test_a_call_whose_tool_never_finished_is_not_a_sibling_failure(tmp_path: Path) -> None:
    saved = tool_line(tmp_path, 1, '{"a": 1}', {"ok": True})
    stopped = tool_line(tmp_path, 1, '{"a": 1}', None, error="ScrimmageStopped: the run ended")
    found = two_runs(tmp_path, saved, stopped)
    assert [w.saved.fields["args"] for w in found.written] == [{"a": 1}]
    assert [e.code for e in found.manual.values()] == ["not_finished"]


@pytest.mark.design("record.runs_merge")
@pytest.mark.parametrize("bad", ["status", "binary"])
@pytest.mark.parametrize("bad_first", [False, True])
def test_a_web_call_listed_in_one_run_blocks_its_saved_twin_in_another(
    team: Team, fake_openai: Callable[..., FakeOpenAI], bad: str, bad_first: bool
) -> None:
    served = iter(range(100))

    def reply(query: str, body: bytes) -> Route:
        del query, body
        good = (next(served) == 0) != bad_first
        if good:
            return 200, {"content-type": "application/json"}, b'{"ok": true}'
        if bad == "status":
            return 503, {"content-type": "application/json"}, b'{"error": "down"}'
        return 200, {"content-type": "image/png"}, b"\x89PNG\r\n\xff\xfe"

    team.service.dynamic["/x"] = reply
    many(
        team,
        fake_openai,
        """
def fetch(k):
    http_get("/x")
    return {"fine": True}

def answer(q):
    ask_model()
    fetch("x")
    return "ok"
""",
        ["bot:fetch"],
        ("tool:fetch",),
    )
    done = team.record("--runs", "2")
    assert done.code == 1, done.out
    assert [m for m in done.mocks() if m["call"] == "http:localhost"] == []


@pytest.mark.design("record.runs_merge")
@pytest.mark.parametrize("later", ["the same reply", "a different reply each run"])
def test_web_children_of_a_tool_that_raised_in_another_run_are_not_written(
    team: Team, fake_openai: Callable[..., FakeOpenAI], later: str
) -> None:
    team.service.json("/x", {"v": 1})
    reply = '{"fine": True}' if later == "the same reply" else '{"t": RUN}'
    many(
        team,
        fake_openai,
        f"""
def fetch(k):
    http_get("/x")
    if RUN == 1:
        raise RuntimeError("after the request")
    return {reply}

def answer(q):
    global RUN
    ask_model()
    RUN = run_index()
    try:
        fetch("x")
    except RuntimeError:
        pass
    return "ok"
""",
        ["bot:fetch"],
        ("tool:fetch",),
    )
    done = team.record("--runs", "3")
    assert done.code == 1, done.out
    assert done.mocks() == []
    assert "tool:fetch" in {m["call"] for m in done.manual("nondeterministic")}


@pytest.mark.design("record.runs_merge")
@pytest.mark.parametrize("bad", ["status", "binary"])
def test_a_web_key_with_a_retry_in_one_run_is_blocked_in_every_run(
    team: Team, fake_openai: Callable[..., FakeOpenAI], bad: str
) -> None:
    served = iter(range(100))

    def reply(query: str, body: bytes) -> Route:
        del query, body
        if next(served) == 0:  # the first request of the first run fails; its retry works
            if bad == "status":
                return 503, {"content-type": "application/json"}, b'{"error": "down"}'
            return 200, {"content-type": "image/png"}, b"\x89PNG\r\n\xff\xfe"
        return 200, {"content-type": "application/json"}, b'{"ok": true}'

    team.service.dynamic["/x"] = reply
    many(
        team,
        fake_openai,
        """
def fetch(k):
    r = http_get("/x")
    if r.status_code != 200 or r.headers["content-type"] != "application/json":
        http_get("/x")
    return {"fine": True}

def answer(q):
    ask_model()
    fetch("x")
    return "ok"
""",
        ["bot:fetch"],
        ("tool:fetch",),
    )
    done = team.record("--runs", "2")
    assert done.code == 1, done.out
    assert [m for m in done.mocks() if m["call"] == "http:localhost"] == []


@pytest.mark.design("record.runs_flag")
def test_runs_has_a_cap(team: Team) -> None:
    team.write("def answer(q):\n    return 'ok'\n", [], ())
    done = team.record("--runs", "1001")
    assert done.code == 2 and not (team.root / "scrimmage-runs").exists()
    assert any("at most 1000" in m["message"] for m in done.doc["mistakes"])


@pytest.mark.design("record.runs_merge")
@pytest.mark.parametrize("beneath", ["model", "credential"])
def test_a_tool_with_a_model_call_beneath_in_one_run_gets_no_tool_mock_from_another_run(
    team: Team, fake_openai: Callable[..., FakeOpenAI], beneath: str
) -> None:
    team.service.routes["/oauth2/token"] = (200, {}, b'{"access_token": "live-token-value"}')
    inside = (
        "ask_model()"
        if beneath == "model"
        else 'httpx.post(SERVICE + "/oauth2/token", timeout=30, json={})'
    )
    many(
        team,
        fake_openai,
        f"""
def inner(k):
    if RUN == 0:
        {inside}
    return {{"same": True}}

def answer(q):
    global RUN
    ask_model()
    RUN = run_index()
    inner("x")
    return "ok"
""",
        ["bot:inner"],
        ("tool:inner", "http:localhost POST /oauth2/token"),
    )
    done = team.record("--runs", "2")
    assert "tool:inner" not in done.written_calls() and done.mocks() == []
    assert bool(done.skipped("credential")) == (beneath == "credential")
    # the clean call in the other run is listed too, and its skeleton names the runs it was made in
    code = "model_inside" if beneath == "model" else "nondeterministic"
    said = [m["message"] for m in done.manual(code) if m["call"] == "tool:inner"]
    assert any("this call was made in run-" in m for m in said), said
    # the clean call never reached the forbidden call itself, so its reason says another run did
    clean = [m for m in said if "this call was made in run-" in m]
    assert all("in another run" in m for m in clean), clean


@pytest.mark.design("record.runs_merge")
def test_a_time_varying_tool_is_not_written_and_the_text_names_the_runs_that_differed(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        STOCK
        + """
def now(k):
    return {"tick": TICK}

def answer(q):
    global TICK
    ask_model()
    TICK = run_index()
    now("x")
    get_stock("a")
    return "ok"
""",
        ["bot:now", "bot:get_stock"],
        ("tool:now", "tool:get_stock"),
    )
    code, out = team.record_text("--runs", "3")
    assert code == 1, out
    ids = re.findall(
        r"run-[0-9a-f]{12}", next(x for x in out.splitlines() if x.startswith("by hand:"))
    )
    assert len(set(ids)) == 3
    assert (
        'by hand: t: tool:now with {"k": "x"}: the same call got different replies in run-' in out
    )
    mocks = tomllib.loads(team.out_path.read_text())["test"][0]["mock"]
    assert [m["call"] for m in mocks] == ["tool:get_stock"]


@pytest.mark.design("record.runs_merge")
def test_each_listed_argument_set_gets_its_own_skeleton(team: Team) -> None:
    team.write(
        """
def boom(key):
    raise RuntimeError("down")

def answer(q):
    for key in ("a", "b"):
        try:
            boom(key)
        except RuntimeError:
            pass
    return "ok"
""",
        ["bot:boom"],
        ("tool:boom",),
    )
    done = team.record()
    assert done.code == 1, done.out
    text = team.out_path.read_text()
    assert '# args    = {key = "a"}' in text and '# args    = {key = "b"}' in text
    assert len(done.manual("raised")) == 2


@pytest.mark.design("record.runs_merge")
def test_an_incomplete_run_writes_nothing_for_its_test_and_exits_3(
    team: Team, fake_openai: Callable[..., FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    many(
        team,
        fake_openai,
        STOCK + "def answer(q):\n    ask_model()\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    real_close = ControlServer.close_run
    closed: list[str] = []

    def drop_a_line_in_the_second(self: Any, run: str) -> Any:
        state = real_close(self, run)
        closed.append(run)
        if len(closed) == 2:
            state.dropped = 1
        return state

    monkeypatch.setattr(ControlServer, "close_run", drop_a_line_in_the_second)
    done = team.record("--runs", "3")
    assert done.code == 3, done.out
    assert done.doc["out"] is None and not team.out_path.exists()
    assert any("nothing was written" in i["message"] for i in done.doc["infra"])


@pytest.mark.design("record.runs_merge")
def test_a_scrimmage_error_in_one_run_writes_nothing_for_its_test(
    team: Team, fake_openai: Callable[..., FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    many(
        team,
        fake_openai,
        STOCK + "def answer(q):\n    ask_model()\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    real = recording.tool_outcome
    seen: list[int] = []

    def buggy(line: dict[str, object], folder: Path) -> recording.Outcome:
        found = real(line, folder)
        seen.append(1)
        if len(seen) == 2:  # the second run's call, not the first's
            return recording.Listed(capture.SCRIMMAGE_ERROR, "Scrimmage failed: X")
        return found

    monkeypatch.setattr(recording, "tool_outcome", buggy)
    done = team.record("--runs", "2")
    assert done.code == 3, done.out
    assert done.doc["out"] is None and not team.out_path.exists()


# Refusals, the budget and other stops.

STOP_BOT = """
import openai

def read_stock(sku):
    return {"count": 3}

def answer(question):
    read_stock("a")
    openai.OpenAI().chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "a very long " * 2000}]
    )
    return "ok"
"""
TIGHT = 'max_cost_usd = 0.000001\nmodel_hosts = [{host = "127.0.0.1", provider = "openai"}]\n'


@pytest.mark.design("record.runs_refusal")
def test_a_refusal_in_one_run_stops_the_row_lists_the_call_and_writes_the_earlier_runs(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    marker = team.root / "refund-ran.txt"
    many(
        team,
        fake_openai,
        """
def read_stock(sku):
    return {"count": 3}

def refund_order(order_id):
    open(os.environ["MARKER"], "w").write("it ran")

def answer(q):
    ask_model()
    n = run_index()
    read_stock(f"r{n}")
    if n == 2:
        refund_order("1")
    return "ok"
""",
        ["bot:read_stock", "bot:refund_order"],
        ("tool:read_stock",),
    )
    team.project.monkeypatch.setenv("MARKER", str(marker))
    done = team.record("--runs", "5")
    assert done.code == 2, done.out
    assert [m["args"]["sku"] for m in done.mocks()] == [
        "r0",
        "r1",
        "r2",
    ]  # run 3 wrote its first call
    assert [m["call"] for m in done.manual("refused")] == ["tool:refund_order"]
    assert not marker.exists()
    assert done.test["not_started"] == 2 and len(done.test["runs"]) == 3
    assert any(
        "run 3 stopped at a refusal; 2 of 5 runs did not start" in n["message"]
        for n in done.doc["notes"]
    )


@pytest.mark.design("record.runs_budget")
def test_a_budget_stop_writes_nothing_and_keeps_the_existing_file(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    two = ONE_TEST + '[[test]]\nname     = "t2"\nquestion = "q"\nexpect   = ["ok"]\n'
    team.write(STOP_BOT, ["bot:read_stock"], ("tool:read_stock",), tests=two, agent_extra=TIGHT)
    patch = team.project.monkeypatch
    patch.setenv("OPENAI_BASE_URL", fake_openai([]).base_url)
    patch.setenv("OPENAI_API_KEY", "test")
    earlier = (
        "# Written by scrimmage record from scrimmage.toml on 2026-01-01. Review it.\n"
        + team.toml.read_text()
    )
    team.out_path.write_text(earlier)
    done = team.record("--runs", "3")
    assert done.code == 2, done.out
    assert done.doc["out"] is None and team.out_path.read_text() == earlier
    (line,) = [m["message"] for m in done.doc["mistakes"] if "budget stopped" in m["message"]]
    assert "after 0 of 3 runs of t and 0 of 3 of t2" in line and "nothing was written" in line
    assert not any("rows without runs" in m["message"] for m in done.doc["mistakes"])


@pytest.mark.design("record.runs_budget")
def test_a_budget_stop_with_one_run_writes_nothing(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    fake = fake_openai([])
    team.write(STOP_BOT, ["bot:read_stock"], ("tool:read_stock",), agent_extra=TIGHT)
    team.project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    team.project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    done = team.record()
    assert done.code == 2, done.out
    assert done.test["outcome"] in ("budget reached", "not reserved")
    assert done.doc["out"] is None and not team.out_path.exists()
    assert done.test["written"] == [] and done.counts["calls_written"] == 0


@pytest.mark.design("record.runs_budget")
def test_a_budget_stop_with_an_infrastructure_error_exits_3(
    team: Team, fake_openai: Callable[..., FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_openai([])
    team.write(STOP_BOT, ["bot:read_stock"], ("tool:read_stock",), agent_extra=TIGHT)
    team.project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    team.project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    real_close = ControlServer.close_run

    def drop_a_line(self: Any, run: str) -> Any:
        state = real_close(self, run)
        state.dropped = 1
        return state

    monkeypatch.setattr(ControlServer, "close_run", drop_a_line)
    done = team.record()
    assert done.code == 3, done.out
    assert done.doc["out"] is None and not team.out_path.exists()


@pytest.mark.design("record.runs_budget")
@pytest.mark.parametrize("reason", ["unseen_tool", "unknown_label"])
def test_a_test_cut_short_by_any_stop_writes_nothing_and_says_how_many_did_not_start(
    team: Team, fake_openai: Callable[..., FakeOpenAI], monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    many(
        team,
        fake_openai,
        STOCK + "def answer(q):\n    ask_model()\n    get_stock('a')\n    return 'ok'\n",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    real_one = running.Runner.one

    def stopping(self: running.Runner, row: Any) -> Any:
        """The stop the runner makes for this reason, after the first run, as it makes it."""
        result = real_one(self, row)
        if self.stopped_by is None:
            self.stopped_by = reason
            self.cancel()
        return result

    monkeypatch.setattr(running.Runner, "one", stopping)
    done = team.record("--runs", "3")
    assert done.doc["out"] is None and not team.out_path.exists()
    assert done.test["not_started"] == 2 and len(done.test["runs"]) == 1
    text = " ".join(n["message"] for n in done.doc["notes"])
    assert f"stopped by {reason}; 2 of 3 runs did not start" in text


# Identity, order, the comment.

TWO_FORMS = (
    STOCK
    + """
def answer(q):
    get_stock("a")
    if run_index() > 0:
        get_stock("b")
    return "ok"
"""
)


@pytest.mark.design("record.runs_identity")
def test_rerecording_a_recorded_file_adds_only_unseen_argument_sets(team: Team) -> None:
    team.project.monkeypatch.setenv("COUNTER", str(team.root / "count.txt"))
    team.write(COUNTER + TWO_FORMS, ["bot:get_stock"], ("tool:get_stock",))
    first = team.record()
    assert first.code == 0 and stock_args(first) == ["a"]
    again = main(["record", str(team.out_path), "--json"])
    captured = team.project.capsys.readouterr()
    doc = json.loads(captured.out)
    assert again == 0 and doc["out"] == str(team.root / "scrimmage.recorded.recorded.toml")
    mocks = tomllib.loads(Path(doc["out"]).read_text())["test"][0]["mock"]
    assert [m["args"]["sku"] for m in mocks] == ["a", "b"]  # a kept, b added, a not written twice


@pytest.mark.design("record.runs_identity")
@pytest.mark.parametrize("value", ["1", "1.0", "True", '"1"'])
def test_no_two_mocks_in_the_output_answer_the_same_exact_arguments(
    team: Team, fake_openai: Callable[..., FakeOpenAI], value: str
) -> None:
    many(
        team,
        fake_openai,
        f"""
def get_stock(k):
    return {{"k": 1}}

def answer(q):
    ask_model()
    get_stock([1, 1.0, True, "1"][run_index()])
    get_stock({value})
    return "ok"
""",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "4")
    assert done.code == 0, done.out
    kinds = sorted(type(m["args"]["k"]).__name__ for m in done.mocks())
    assert kinds == ["bool", "float", "int", "str"], "four sets, four mocks, told apart by type"
    assert sum(w["calls"] for w in done.test["written"]) == 8


@pytest.mark.design("record.runs_identity")
def test_mock_order_follows_the_run_submitted_first_not_the_one_that_finished_first(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    fifo = team.root / "meet"
    os.mkfifo(fifo)
    team.project.monkeypatch.setenv("MEET", str(fifo))
    many(
        team,
        fake_openai,
        STOCK
        + """
def answer(q):
    ask_model()
    n = run_index()
    if n == 0:  # the first to start makes its call after the second has made and ended its own
        open(os.environ["MEET"], "rb").read(1)
    get_stock(os.environ["SCRIMMAGE_RUN"])
    if n == 1:
        open(os.environ["MEET"], "wb").write(b"x")
    return "ok"
""",
        ["bot:get_stock"],
        ("tool:get_stock",),
        extra="parallel = 2\n",
    )
    done = team.record("--runs", "2")
    assert done.code == 0, done.out
    assert stock_args(done) == run_ids(
        done
    )  # the mocks follow the runs, as tests[].runs lists them


@pytest.mark.design("record.runs_identity")
def test_a_mock_comment_says_in_how_many_runs_its_call_was_seen(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        answering("get_stock('a' if run_index() == 1 else 'b')"),
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "3")
    text = team.out_path.read_text()
    assert 'tool:get_stock with {"sku": "a"}, 1 call in 1 of 3 runs.' in text
    assert 'tool:get_stock with {"sku": "b"}, 2 calls in 2 of 3 runs.' in text
    assert done.code == 0


@pytest.mark.design("record.runs_identity")
def test_a_mock_comment_names_its_runs_outcome_when_it_did_not_answer(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        STOCK
        + """
def answer(q):
    ask_model()
    get_stock("a")
    if run_index() == 0:
        raise RuntimeError("the first run crashes after its call")
    return "ok"
""",
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "2")
    assert done.code == 1, done.out
    first = run_ids(done)[0]
    assert f"{first} (crashed): tool:get_stock" in team.out_path.read_text()


# Output.


@pytest.mark.design("record.runs_output")
def test_record_json_counts_calls_per_run_and_in_total(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        answering("get_stock(f'r{run_index()}')"),
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "3")
    assert done.counts == {"calls_seen": 3, "calls_written": 3}
    assert done.doc["numbers"]["runs"] == 3 and done.doc["numbers"]["runs_recorded"] == 3
    runs = done.test["runs"]
    assert [(r["calls_seen"], r["calls_written"], r["outcome"]) for r in runs] == [
        (1, 1, "answered")
    ] * 3
    assert abs(sum(r["cost_usd"] for r in runs) - done.doc["cost_usd"]) < 1e-6
    assert done.test["outcome"] == "answered" and done.test["run"] == runs[0]["run"]


@pytest.mark.design("record.runs_output")
def test_record_json_names_argument_sets_per_call_without_values(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        answering("get_stock('sku-VALUE-1' if run_index() == 0 else 'sku-VALUE-2')"),
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    done = team.record("--runs", "3")
    (sets,) = done.test["argument_sets"]
    assert sets["call"] == "tool:get_stock" and sets["seen"] == 2 and sets["written"] == 2
    assert sets["sets"] == [
        {"runs": 1, "mock": 1, "code": None},
        {"runs": 2, "mock": 2, "code": None},
    ]
    assert "VALUE" not in json.dumps(done.doc)
    assert done.doc["numbers"]["argument_sets_seen"] == 2


@pytest.mark.design("record.runs_output")
def test_record_text_prints_each_run_and_each_calls_argument_sets(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    many(
        team,
        fake_openai,
        answering("get_stock('b' if run_index() == 2 else 'a')"),
        ["bot:get_stock"],
        ("tool:get_stock",),
    )
    code, out = team.record_text("--runs", "3")
    assert code == 0, out
    assert "scrimmage record: 1 test, 3 runs each, 3 runs; records in" in out
    assert re.search(r"^t  3 of 3 answered  wrote 2$", out, re.M)
    assert (
        len(
            re.findall(
                r"^  run [123]  run-[0-9a-f]{12}  answered  1 of 1 calls written$", out, re.M
            )
        )
        == 3
    )
    assert (
        'arguments: t: tool:get_stock: 2 sets in 3 runs: {"sku": "a"} in 2, {"sku": "b"} in 1; '
        "each has its own mock"
    ) in out
    assert re.search(r"model calls cost \$[0-9.]+ of a budget of \$1, 3 runs$", out, re.M)


@pytest.mark.design("record.runs_output")
def test_a_tests_outcome_is_its_first_run_that_did_not_answer(tmp_path: Path) -> None:
    test = loaded(tmp_path).tests[1]
    answered, crashed = result_for(test), result_for(test)
    runs = [
        dataclasses.replace(answered, run="run-a"),
        dataclasses.replace(crashed, run="run-b", outcome="crashed"),
        dataclasses.replace(answered, run="run-c", outcome="timed out"),
    ]
    found = recording.TestRecord(test, runs, asked=3)
    shown = recording.test_json(found)
    assert (shown["run"], shown["outcome"]) == ("run-b", "crashed")
    assert recording.TestRecord(test, runs[:1]).result.run == "run-a"


# Parallel.


@pytest.mark.design("record.runs_parallel")
def test_runs_follow_the_files_parallel_setting(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    fifo = team.root / "meet"
    os.mkfifo(fifo)
    team.project.monkeypatch.setenv("MEET", str(fifo))
    team.project.monkeypatch.setenv("ALIVE", str(team.root / "alive.json"))
    many(
        team,
        fake_openai,
        STOCK
        + """
def alive(change):
    with open(os.environ["ALIVE"], "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        now, top = json.loads(f.read() or "[0, 0]")
        now += change
        f.seek(0)
        f.truncate()
        f.write(json.dumps([now, max(top, now)]))

def answer(q):
    ask_model()
    n = run_index()
    alive(1)
    if n == 0:  # waits for run 1 to be alive too: they overlap only if two run at once
        open(os.environ["MEET"], "rb").read(1)
    elif n == 1:
        with open(os.environ["MEET"], "wb") as f:
            f.write(b"x")
    get_stock(f"r{n}")
    alive(-1)
    return "ok"
""",
        ["bot:get_stock"],
        ("tool:get_stock",),
        extra="parallel = 2\n",
    )
    done = team.record("--runs", "4")
    assert done.code == 0, done.out
    assert json.loads((team.root / "alive.json").read_text())[1] == 2


@pytest.mark.design("record.runs_parallel")
def test_parallel_runs_write_the_same_file_as_runs_in_order(
    team: Team, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    code = answering("get_stock('a')", "get_stock('b')")
    texts: list[str] = []
    for parallel in (1, 4):
        many(
            team,
            fake_openai,
            code,
            ["bot:get_stock"],
            ("tool:get_stock",),
            extra=f"parallel = {parallel}\n",
        )
        done = team.record("--runs", "4")
        assert done.code == 0, done.out
        mocks = team.out_path.read_text().split("[[test.mock]]", 1)[1]
        texts.append("\n".join(x for x in mocks.splitlines() if not x.startswith("#")))
    assert texts[0] == texts[1] and texts[0].count("[[test.mock]]") == 1  # two mocks, a and b


@pytest.mark.design("record.runs_merge")
def test_inside_one_run_a_saved_and_a_listed_call_on_one_web_key_keep_todays_rules(
    team: Team,
) -> None:
    served = iter(range(100))

    def reply(query: str, body: bytes) -> Route:
        del query, body
        if next(served) == 0:
            return 200, {"content-type": "application/json"}, b'{"ok": true}'
        return 503, {"content-type": "application/json"}, b'{"error": "down"}'

    team.service.dynamic["/x"] = reply
    team.write(
        """
def fetch(k):
    http_get("/x")
    http_get("/x")
    return {"fine": True}

def answer(q):
    fetch("x")
    return "ok"
""",
        ["bot:fetch"],
        ("tool:fetch",),
    )
    done = team.record()
    assert [m["status"] for m in done.mocks() if m["call"] == "http:localhost"] == [200]


@pytest.mark.design("record.carried_never_run")
def test_a_carried_fault_is_never_run_by_record(team: Team) -> None:
    """record runs baselines only, so an append, a replace or a slow in the team's file never
    runs, and the mock it writes holds the real value, not a changed one."""
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
            ONE_TEST
            + '[[test.fault]]\ncall = "tool:get_order"\ndo = "append"\ntext = "INJECTED"\n'
            + 'forbid = ["x"]\n'
            + '[[test.fault]]\ncall = "tool:get_order"\ndo = "replace"\nfind = "shipped"\n'
            + 'text = "INJECTED"\n'
            + '[[test.fault]]\ncall = "tool:get_order"\ndo = "slow 30s"\nforbid = ["x"]\n'
        ),
    )
    done = team.record()
    assert done.code == 0, done.out
    lines = lines_of(done)
    calls = [x for x in lines if "call" in x]
    assert [(x["action"], "fault" in x, "under" in x, "changed" in x) for x in calls] == [
        ("real", False, False, False)
    ]
    assert all(x.get("row", "baseline") == "baseline" for x in lines)
    written = tomllib.loads(team.out_path.read_text())["test"][0]["mock"]
    assert [m["returns"] for m in written] == [{"order_id": "1", "status": "shipped"}]
