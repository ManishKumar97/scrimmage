"""MCP tools, through the real command, against real servers on both transports.

The server in fakes/docs_server.py is served by the official library. It writes every
tool call it really receives to a log, so "the server never saw it" is a fact, not a hope.
"""

import asyncio
import contextlib
import dataclasses
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
import types
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import jsonschema
import mcp.client.session as mcp_session
import mcp.client.streamable_http as mcp_http
import pytest

import fakes
from conftest import Project, Result
from derail_sdk import config, decide, labels, mcp_gate, plan, state
from derail_sdk.config import Fault, Mock
from derail_sdk.errors import ConfigError, DerailRefused
from derail_sdk.http_gate import MCP_TRANSPORT
from derail_sdk.plan import Plan
from fakes.openai_server import FakeOpenAI

SERVER = Path(fakes.__file__).parent / "docs_server.py"
LABEL = "mcp:docs/search"
READY = re.compile(r"running on (http://127\.0\.0\.1:\d+)")
BOTH = pytest.mark.parametrize("transport", ["stdio", "http"])
MOCK = f'[[test.mock]]\ncall = "{LABEL}"\nreturns = {{hits = ["mocked"], total = 1}}\n'
ERROR = f'[[test.fault]]\ncall = "{LABEL}"\ndo = "error"\n'
EMPTY = f'[[test.fault]]\ncall = "{LABEL}"\ndo = "empty"\n'
TIMEOUT = f'[[test.fault]]\ncall = "{LABEL}"\ndo = "timeout"\n'


def toml(function: str, body: str, extra: str = "") -> str:
    return (
        f'[agent]\nfunction = "{function}"\nruns = 1\nmax_cost_usd = "none"\n{extra}\n'
        '[[test]]\nname = "docs"\nquestion = "Find refunds"\nexpect = ["x"]\n' + body
    )


@dataclass
class Docs:
    """One test's documents server: its log, and the way to start it."""

    project: Project
    log: Path
    started: list[subprocess.Popen[bytes]] = field(default_factory=list[subprocess.Popen[bytes]])
    runs: int = 0

    def start(self, transport: str, mode: str = "ok", name: str = "docs") -> None:
        """A stdio server is started by the bot; a web one runs on localhost until the end."""
        self.project.monkeypatch.setenv("DOCS_MODE", mode)
        self.project.monkeypatch.setenv("DOCS_TRANSPORT", transport)
        if transport == "stdio":
            return
        # Port 0: the system picks a free one, and the server's own log line says which.
        said = self.log.with_suffix(f".{len(self.started)}.err")
        command = [sys.executable, str(SERVER), transport, name, str(self.log), mode, "0"]
        with said.open("wb") as errors:
            self.started.append(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors))
        deadline = time.monotonic() + 30
        while (ready := READY.search(said.read_text())) is None:
            assert time.monotonic() < deadline, said.read_text()
            time.sleep(0.02)
        path = "sse" if transport == "sse" else "mcp"
        self.project.monkeypatch.setenv("DOCS_URL", f"{ready.group(1)}/{path}")

    def stop(self) -> None:
        for server in self.started:
            server.kill()  # a slow tool holds the server, and nothing here needs it to close well
            server.wait(timeout=10)
        self.started.clear()

    def received(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(self, function: str, body: str, extra: str = "") -> Result:
        """One derail test. Its records move aside, so a Result reads only its own."""
        self.project.toml.write_text(toml(function, body, extra))
        result = self.project.test()
        self.runs += 1
        kept = self.log.parent / f"records-{self.runs}"
        kept.mkdir()
        shutil.move(self.project.root / "derail-runs", kept / "derail-runs")
        return dataclasses.replace(result, root=kept)


@pytest.fixture
def docs(project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Docs]:
    log = tmp_path / "received.log"
    monkeypatch.setenv("DOCS_SERVER", str(SERVER))
    monkeypatch.setenv("DOCS_LOG", str(log))
    server = Docs(project, log)
    yield server
    server.stop()


def fits_the_schema(result: Result) -> None:
    schema = (Path(plan.__file__).parent / "schemas" / "record.v1.json").read_text()
    validator = jsonschema.Draft202012Validator(json.loads(schema))
    for lines in result.records():
        for line in lines:
            validator.validate(line)


def row(result: Result, name: str) -> list[dict[str, Any]]:
    return next(lines for lines in result.records() if lines[-1]["row"] == name)  # type: ignore[misc]


def answer_of(result: Result, name: str = "baseline") -> str:
    return str(row(result, name)[-1]["summary"]["answer"])


def calls(result: Result, name: str = "baseline") -> list[tuple[str, str]]:
    return [(x["call"], x["action"]) for x in row(result, name)[:-1] if "call" in x]


# 5.1 The gate and mocks.


@pytest.mark.design("mcp.use_case")
@BOTH
def test_the_server_never_sees_a_mocked_or_faulted_call(docs: Docs, transport: str) -> None:
    docs.start(transport)
    result = docs.run("acme.docs_bot:answer", MOCK + ERROR + EMPTY + TIMEOUT)
    assert docs.received() == []
    assert "structured={'hits': ['mocked'], 'total': 1}" in answer_of(result), result.out
    assert "error=False" in answer_of(result)
    assert calls(result) == [(LABEL, "mock")]
    assert "error=True" in answer_of(result, "fault 1")
    assert calls(result, "fault 1") == [(LABEL, "fault")]
    assert row(result, "fault 1")[0]["fault"]["do"] == "error"
    assert "has an output schema" in answer_of(result, "fault 2")
    assert answer_of(result, "fault 3").startswith("MCPError code=-32001")
    fits_the_schema(result)


@pytest.mark.design("mcp.g1_no_web_decision")
def test_over_http_no_mcp_message_reaches_the_web_gate(docs: Docs) -> None:
    docs.start("http")
    result = docs.run("acme.docs_bot:answer", MOCK, 'real = ["mcp:docs/note"]')
    assert result.code == 0, result.out
    # The transport's POSTs, its GET stream and its DELETE made no line and no refusal.
    assert calls(result) == [(LABEL, "mock")]
    assert docs.received() == []


@pytest.mark.design("mcp.g1_no_web_decision")
def test_a_real_call_over_http_also_stays_off_the_web_gate(docs: Docs) -> None:
    docs.start("http")
    result = docs.run("acme.docs_bot:answer", "", f'real = ["{LABEL}"]')
    assert result.code == 0, result.out
    assert calls(result) == [(LABEL, "real")]
    assert docs.received() == ["search refunds"]
    assert "real refunds" in answer_of(result)


@pytest.mark.design("mcp.mock_wrong_shape")
@BOTH
def test_a_mock_of_the_wrong_shape_fails_as_a_real_reply_does(docs: Docs, transport: str) -> None:
    docs.start(transport, "wrong")
    function = "acme.docs_bot:answer"
    real = docs.run(function, "", f'real = ["{LABEL}"]')
    assert docs.received() == ["search refunds"]
    docs.log.unlink()
    shape = f'[[test.mock]]\ncall = "{LABEL}"\nreturns = {{hits = "not a list"}}\n'
    mocked = docs.run(function, shape)
    assert docs.received() == []
    assert answer_of(real).startswith("RuntimeError Invalid structured content")
    assert answer_of(mocked) == answer_of(real)


@pytest.mark.design("mcp.mock_wrong_shape")
@BOTH
def test_the_shape_is_checked_for_a_tool_the_bot_never_listed(docs: Docs, transport: str) -> None:
    docs.start(transport)
    shape = f'[[test.mock]]\ncall = "{LABEL}"\nreturns = {{hits = "not a list"}}\n'
    result = docs.run("acme.docs_bot:answer_not_listed", shape)
    assert answer_of(result).startswith("RuntimeError Invalid structured content")
    assert docs.received() == []


@pytest.mark.design("mcp.mock_text")
def test_a_mock_is_text_or_json_and_fills_the_structured_result_only_for_a_shape(
    docs: Docs,
) -> None:
    docs.start("stdio")
    docs.project.monkeypatch.setenv("DOCS_TOOL", "note")
    text = '[[test.mock]]\ncall = "mcp:docs/note"\nreturns = "mocked note"\n'
    result = docs.run("acme.docs_bot:answer", text)
    assert answer_of(result) == "error=False blocks=1 text='mocked note' structured=None"
    table = '[[test.mock]]\ncall = "mcp:docs/note"\nreturns = {a = 1}\n'
    result = docs.run("acme.docs_bot:answer", table)
    assert answer_of(result) == """error=False blocks=1 text='{\\n  "a": 1\\n}' structured=None"""


@pytest.mark.design("mcp.mock_text")
def test_a_text_mock_on_a_tool_with_a_shape_fails_the_librarys_check(docs: Docs) -> None:
    docs.start("stdio")
    text = f'[[test.mock]]\ncall = "{LABEL}"\nreturns = "just text"\n'
    result = docs.run("acme.docs_bot:answer", text)
    assert [s["outcome"] for s in result.summaries()] == ["crashed"]
    assert docs.received() == []


@pytest.mark.design("mcp.mock_text")
def test_a_wrapped_shape_takes_the_field_the_server_sends(docs: Docs) -> None:
    docs.start("stdio")
    docs.project.monkeypatch.setenv("DOCS_TOOL", "title")
    mock = '[[test.mock]]\ncall = "mcp:docs/title"\nreturns = {result = "mocked"}\n'
    result = docs.run("acme.docs_bot:answer", mock)
    assert "structured={'result': 'mocked'}" in answer_of(result)
    assert "error=False" in answer_of(result)


@pytest.mark.design("mcp.mock_text")
@BOTH
def test_a_mocks_text_is_written_as_a_real_server_writes_it(docs: Docs, transport: str) -> None:
    docs.start(transport, "accent")
    real = docs.run("acme.docs_bot:answer", "", f'real = ["{LABEL}"]')
    mock = f'[[test.mock]]\ncall = "{LABEL}"\nreturns = {{hits = ["réal refunds"], total = 1}}\n'
    mocked = docs.run("acme.docs_bot:answer", mock)
    assert "réal" in answer_of(real)
    assert answer_of(mocked) == answer_of(real)


@pytest.mark.design("mcp.short_label")
@pytest.mark.parametrize("label", ["mcp:search", LABEL])
def test_a_tool_can_be_named_with_or_without_its_server(docs: Docs, label: str) -> None:
    docs.start("stdio")
    mock = f'[[test.mock]]\ncall = "{label}"\nreturns = {{hits = [], total = 0}}\n'
    result = docs.run("acme.docs_bot:answer", mock)
    assert calls(result) == [(LABEL, "mock")]
    assert docs.received() == []


@pytest.mark.design("mcp.real_listed")
@BOTH
@pytest.mark.parametrize("label", ["mcp:search", LABEL])
def test_a_listed_tool_runs_for_real(docs: Docs, transport: str, label: str) -> None:
    docs.start(transport)
    result = docs.run("acme.docs_bot:answer", "", f'real = ["{label}"]')
    assert result.code == 0, result.out
    assert docs.received() == ["search refunds"]
    assert "'hits': ['real refunds']" in answer_of(result)


@pytest.mark.design("mcp.refused")
@BOTH
def test_an_unlisted_tool_is_refused_and_never_reaches_the_server(
    docs: Docs, transport: str
) -> None:
    docs.start(transport)
    docs.project.monkeypatch.setenv("DOCS_TOOL", "delete_doc")
    result = docs.run("acme.docs_bot:answer", "")
    assert result.code == 2, result.out
    assert docs.received() == []
    assert [s["outcome"] for s in result.summaries()] == ["refused call"]
    line = result.records()[0][0]
    assert (line["call"], line["action"], line["reason"]) == (
        "mcp:docs/delete_doc",
        "refused",
        "unlisted",
    )
    assert 'add "mcp:docs/delete_doc" to real' in result.out
    fits_the_schema(result)


@pytest.mark.design("mcp.read_only_hint")
def test_a_read_only_hint_is_quoted_and_never_trusted(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer", "")
    assert result.code == 2
    assert docs.received() == []
    assert "marks this tool as read-only, but that is only a hint" in result.out


@pytest.mark.design("mcp.refusal_poisons")
def test_a_swallowed_refusal_stops_every_later_tool_call(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_swallows_refusal", MOCK)
    assert docs.received() == []
    line = row(result, "baseline")
    assert [(x["action"], x.get("reason")) for x in line if "action" in x] == [
        ("refused", "unlisted"),
        ("refused", "poisoned"),
    ]


@pytest.mark.design("mcp.reads_recorded")
def test_a_request_a_server_could_act_on_passes_and_leaves_a_line(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_with_reads", "")
    assert result.code == 0, result.out
    assert docs.received() == ["resources/read handbook", "prompts/get refunds"]
    assert calls(result) == [
        ("mcp:docs/resources/read", "real"),
        ("mcp:docs/prompts/get", "real"),
        ("mcp:docs/completion/complete", "real"),
        ("mcp:docs/resources/subscribe", "real"),
    ]
    lines = row(result, "baseline")
    assert "docs://handbook" in lines[0]["args"]["preview"]
    assert "the handbook" in lines[0]["result"]["preview"]
    fits_the_schema(result)


@pytest.mark.design("mcp.reads_recorded")
def test_a_recorded_read_is_a_kept_line_and_is_not_decided() -> None:
    sink = Sink()
    run = state.RunState(a_plan(), sink, [])
    run.finish(run.passed(mcp_gate.call_for("docs", "resources/read")))
    assert sink.lines[0]["keep"] is True
    assert run.refused is None


@pytest.mark.design("mcp.unknown_method")
@BOTH
def test_a_request_this_release_does_not_know_is_gated_not_passed(
    docs: Docs, transport: str
) -> None:
    docs.start(transport)
    result = docs.run("acme.docs_bot:answer_unknown_request", "")
    assert result.code == 2, result.out
    assert calls(result) == [("mcp:docs/tasks/run", "refused")]
    assert [s["outcome"] for s in result.summaries()] == ["refused call"]


@pytest.mark.design("mcp.unknown_method")
def test_a_listed_unknown_request_is_sent(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_unknown_request", "", 'real = ["mcp:docs/tasks/run"]')
    # The server does not know it, which is its own answer: the request really went out.
    assert calls(result) == [("mcp:docs/tasks/run", "real")]
    assert row(result, "baseline")[0]["error"].startswith("MCPError")


@pytest.mark.design("mcp.real_line_kept")
def test_a_real_line_is_kept_when_the_queue_is_full_and_a_mock_line_is_not() -> None:
    sink = Sink()
    run = state.RunState(a_plan(real=(LABEL,)), sink, [])
    run.finish(run.decide(mcp_gate.call_for("docs", "search"), {}))
    assert sink.lines[0]["action"] == "real"
    assert sink.lines[0]["keep"] is True
    mocked = state.RunState(a_plan(mocks=(Mock(LABEL, "x", None),)), sink, [])
    mocked.finish(mocked.decide(mcp_gate.call_for("docs", "search"), {}))
    assert sink.lines[1]["action"] == "mock"
    assert sink.lines[1]["keep"] is False


# 5.2 Faults and checks.


REAL_FAILURES = [("error", "fail", ERROR), ("empty", "empty", EMPTY)]


@pytest.mark.design("mcp.real_against_injected_error")
@BOTH
@pytest.mark.parametrize(("name", "mode", "fault"), REAL_FAILURES)
def test_a_real_failure_and_the_injected_one_look_the_same(
    docs: Docs, transport: str, name: str, mode: str, fault: str
) -> None:
    function = "acme.docs_bot:answer"
    docs.start(transport, mode)
    real = docs.run(function, "", f'real = ["{LABEL}"]')
    assert docs.received() == ["search refunds"]
    docs.stop()
    docs.log.unlink()
    docs.start(transport, "ok")
    injected = docs.run(function, MOCK + fault)
    assert docs.received() == []
    assert answer_of(injected, "fault 1") == answer_of(real), name


@pytest.mark.design("mcp.real_against_injected_empty")
@BOTH
def test_a_real_empty_reply_and_the_injected_one_on_a_tool_with_no_shape(
    docs: Docs, transport: str
) -> None:
    function = "acme.docs_bot:answer"
    docs.project.monkeypatch.setenv("DOCS_TOOL", "note")
    docs.start(transport, "empty")
    real = docs.run(function, "", 'real = ["mcp:docs/note"]')
    docs.stop()
    docs.start(transport, "ok")
    fault = '[[test.fault]]\ncall = "mcp:docs/note"\ndo = "empty"\n'
    mock = '[[test.mock]]\ncall = "mcp:docs/note"\nreturns = "x"\n'
    injected = docs.run(function, mock + fault)
    assert answer_of(real) == "error=False blocks=0 text='' structured=None"
    assert answer_of(injected, "fault 1") == answer_of(real)


@pytest.mark.design("mcp.real_against_injected_timeout")
@BOTH
def test_a_real_timeout_and_the_injected_one_look_the_same(
    docs: Docs, transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    function = "acme.docs_bot:answer"
    monkeypatch.setenv("DOCS_TIMEOUT", "1")
    docs.start(transport, "slow")
    real = docs.run(function, "", f'real = ["{LABEL}"]')
    docs.stop()
    docs.start(transport, "ok")
    injected = docs.run(function, MOCK + TIMEOUT)
    assert answer_of(real).startswith("MCPError code=-32001")
    assert answer_of(injected, "fault 1") == answer_of(real)


@pytest.mark.design("mcp.fault_first_n")
def test_a_fault_counts_calls_and_a_mock_answers_the_rest(docs: Docs) -> None:
    docs.start("stdio")
    fault = f'[[test.fault]]\ncall = "{LABEL}"\ndo = "error"\nwhen = "first 1"\n'
    result = docs.run("acme.docs_bot:answer_twice", MOCK + fault)
    assert calls(result, "fault 1") == [(LABEL, "fault"), (LABEL, "mock")]
    assert row(result, "fault 1")[0]["fault"] == {"do": "error", "when": "first 1", "n": 1}
    assert docs.received() == []


@pytest.mark.design("mcp.ambiguous_label")
def test_a_label_two_servers_match_stops_the_test(docs: Docs) -> None:
    docs.start("stdio")
    mock = '[[test.mock]]\ncall = "mcp:search"\nreturns = {hits = [], total = 0}\n'
    later = '[[test]]\nname = "later"\nquestion = "x"\nexpect = ["x"]\n'
    body = mock + ERROR.replace(LABEL, "mcp:search") + later + MOCK
    result = docs.run("acme.docs_bot:answer_two_servers", body)
    assert result.code == 2, result.out
    assert docs.received() == []
    assert "matches a tool on more than one server" in result.out
    assert "mcp:docs/search or mcp:wiki/search" in result.out
    # The test stopped: neither the fault row nor the later test ran.
    assert len(result.records()) == 1
    assert result.records()[0][0]["reason"] == "ambiguous_label"
    fits_the_schema(result)


@pytest.mark.design("mcp.ambiguous_label")
def test_naming_the_server_settles_it(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_two_servers", MOCK)
    assert result.code == 0, result.out
    assert calls(result) == [(LABEL, "mock")]


@pytest.mark.design("mcp.ambiguous_label")
def test_a_real_label_two_servers_match_stops_the_test_too(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_two_servers", "", 'real = ["mcp:search"]')
    assert result.code == 2
    assert docs.received() == []


@pytest.mark.design("mcp.slash_in_a_tool")
def test_a_tool_with_a_slash_is_not_taken_for_a_server_and_a_tool(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_slashed_tool", "", f'real = ["{LABEL}"]')
    assert result.code == 2, result.out
    assert docs.received() == []
    assert calls(result) == [("mcp:wiki/docs/search", "refused")]
    listed = docs.run("acme.docs_bot:answer_slashed_tool", "", 'real = ["mcp:wiki/docs/search"]')
    assert listed.code == 0, listed.out
    assert docs.received() == ["docs/search refunds"]


@pytest.mark.design("mcp.slash_in_a_tool")
def test_a_tool_with_a_slash_has_no_short_label() -> None:
    assert mcp_gate.call_for("wiki", "docs/search").short is None
    assert mcp_gate.call_for("wiki", "search").short == "mcp:search"


@pytest.mark.design("mcp.label_read_two_ways")
@pytest.mark.parametrize("kind", ["real", "mock"])
def test_a_label_that_reads_two_ways_stops_the_test(docs: Docs, kind: str) -> None:
    docs.start("stdio")
    label = "mcp:a/b/c"
    mock = f'[[test.mock]]\ncall = "{label}"\nreturns = "x"\n'
    body, extra = ("", f'real = ["{label}"]') if kind == "real" else (mock, "")
    result = docs.run("acme.docs_bot:answer_two_readings", body, extra)
    assert result.code == 2, result.out
    assert docs.received() == []
    assert "could mean any of the tools b/c, c" in result.out
    assert result.records()[0][0]["reason"] == "ambiguous_label"


@pytest.mark.design("mcp.unused_mock")
def test_a_mock_no_call_used_is_a_test_mistake(docs: Docs) -> None:
    docs.start("stdio")
    typo = '[[test.mock]]\ncall = "mcp:docs/serach"\nreturns = "x"\n'
    result = docs.run("acme.docs_bot:answer", MOCK + typo)
    assert result.code == 2, result.out
    assert "no call matched the mock on mcp:docs/serach" in result.out


@pytest.mark.design("mcp.never_fired")
def test_a_fault_no_call_met_is_a_test_mistake(docs: Docs) -> None:
    docs.start("stdio")
    fault = '[[test.fault]]\ncall = "mcp:docs/other"\ndo = "error"\n'
    result = docs.run("acme.docs_bot:answer", MOCK + fault)
    assert result.code == 2, result.out
    assert "the fault never fired" in result.out


@pytest.mark.design("mcp.two_levels_mixed")
def test_a_tool_mock_and_an_mcp_fault_is_named_as_two_levels(docs: Docs) -> None:
    docs.start("stdio")
    body = '[[test.mock]]\ncall = "tool:find_docs"\nreturns = "x"\n' + ERROR
    tools = 'tools = ["acme.docs_bot:find_docs"]'
    result = docs.run("acme.docs_bot:answer_through_a_tool", body, tools)
    assert result.code == 2, result.out
    assert "mock on tool:find_docs replaces the whole tool" in result.out


@pytest.mark.design("mcp.server_inherits_the_run")
def test_a_stdio_server_that_inherits_the_run_still_serves(docs: Docs) -> None:
    docs.start("stdio")
    function = "acme.docs_bot:answer_with_the_runs_environment"
    result = docs.run(function, "", f'real = ["{LABEL}"]')
    assert result.code == 0, result.out
    assert docs.received() == ["search refunds"]


# The record.


@pytest.mark.design("mcp.record_redacted")
def test_arguments_and_results_are_recorded_without_secrets(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_keeps_a_secret", "", f'real = ["{LABEL}"]')
    assert result.code == 0, result.out
    assert docs.received() == [f"search token sk-{'a' * 40}"]  # the server got the real value
    line = row(result, "baseline")[0]
    assert "aaaaaaaa" not in json.dumps(line)
    assert "[secret removed]" in line["args"]["preview"]
    folder = next((result.root / "derail-runs").glob("*/blobs"), None)
    files = list(folder.iterdir()) if folder else []
    assert files, "the side folder holds the full arguments and result"
    assert all(b"aaaaaaaa" not in f.read_bytes() for f in files)


@pytest.mark.design("mcp.real_error_recorded")
def test_a_real_protocol_error_is_on_the_line_and_reaches_the_agent(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCS_TIMEOUT", "1")
    docs.start("stdio", "slow")
    result = docs.run("acme.docs_bot:answer", "", f'real = ["{LABEL}"]')
    line = row(result, "baseline")[0]
    assert line["action"] == "real"
    assert line["error"].startswith("MCPError")
    assert result.code == 0  # a real tool failure is the agent's world, not infrastructure


@pytest.mark.design("mcp.record_redacted")
def test_a_label_a_server_made_up_is_recorded_without_secrets() -> None:
    sink = Sink()
    run = state.RunState(a_plan(), sink, [])
    gate = mcp_gate.Gate(run, None, stopper([]))
    with pytest.raises(DerailRefused):
        mcp_gate.decided(gate, run, mcp_gate.call_for("docs", "sk-" + "a" * 40), {})
    assert "aaaaaaaa" not in str(sink.lines[0])


# The mark.


@pytest.mark.design("mcp.mark_does_not_leak")
def test_the_agents_own_write_to_the_servers_host_is_still_refused(docs: Docs) -> None:
    docs.start("http")
    result = docs.run("acme.docs_bot:answer_web_after_mcp", MOCK)
    assert result.code == 2, result.out
    assert calls(result)[-1] == ("http:127.0.0.1", "refused")
    refused = [x for x in row(result, "baseline") if x.get("action") == "refused"]
    assert refused[0]["reason"] == "web_write"


@pytest.mark.design("mcp.mark_does_not_leak")
def test_a_write_after_the_connection_closed_is_still_refused(docs: Docs) -> None:
    docs.start("http")
    result = docs.run("acme.docs_bot:answer_web_after_close", "")
    assert result.code == 2, result.out
    assert calls(result) == [("http:127.0.0.1", "refused")]


def other_port(docs: Docs) -> str:
    """The MCP server's host on a port nothing listens on. A sent POST would fail, not pass."""
    return re.sub(r":\d+/.*", ":9/x", os.environ["DOCS_URL"])


def other_path(docs: Docs) -> str:
    return re.sub(r"/mcp$", "/orders", os.environ["DOCS_URL"])


def the_endpoint(docs: Docs) -> str:
    return os.environ["DOCS_URL"]


TARGETS = [other_port, other_path]


def refused_web_write(result: Result) -> bool:
    return any(
        x.get("kind") == "http" and x.get("reason") == "web_write"
        for lines in result.records()
        for x in lines
    )


@pytest.mark.design("mcp.mark_is_the_endpoint")
@pytest.mark.parametrize("target", TARGETS)
def test_a_post_from_an_event_hook_is_a_web_write(
    docs: Docs, target: Callable[[Docs], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    docs.start("http")
    monkeypatch.setenv("DOCS_TARGET", target(docs))
    result = docs.run("acme.docs_bot:answer_web_in_a_hook", "")
    assert refused_web_write(result), result.out


@pytest.mark.design("mcp.mark_is_the_endpoint")
def test_a_post_to_another_host_from_an_event_hook_is_a_web_write(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs.start("http")
    monkeypatch.setenv("DOCS_TARGET", "https://payments.acme.internal/refunds")
    result = docs.run("acme.docs_bot:answer_web_in_a_hook", "")
    assert ("http:payments.acme.internal", "refused") in calls(result), result.out


@pytest.mark.design("mcp.mark_ends_with_the_send")
@pytest.mark.parametrize("target", [*TARGETS, the_endpoint])
@pytest.mark.parametrize("callback", ["logging", "progress", "sampling"])
def test_a_post_from_a_callback_is_a_web_write(
    docs: Docs, callback: str, target: Callable[[Docs], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    docs.start("http")
    monkeypatch.setenv("DOCS_TARGET", target(docs))
    monkeypatch.setenv("DOCS_CALLBACK", callback)
    result = docs.run("acme.docs_bot:answer_from_a_callback", "", 'real = ["mcp:docs/chatty"]')
    assert refused_web_write(result), result.out


@pytest.mark.design("mcp.mark_ends_with_the_send")
def test_a_model_call_in_a_callback_gets_its_line_and_its_budget(
    docs: Docs, fake_openai: Callable[..., FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    docs.start("http")
    monkeypatch.setenv("DOCS_CALLBACK", "model")
    fake = fake_openai(["acme_answer"])
    extra = 'real = ["mcp:docs/chatty"]\nmodel_hosts = [{host = "127.0.0.1", provider = "openai"}]'
    docs.project.toml.write_text(
        toml("acme.docs_bot:answer_from_a_callback", "", extra).replace(
            'max_cost_usd = "none"', "max_cost_usd = 1"
        )
    )
    result = docs.project.test(fake)
    assert result.code == 0, result.out
    assert len(fake.requests) == 1
    line = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert float(str(line["cost_usd"])) > 0
    assert result.summaries()[0]["cost_usd"] == line["cost_usd"]


@pytest.mark.design("mcp.token_request")
def test_a_token_post_on_the_servers_host_is_refused_unless_listed(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs.start("http")
    monkeypatch.setenv("DOCS_TARGET", other_path(docs))
    result = docs.run("acme.docs_bot:answer_with_a_token", MOCK)
    assert refused_web_write(result), result.out
    assert 'add "http:127.0.0.1 POST /orders" to real' in result.out
    listed = docs.run(
        "acme.docs_bot:answer_with_a_token", MOCK, 'real = ["http:127.0.0.1 POST /orders"]'
    )
    assert listed.code == 0, listed.out
    assert ("http:127.0.0.1", "real") in calls(listed)
    assert (LABEL, "mock") in calls(listed)


@pytest.mark.design("mcp.redirect_fails_closed")
def test_a_redirect_to_another_path_is_a_web_write(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs.start("http")
    monkeypatch.setenv("DOCS_URL", os.environ["DOCS_URL"] + "/")
    result = docs.run("acme.docs_bot:answer", MOCK)
    assert refused_web_write(result), result.out
    assert docs.received() == []


# Both protocol eras.


@pytest.mark.design("mcp.newer_era")
def test_the_newer_protocol_is_mocked_faulted_and_refused_the_same(docs: Docs) -> None:
    docs.start("http")
    result = docs.run("acme.docs_bot:answer_newer_protocol", MOCK + ERROR)
    assert answer_of(result).startswith("2026-07-28 error=False")
    assert "'hits': ['mocked']" in answer_of(result)
    assert "2026-07-28 error=True" in answer_of(result, "fault 1")
    assert docs.received() == []
    assert result.code == 0, result.out
    assert calls(result) == [(LABEL, "mock")]
    refused = docs.run("acme.docs_bot:answer_newer_protocol", "")
    assert refused.code == 2
    assert docs.received() == []


# The old transport.


@pytest.mark.design("mcp.old_sse_refused")
def test_the_old_sse_transport_fails_closed_at_the_web_gate(docs: Docs) -> None:
    docs.start("sse")
    result = docs.run("acme.docs_bot:answer_sse", MOCK)
    assert result.code == 2, result.out
    assert docs.received() == []
    assert ("http:127.0.0.1", "refused") in calls(result)
    assert [s["outcome"] for s in result.summaries()] == ["refused call"]


# A model that is offered the server's tools.


@pytest.mark.design("mcp.seen_by_the_unseen_check")
def test_a_tool_the_server_lists_can_be_offered_to_the_model(
    docs: Docs, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    docs.start("stdio")
    fake = fake_openai(["docs_search", "acme_answer"])
    hosts = 'model_hosts = [{host = "127.0.0.1", provider = "openai"}]'
    docs.project.toml.write_text(
        toml("acme.docs_bot:answer_model", MOCK + ERROR, hosts).replace(
            'max_cost_usd = "none"', "max_cost_usd = 1"
        )
    )
    result = docs.project.test(fake)
    assert result.code == 0, result.out
    assert docs.received() == []
    told = [
        str(m["content"])
        for request in fake.requests
        for m in request["messages"]
        if m.get("role") == "tool"
    ]
    assert any("mocked" in text for text in told)
    assert any(text.startswith("tool failed: Error executing tool search") for text in told)


# A framework's tool around an MCP call.


@pytest.mark.design("mcp.tool_gate_first")
def test_a_langchain_tool_meets_its_own_gate_first(docs: Docs) -> None:
    docs.start("stdio")
    function = "acme.docs_bot:answer_langchain"
    refused = docs.run(function, MOCK)
    assert calls(refused) == [("tool:lookup", "refused")]
    mock = '[[test.mock]]\ncall = "tool:lookup"\nreturns = "mocked tool"\n'
    mocked = docs.run(function, mock)
    assert calls(mocked) == [("tool:lookup", "mock")]
    listed = docs.run(function, MOCK, 'real = ["tool:lookup"]')
    assert listed.code == 0, listed.out
    assert calls(listed) == [(LABEL, "mock"), ("tool:lookup", "real")]
    assert docs.received() == []


@pytest.mark.design("mcp.mock_on_a_closed_session")
def test_a_mock_on_a_closed_session_still_answers(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_after_close", MOCK)
    assert "'hits': ['mocked']" in answer_of(result), result.out


# 5.3 FastMCP.


@pytest.mark.design("mcp.fastmcp_mock")
@BOTH
def test_fastmcps_client_is_mocked_and_faulted(docs: Docs, transport: str) -> None:
    docs.start(transport)
    function = "acme.docs_bot:answer_fastmcp"
    result = docs.run(function, MOCK + ERROR + TIMEOUT)
    assert result.code == 0, result.out
    assert docs.received() == []
    assert calls(result) == [(LABEL, "mock")]
    assert "hits=['mocked']" in answer_of(result)
    assert answer_of(result, "fault 1").startswith("ToolError Error executing tool search")
    assert answer_of(result, "fault 2").startswith("MCPError code=-32001")
    fits_the_schema(result)


@pytest.mark.design("mcp.fastmcp_wrong_shape")
@BOTH
def test_fastmcp_a_wrong_shape_fails_as_a_real_reply_does(docs: Docs, transport: str) -> None:
    function = "acme.docs_bot:answer_fastmcp"
    docs.start(transport, "wrong")
    real = docs.run(function, "", f'real = ["{LABEL}"]')
    docs.stop()
    docs.log.unlink()
    docs.start(transport, "ok")
    shape = f'[[test.mock]]\ncall = "{LABEL}"\nreturns = {{hits = "not a list"}}\n'
    mocked = docs.run(function, shape)
    assert docs.received() == []
    assert answer_of(real).startswith("RuntimeError Invalid structured content")
    assert answer_of(mocked) == answer_of(real)


@pytest.mark.design("mcp.fastmcp_real_against_injected")
@BOTH
def test_fastmcp_a_real_failure_and_the_injected_one_look_the_same(
    docs: Docs, transport: str
) -> None:
    function = "acme.docs_bot:answer_fastmcp"
    docs.start(transport, "fail")
    real = docs.run(function, "", f'real = ["{LABEL}"]')
    docs.stop()
    docs.start(transport, "ok")
    injected = docs.run(function, MOCK + ERROR)
    assert answer_of(real) == "ToolError Error executing tool search"
    assert answer_of(injected, "fault 1") == answer_of(real)


@pytest.mark.design("mcp.fastmcp_real_against_injected")
@BOTH
def test_fastmcp_a_real_timeout_and_the_injected_one_look_the_same(
    docs: Docs, transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCS_TIMEOUT", "1")
    docs.start(transport, "slow")
    real = docs.run("acme.docs_bot:answer_fastmcp", "", f'real = ["{LABEL}"]')
    docs.stop()
    docs.start(transport, "ok")
    injected = docs.run("acme.docs_bot:answer_fastmcp", MOCK + TIMEOUT)
    assert answer_of(real).startswith("MCPError code=-32001")
    assert answer_of(injected, "fault 1") == answer_of(real)


ROUTER = "mcp:MCPRouter/docs_search"


@pytest.mark.design("mcp.fastmcp_router")
def test_fastmcps_router_fails_closed(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_fastmcp_two_servers", MOCK)
    assert result.code == 2, result.out
    assert docs.received() == []
    assert calls(result)[0] == (ROUTER, "refused")


@pytest.mark.design("mcp.fastmcp_router")
def test_fastmcps_router_can_be_mocked(docs: Docs) -> None:
    docs.start("stdio")
    mock = f'[[test.mock]]\ncall = "{ROUTER}"\nreturns = {{hits = ["mocked"], total = 1}}\n'
    result = docs.run("acme.docs_bot:answer_fastmcp_two_servers", mock)
    assert result.code == 0, result.out
    assert docs.received() == []
    assert calls(result) == [(ROUTER, "mock")]


@pytest.mark.design("mcp.fastmcp_router")
def test_a_forwarded_call_says_what_decided_it(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_fastmcp_two_servers", MOCK, f'real = ["{ROUTER}"]')
    assert result.code == 0, result.out
    assert docs.received() == []
    lines = {x["call"]: x for x in row(result, "baseline") if "call" in x}
    assert lines[LABEL]["action"] == "mock"
    assert lines[ROUTER]["forwarded_to"] == LABEL
    fits_the_schema(result)


@pytest.mark.design("mcp.fastmcp_router")
def test_a_forwarded_call_that_is_refused_stops_at_the_server_label(docs: Docs) -> None:
    docs.start("stdio")
    result = docs.run("acme.docs_bot:answer_fastmcp_two_servers", "", f'real = ["{ROUTER}"]')
    assert docs.received() == []
    assert (LABEL, "refused") in calls(result)


@pytest.mark.design("mcp.fastmcp_refused")
def test_fastmcps_client_is_refused(docs: Docs) -> None:
    docs.start("http")
    docs.project.monkeypatch.setenv("DOCS_TOOL", "delete_doc")
    result = docs.run("acme.docs_bot:answer_fastmcp", "")
    assert result.code == 2, result.out
    assert docs.received() == []
    assert [s["outcome"] for s in result.summaries()] == ["refused call"]


# A library that changed.


class Sink:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []

    def put(self, line: dict[str, object], *, keep: bool = False) -> None:
        self.lines.append({**line, "keep": keep})


def stopper(stops: list[str]) -> Callable[[str], None]:
    return stops.append


def a_plan(real: tuple[str, ...] = (), mocks: tuple[Mock, ...] = ()) -> Plan:
    return Plan("run-1", "t", "baseline", mocks, (), (), real, 60.0)


def gate_for(*stops: list[str]) -> mcp_gate.Gate:
    return mcp_gate.Gate(state.RunState(a_plan(), Sink(), []), None, stopper(stops[0]))


def library(**session: Any) -> types.ModuleType:
    """A stand-in for mcp.client.session, as a later version might look."""

    async def send_request(
        self: Any,
        request: Any,
        result_type: Any,
        request_read_timeout_seconds: Any = None,
        metadata: Any = None,
        progress_callback: Any = None,
    ) -> Any:
        del request_read_timeout_seconds, progress_callback
        return await self._dispatcher.send_raw_request(request)

    async def call_tool(self: Any) -> Any:
        return await self.send_request()

    module = types.ModuleType("mcp.client.session")
    fields = {
        "send_request": send_request,
        "call_tool": call_tool,
        "server_info": None,
        "list_tools": None,
        **session,
    }
    module.ClientSession = type("ClientSession", (), fields)  # type: ignore[attr-defined]
    module.types = types.SimpleNamespace(REQUEST_TIMEOUT=-32001)  # type: ignore[attr-defined]
    module.MCPError = RuntimeError  # type: ignore[attr-defined]
    return module


def test_the_installed_library_has_what_the_gate_relies_on() -> None:
    assert mcp_gate.gatable(mcp_session)
    assert mcp_gate.web_gatable(mcp_http)
    assert mcp_gate.version() == (2, 3)


@pytest.mark.design("mcp.structural")
def test_the_stand_in_library_passes_so_each_change_below_is_what_fails() -> None:
    assert mcp_gate.gatable(library())


async def renamed(self: Any, request: Any, result_type: Any, *args: Any) -> Any:
    return await self._dispatcher.send_raw_request(request)


async def around(
    self: Any,
    request: Any,
    result_type: Any,
    request_read_timeout_seconds: Any = None,
    metadata: Any = None,
    progress_callback: Any = None,
) -> Any:
    del request_read_timeout_seconds, progress_callback
    return await self._transport.write(request)


async def direct(self: Any) -> Any:
    return await self._dispatcher.send_raw_request("tools/call")


@pytest.mark.design("mcp.structural")
@pytest.mark.parametrize(
    "change",
    [
        {"send_request": renamed},  # its arguments changed
        {"send_request": around},  # it no longer goes through the dispatcher a mock replaces
        {"call_tool": direct},  # call_tool no longer goes through the gate
        {"send_request": None},
    ],
)
def test_a_changed_library_stops_the_program(change: dict[str, Any]) -> None:
    module = library(**change)
    assert not mcp_gate.gatable(module)
    stops: list[str] = []
    gate = gate_for(stops)
    mcp_gate.patch(module, gate)
    assert stops and "does not have the functions Derail gates" in stops[0]
    assert cast(Sink, gate.run.sink).lines[0]["notice"] == "structure"
    assert not getattr(module.ClientSession.send_request, mcp_gate.GATED, False)


@pytest.mark.design("mcp.structural")
@pytest.mark.parametrize("name", ["server_info", "list_tools"])
def test_a_library_that_renamed_what_the_gate_reads_stops_the_program(name: str) -> None:
    module = library()
    delattr(module.ClientSession, name)
    assert not mcp_gate.gatable(module)


def test_a_library_without_its_error_or_timeout_code_stops_the_program() -> None:
    module = library()
    delattr(module, "MCPError")
    assert not mcp_gate.gatable(module)
    module = library()
    module.types = types.SimpleNamespace()  # type: ignore[attr-defined]
    assert not mcp_gate.gatable(module)
    module = types.ModuleType("mcp.client.session")
    assert not mcp_gate.gatable(module)


@pytest.mark.design("mcp.structural")
def test_a_changed_transport_stops_the_program() -> None:
    stops: list[str] = []
    module = types.ModuleType("mcp.client.streamable_http")
    mcp_gate.patch_transport(module, gate_for(stops))
    assert stops


@pytest.mark.design("mcp.structural")
def test_mcp_1_stops_a_program_that_only_imports_it(docs: Docs) -> None:
    root = docs.project.root
    package = root / "mcp" / "client"
    package.mkdir(parents=True)
    (root / "mcp" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "session.py").write_text("class ClientSession:\n    pass\n")
    info = root / "mcp-1.30.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: mcp\nVersion: 1.30.0\n")
    (root / "acme" / "stale.py").write_text(
        "def answer(question):\n    import mcp.client.session\n    return 'ran'\n"
    )
    result = docs.run("acme.stale:answer", "")
    assert result.code == 3, result.out + result.err
    assert "mcp 1.30 does not have the functions Derail gates" in result.out
    assert "Derail supports mcp 2.x, tested with 2.3" in result.out
    assert "even if it never calls an MCP tool" in result.out
    assert "Install mcp 2.x in the test environment" in result.out


def test_a_newer_version_warns_and_a_tested_one_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    module = library()
    gate = gate_for([])
    sink = cast(Sink, gate.run.sink)
    monkeypatch.setattr(mcp_gate, "version", lambda: (9, 0))
    mcp_gate.patch(module, gate)
    assert sink.lines[0]["notice"] == "newer_version"
    assert "mcp 9.0 is newer" in str(sink.lines[0]["message"])
    gated = module.ClientSession.send_request
    mcp_gate.patch(module, gate)  # a second patch does not wrap twice
    assert module.ClientSession.send_request is gated
    monkeypatch.setattr(mcp_gate, "version", lambda: (2, 3))
    sink.lines.clear()
    mcp_gate.patch(library(), gate)
    assert sink.lines == []


def test_a_version_that_cannot_be_read_is_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert mcp_gate.version() is None
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "weird")
    assert mcp_gate.version() is None


def test_with_no_run_the_gate_only_passes_the_call_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dispatcher:
        async def send_raw_request(self, request: str) -> str:
            return f"sent {request}"

    module = library()
    mcp_gate.patch(module, gate_for([]))
    session = module.ClientSession()
    session._dispatcher = Dispatcher()
    monkeypatch.setattr(mcp_gate, "current", lambda: None)
    assert asyncio.run(session.send_request("tools/call", None)) == "sent tools/call"


def transport_library(seen: list[object]) -> types.ModuleType:
    """A stand-in for mcp.client.streamable_http, to watch the mark come and go."""

    @contextlib.asynccontextmanager
    async def stream_within_origin(client: Any, method: str, url: str) -> AsyncIterator[str]:
        seen.append(MCP_TRANSPORT.get())
        if client == "broken":
            raise OSError("down")
        yield "reply"

    @contextlib.asynccontextmanager
    async def sse_within_origin(client: Any, url: str) -> AsyncIterator[str]:
        seen.append(MCP_TRANSPORT.get())
        yield "events"

    async def request_within_origin(client: Any, method: str, url: str) -> str:
        seen.append(MCP_TRANSPORT.get())
        if client == "broken":
            raise OSError("down")
        return "reply"

    module = types.ModuleType("mcp.client.streamable_http")
    module.stream_within_origin = stream_within_origin  # type: ignore[attr-defined]
    module.sse_within_origin = sse_within_origin  # type: ignore[attr-defined]
    module.request_within_origin = request_within_origin  # type: ignore[attr-defined]
    module.httpx2 = mcp_http.httpx2  # type: ignore[attr-defined]
    return module


URL = "http://Docs.Acme.Internal:8000/mcp"
ENDPOINT = ("http", "docs.acme.internal", 8000, "/mcp")


@pytest.mark.design("mcp.mark_ends_with_the_send")
def test_the_mark_lasts_for_the_send_and_not_for_the_reply() -> None:
    seen: list[object] = []
    module = transport_library(seen)
    gate = gate_for([])
    mcp_gate.patch_transport(module, gate)
    gated = module.request_within_origin
    mcp_gate.patch_transport(module, gate)  # a second patch does not wrap twice
    assert module.request_within_origin is gated

    async def go() -> None:
        async with module.stream_within_origin("client", "POST", URL) as reply:
            assert reply == "reply"
            assert MCP_TRANSPORT.get() is None
        async with module.sse_within_origin("client", URL):
            assert MCP_TRANSPORT.get() is None
        assert await module.request_within_origin("client", "DELETE", URL) == "reply"
        assert MCP_TRANSPORT.get() is None

    asyncio.run(go())
    assert seen == [ENDPOINT, ENDPOINT, ENDPOINT]


@pytest.mark.design("mcp.mark_ends_with_the_send")
def test_a_send_that_fails_leaves_no_mark() -> None:
    module = transport_library([])
    mcp_gate.patch_transport(module, gate_for([]))

    async def go() -> None:
        with pytest.raises(OSError, match="down"):
            async with module.stream_within_origin("broken", "POST", URL):
                pass
        assert MCP_TRANSPORT.get() is None
        with pytest.raises(OSError, match="down"):
            await module.request_within_origin("broken", "DELETE", URL)
        assert MCP_TRANSPORT.get() is None

    asyncio.run(go())


# Labels and settings.


@pytest.mark.design("mcp.labels")
@pytest.mark.parametrize(
    "label", ["mcp:search", "mcp:docs/search", "mcp:io.acme/docs/search", "mcp:My Docs/search"]
)
def test_a_good_label_is_kept_as_it_is(label: str) -> None:
    assert labels.check(label, "x") == label
    assert labels.check(label, "x", pattern=True) == label


@pytest.mark.design("mcp.labels")
@pytest.mark.parametrize(
    "label", ["mcp:", "mcp:*", "mcp:docs/*", "mcp:/search", "mcp:docs/", "mcp: x", "mcp:a\nb"]
)
def test_a_bad_label_is_rejected_with_a_pointer(label: str) -> None:
    with pytest.raises(ConfigError, match=r"docs/configuration\.md"):
        labels.check(label, "x", pattern=True)


def test_a_bare_mcp_word_is_not_a_label() -> None:
    with pytest.raises(ConfigError, match="'mcp:<tool>'"):
        labels.check("mcp", "x")


@pytest.mark.design("mcp.decide")
def test_a_short_label_names_the_tool_on_any_server() -> None:
    long, short = mcp_gate.call_for("docs", "search"), "mcp:search"
    numbers = [1]
    fault = Fault(short, "error", "always")
    planned = Plan("r", "t", "f", (), (fault,), (), (), 1.0)
    assert decide.decide(long, planned, numbers).action == "fault"
    assert decide.decide(long, a_plan(real=(short,)), []).action == "real"
    assert decide.decide(long, a_plan(real=("mcp:wiki/search",)), []).reason == "unlisted"
    assert decide.decide(long, a_plan(), []).reason == "unlisted"


@pytest.mark.design("mcp.unnamed_server")
def test_a_server_with_no_name_has_only_the_short_label() -> None:
    call = mcp_gate.call_for(None, "search")
    assert call.label == "mcp:search"
    assert decide.decide(call, a_plan(real=(LABEL,)), []).reason == "unlisted"


@pytest.mark.design("mcp.config")
@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (f'[[test.mock]]\ncall = "{LABEL}"\nreturns = "x"\ntype = "a.b:C"\n', "type: unknown"),
        (f'[[test.mock]]\ncall = "{LABEL}"\n', "returns: required"),
        (f'[[test.fault]]\ncall = "{LABEL}"\ndo = "status 503"\n', "works on a model call"),
        (
            f'[[test.fault]]\ncall = "{LABEL}"\ndo = "error"\nraises = "KeyError"\n',
            "raises: unknown",
        ),
        (f'[[test.fault]]\ncall = "{LABEL}"\ndo = "slow"\n', "is not a fault"),
        (f'[[test.fault]]\ncall = "{LABEL}"\ndo = "error"\nwhen = "requests 1"\n', "counts model"),
        ('[[test.fault]]\ncall = "tool:x"\ndo = "empty"\n', "works only on an MCP tool"),
        ('[[test.fault]]\ncall = "http:a.b"\ndo = "empty"\n', "works only on an MCP tool"),
        ('[[test.fault]]\ncall = "model"\ndo = "empty"\n', "works only on an MCP tool"),
    ],
)
def test_bad_mcp_settings_are_rejected_with_the_reason(
    tmp_path: Path, body: str, problem: str
) -> None:
    path = tmp_path / "derail.toml"
    path.write_text(toml("acme.docs_bot:answer", body))
    with pytest.raises(ConfigError, match=problem):
        config.load(path)


@pytest.mark.design("mcp.plan_schema")
def test_a_plan_with_mcp_entries_fits_the_schema_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "derail.toml"
    body = MOCK + EMPTY
    path.write_text(toml("acme.docs_bot:answer", body, f'real = ["mcp:note", "{LABEL}"]'))
    loaded = config.load(path)
    row_ = plan.rows(loaded)[1]
    made = plan.plan_for(loaded, row_, "run-1")
    schema = json.loads((Path(plan.__file__).parent / "schemas" / "plan.v1.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(made.to_json())
    assert Plan.from_json(made.to_json()) == made
