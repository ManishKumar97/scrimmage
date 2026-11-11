"""The Scrimmage MCP server, driven over real stdio by the SDK's own client.

Each test starts `scrimmage-mcp` in a copy of the example bot, with the fake model, and calls
its tools as a coding agent would. tests/mcp_shim is on the server's PYTHONPATH, so a test can
run a few lines of its own in the server, in each scrimmage command it starts, or in a run.
"""

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
import mcp_types as types
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conftest import ACME, EXAMPLE
from fakes.openai_server import FakeOpenAI
from scrimmage_core import cli, mcp_server

pytestmark = pytest.mark.slow
Fake = Callable[..., FakeOpenAI]
SERVER = str(Path(sys.executable).parent / "scrimmage-mcp")
SHIM = Path(__file__).parent / "mcp_shim"
# The shim's test: is this process a scrimmage command the server started?
IN_CHILD = "import sys\nchild = sys.orig_argv[1:4] == ['-P', '-m', 'scrimmage_core']\n"
IN_SERVER = "import sys\nserver = sys.orig_argv[1:2] == [" + repr(SERVER) + "]\n"
# Each child the server starts writes a marker named for its pid.
COUNT_STARTS = IN_CHILD + (
    "if child:\n"
    "    import os, pathlib\n"
    "    pathlib.Path(os.environ['STARTS'], str(os.getpid())).touch()\n"
)


@dataclass
class Bed:
    """A copy of the example bot, and the environment the server starts in."""

    root: Path
    env: dict[str, str]
    log: Path  # the server's standard error, which a client keeps in its own log
    scratch: Path

    @property
    def toml(self) -> Path:
        return self.root / "scrimmage.toml"

    def edit(self, old: str, new: str) -> None:
        text = self.toml.read_text()
        assert old in text, old
        self.toml.write_text(text.replace(old, new))

    def shim(self, code: str) -> None:
        path = self.scratch / "shim.py"
        path.write_text(code)
        self.env["SCRIMMAGE_TEST_SHIM"] = str(path)

    def model(self, fake: FakeOpenAI) -> None:
        self.env.update(OPENAI_BASE_URL=fake.base_url, OPENAI_API_KEY="test")

    def starts(self) -> int:
        return len(list((self.scratch / "starts").iterdir()))

    def params(self, *args: str, config: str = "scrimmage.toml") -> StdioServerParameters:
        return StdioServerParameters(
            command=SERVER, args=["--config", config, *args], env=self.env, cwd=self.root
        )


@pytest.fixture
def bed(tmp_path: Path) -> Bed:
    # Python runs only the first sitecustomize it finds, so the shim works only if the
    # environment has none of its own.
    assert importlib.util.find_spec("sitecustomize") is None
    root = tmp_path / "acme-bot"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("__pycache__"))
    scratch = tmp_path / "scratch"
    (scratch / "starts").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SHIM), env.get("PYTHONPATH")) if p)
    env["STARTS"] = str(scratch / "starts")
    return Bed(root, env, scratch / "server.err", scratch)


@contextlib.asynccontextmanager
async def connect(
    bed: Bed, *args: str, config: str = "scrimmage.toml"
) -> AsyncIterator[ClientSession]:
    with bed.log.open("a") as errlog:
        async with (
            stdio_client(bed.params(*args, config=config), errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session


def call(
    bed: Bed, *calls: tuple[str, dict[str, Any]], server: tuple[str, ...] = ()
) -> list[types.CallToolResult]:
    """Each call in turn, on one server."""

    async def go() -> list[types.CallToolResult]:
        async with connect(bed, *server) as session:
            return [await session.call_tool(name, arguments) for name, arguments in calls]

    return anyio.run(go)


def listed(bed: Bed, *server: str) -> tuple[types.InitializeResult, list[types.Tool]]:
    async def go() -> tuple[types.InitializeResult, list[types.Tool]]:
        with bed.log.open("a") as errlog:
            async with (
                stdio_client(bed.params(*server), errlog=errlog) as (read, write),
                ClientSession(read, write) as session,
            ):
                hello = await session.initialize()
                return hello, (await session.list_tools()).tools

    return anyio.run(go)


def texts(result: types.CallToolResult) -> list[str]:
    return [block.text for block in result.content if isinstance(block, types.TextContent)]


def doc_of(result: types.CallToolResult) -> dict[str, Any]:
    assert isinstance(result.structured_content, dict), texts(result)
    return cast(dict[str, Any], result.structured_content)


ANNOTATIONS = {
    "check": {"readOnlyHint": True, "openWorldHint": False},
    "show": {"readOnlyHint": True, "openWorldHint": False},
    "test": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
             "openWorldHint": True},
    "record": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
               "openWorldHint": True},
    "init": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
             "openWorldHint": False},
}  # fmt: skip


def hints(tool: types.Tool) -> dict[str, object]:
    assert tool.annotations is not None
    return tool.annotations.model_dump(by_alias=True, exclude_none=True)


def test_the_server_lists_five_tools_with_their_annotations(bed: Bed) -> None:
    hello, tools = listed(bed)
    assert hello.server_info.name == "scrimmage"
    assert hello.instructions and "never on the wording of a message" in hello.instructions
    assert [tool.name for tool in tools] == ["check", "test", "record", "init", "show"]
    for tool in tools:
        assert hints(tool) == ANNOTATIONS[tool.name], tool.name
        assert tool.description and tool.description.startswith("Scrimmage: "), tool.name
        assert tool.input_schema["additionalProperties"] is False, tool.name
        assert tool.output_schema == mcp_server.OUTPUT_SCHEMAS[tool.name], tool.name
    by_name = {tool.name: tool for tool in tools}
    for name in ("test", "record"):
        assert by_name[name].input_schema["required"] == ["max_cost_usd"], name
    assert "required" not in by_name["check"].input_schema


def test_probe_is_not_an_input_without_the_server_flag(bed: Bed) -> None:
    bed.shim(COUNT_STARTS)
    _, tools = listed(bed)
    init = next(tool for tool in tools if tool.name == "init")
    assert "probe" not in init.input_schema["properties"]
    (refused,) = call(bed, ("init", {"probe": "acme.agent:answer"}))
    assert refused.is_error and refused.structured_content is None
    assert "probe" in texts(refused)[0] and bed.starts() == 0
    _, tools = listed(bed, "--allow-probe")
    init = next(tool for tool in tools if tool.name == "init")
    assert "probe" in init.input_schema["properties"]
    assert hints(init)["openWorldHint"] is True


# Every flag of each command the server runs: the input it comes from, or why it is not one.
FLAGS = {
    "check": {"--json": "always passed", "--runs": "passed by the check before a record",
              "path": "the server's --config"},
    "test": {"--json": "always passed", "--compare": "compare",
             "--max-cost-usd": "max_cost_usd", "path": "the server's --config"},
    "record": {"--json": "always passed", "--test": "tests", "--refresh": "refresh",
               "--runs": "runs", "--max-cost-usd": "max_cost_usd",
               "path": "the server's --config"},
    "init": {"--json": "always passed", "--out": "the server's --config", "--probe": "probe",
             "folder": "folder"},
    "show": {"--json": "always passed", "--run": "run", "--compare": "compare",
             "target": "target"},
}  # fmt: skip


def cli_flags(command: str, capsys: pytest.CaptureFixture[str]) -> set[str]:
    """The flags and positionals `scrimmage COMMAND --help` lists."""
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
    usage = capsys.readouterr().out.split("\n\n")[0]
    words = set(usage.replace("[", " ").replace("]", " ").split()[3:])
    return {w for w in words if w.startswith("--") or w.islower()} - {"--help", "-h", "..."}


def test_every_cli_flag_is_an_input_or_left_out_with_a_reason(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    state = mcp_server.Scrimmage(
        mcp_server.Settings(tmp_path, tmp_path / "scrimmage.toml", 10.0, False, True, 1800.0)
    )
    schemas = {tool.name: tool.input_schema["properties"] for tool in state.tools()}
    for command, mapped in FLAGS.items():
        found = cli_flags(command, capsys)
        assert found == set(mapped), (command, found ^ set(mapped))
        for flag, reason in mapped.items():
            assert reason in schemas[command] or " " in reason, (command, flag, reason)


SPANS = """
import os
if server:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    class ToFile(SpanExporter):
        def export(self, spans):
            with open(os.environ["SPANS"], "a") as file:
                file.writelines(span.name + "\\n" for span in spans)
            return SpanExportResult.SUCCESS

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ToFile()))
    trace.set_tracer_provider(provider)
    if os.environ.get("KEEP_TELEMETRY"):  # the server as mcp builds it, to show a span is seen
        from scrimmage_core import mcp_server
        mcp_server.without_telemetry = lambda server: None
"""


def test_the_server_makes_no_span(bed: Bed, fake_openai: Fake) -> None:
    spans = bed.scratch / "spans.txt"
    bed.env["SPANS"] = str(spans)
    bed.shim(IN_SERVER + SPANS)
    bed.edit("[agent]\n", "[agent]\nruns = 1\n")
    seen: list[float] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append(progress)

    async def go() -> None:
        async with connect(bed) as session:
            await session.list_tools()
            assert not (await session.call_tool("check", {})).is_error
            done = await session.call_tool(
                "test", {"max_cost_usd": 5}, progress_callback=on_progress
            )
            assert not done.is_error and doc_of(done)["command"] == "test", texts(done)

    for keep in ("", "1"):
        bed.model(fake_openai(ACME * 2))
        bed.env["KEEP_TELEMETRY"] = keep
        spans.write_text("")
        anyio.run(go)
        names = spans.read_text().splitlines()
        if keep:
            assert "tools/call check" in names, names  # the same steps, unpatched, give spans
        else:
            assert names == [], names


def test_scrimmage_mcp_without_the_extra_says_how_to_install(bed: Bed) -> None:
    bed.shim("import sys\nsys.modules['mcp'] = None  # as if the extra were not installed\n")
    done = subprocess.run(
        [SERVER, "--config", "scrimmage.toml"],
        env=bed.env, cwd=bed.root, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert done.returncode == 2 and done.stdout == ""
    assert done.stderr == mcp_server.INSTALL + "\n"
    assert 'pip install "scrimmage-core[mcp]"' in done.stderr


def in_subfolder(bed: Bed) -> None:
    """The bot and its file moved one folder down, so the config's folder is not the root."""
    sub = bed.root / "sub"
    sub.mkdir()
    for name in ("acme", "bot.py", "scrimmage.toml"):
        (bed.root / name).rename(sub / name)


def test_a_forwarded_records_path_round_trips_with_a_config_in_a_subfolder(
    bed: Bed, fake_openai: Fake
) -> None:
    in_subfolder(bed)
    bed.model(fake_openai(ACME * 6))
    toml = bed.root / "sub" / "scrimmage.toml"
    toml.write_text(toml.read_text().replace("[agent]\n", "[agent]\nruns = 1\n"))

    async def go() -> None:
        async with connect(bed, config="sub/scrimmage.toml") as session:
            first = doc_of(await session.call_tool("test", {"max_cost_usd": 5}))
            records = first["records"]
            assert first["exit"] == 0 and (bed.root / records).is_dir(), first
            shown = doc_of(await session.call_tool("show", {"target": records}))
            assert shown["exit"] == 0 and shown["records"] == records, shown
            later = await session.call_tool("test", {"max_cost_usd": 5, "compare": records})
            assert doc_of(later)["compare"]["earlier"] == records

    anyio.run(go)


def test_a_path_outside_the_root_starts_no_child(bed: Bed, tmp_path: Path) -> None:
    bed.shim(COUNT_STARTS)
    (bed.root / "out").symlink_to(tmp_path)
    values = ["../x", "/etc", "-x", "~/x", "a\x00b", "a\nb", "a\x1bb", "a/" * 512 + "b", "é" * 128]
    calls = [
        call
        for value in [*values, "out", str(tmp_path / "acme-bot2")]
        for call in (
            ("show", {"target": value}),
            ("show", {"compare": value}),
            ("test", {"max_cost_usd": 0.5, "compare": value}),
            ("init", {"folder": value}),
        )
    ]
    results = call(bed, *calls)
    for (name, arguments), result in zip(calls, results, strict=True):
        assert result.is_error and result.structured_content is None, (name, arguments)
    assert bed.starts() == 0


def server_exit(bed: Bed, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SERVER, *args], env=bed.env, cwd=bed.root, capture_output=True, text=True,
        timeout=60, check=False, stdin=subprocess.DEVNULL,
    )  # fmt: skip


def test_the_root_cannot_be_slash_and_the_config_must_be_inside_it(
    bed: Bed, tmp_path: Path
) -> None:
    for args, word in (
        (["--root", "/", "--config", "etc/x.toml"], "--root"),
        (["--root", str(bed.toml), "--config", "x.toml"], "--root"),
        (["--root", str(tmp_path / "missing"), "--config", "x.toml"], "--root"),
        (["--config", "../outside.toml"], "--config"),
        (["--config", str(tmp_path / "outside.toml")], "--config"),
        (["--root", str(bed.root)], "--config"),  # it is required
    ):
        done = server_exit(bed, *args)
        assert done.returncode == 2 and word in done.stderr, (args, done.stderr)
    (bed.root / "in").symlink_to(tmp_path)
    done = server_exit(bed, "--config", "in/scrimmage.toml")  # a link that leads out
    assert done.returncode == 2 and "--config" in done.stderr
    bed.toml.unlink()  # a config that does not exist yet, for init to write, is fine
    _, tools = listed(bed)
    assert len(tools) == 5
    (initialised,) = call(bed, ("init", {}))
    assert doc_of(initialised)["exit"] == 0 and bed.toml.exists()


SK = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0"
# What a scrimmage command does when it starts, chosen per call by the file MODE names.
MODES = IN_CHILD + (
    "import os, pathlib\n"
    "mode_file = pathlib.Path(os.environ.get('MODE', '/nonexistent'))\n"
    "mode = mode_file.read_text() if child and mode_file.exists() else ''\n"
    "if mode == 'check-raises':\n"
    "    from scrimmage_core import config\n"
    "    def boom(path):\n"
    f"        raise RuntimeError('the reader broke on {SK}')\n"
    "    config.load = boom\n"
    "if mode == 'init-raises':\n"
    "    from scrimmage_core import init\n"
    "    def boom(*args, **kwargs):\n"
    "        raise RuntimeError('the drafter broke')\n"
    "    init.prepare = boom\n"
    "if mode == 'show-raises':\n"
    "    from scrimmage_core import show\n"
    "    def boom(*args, **kwargs):\n"
    "        raise RuntimeError('show broke')\n"
    "    show.build = boom\n"
    "if mode == 'no-document':\n"
    "    sys.stderr.write('Traceback (most recent call last):\\n')\n"
    "    sys.stderr.write(f'  File \"{os.getcwd()}/acme/agent.py\", line 3\\n')\n"
    f"    sys.stderr.write('RuntimeError: {SK} at ' + os.getcwd() + '/x' + ' y' * 300 + '\\n\\n')\n"
    "    sys.stderr.flush()\n"
    "    os._exit(5)\n"
    "if mode == 'junk':\n"
    "    mode_file.write_text('')  # once\n"
    "    sys.stdout.write('this is not a document\\n')\n"
    "    sys.stdout.flush()\n"
    "if mode == 'loud':\n"
    "    sys.stderr.write('x' * 4 * 2**20)  # far more than a pipe holds\n"
    "    sys.stderr.flush()\n"
    "if mode == 'read-stdin':\n"
    "    pathlib.Path(os.environ['STDIN_SEEN']).write_text(\n"
    "        os.readlink('/proc/self/fd/0') + '\\n' + sys.stdin.read()\n"
    "    )\n"
)


@dataclass
class Modes:
    bed: Bed

    def __call__(self, mode: str) -> None:
        (self.bed.scratch / "mode").write_text(mode)


@pytest.fixture
def modes(bed: Bed) -> Modes:
    bed.shim(MODES)
    bed.env["MODE"] = str(bed.scratch / "mode")
    return Modes(bed)


def received(result: types.CallToolResult) -> str:
    """Every byte the client got for this result, as it serializes it."""
    return result.model_dump_json(by_alias=True)


RECORD_BOT = """
import openai

def get_invoice(order_id):
    return INVOICE

def answer(question):
    openai.OpenAI().chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}]
    )
    get_invoice(ORDER)
    return "ok"
"""
RECORD_TOML = """[agent]
function = "acme.invoices:answer"
tools    = ["acme.invoices:get_invoice"]
real     = ["tool:get_invoice"]
runs     = 1
model_hosts = [{host = "127.0.0.1", provider = "openai"}]

[[test]]
name     = "invoice"
question = "Send me the invoice"
expect   = ["ok"]
"""


def record_bot(bed: Bed, invoice: str, order: str = "'1'") -> None:
    """A bot whose one real tool returns INVOICE, called with ORDER."""
    code = RECORD_BOT.replace("INVOICE", invoice).replace("ORDER", order)
    (bed.root / "acme" / "invoices.py").write_text(code)
    bed.toml.write_text(RECORD_TOML)


def test_a_secret_in_a_recorded_reply_or_argument_never_reaches_the_client(
    bed: Bed, fake_openai: Fake
) -> None:
    reply_key, argument_key = (
        "sk-proj-" + "Z9y8X7w6V5u4T3s2R1q0",
        "sk-proj-" + "Q1w2E3r4T5y6U7i8O9p0",
    )
    record_bot(bed, f"{{'status': 'paid', 'token': '{reply_key}'}}", repr(argument_key))
    bed.model(fake_openai(["chat_answer"]))
    (recorded,) = call(bed, ("record", {"max_cost_usd": 5}))
    assert doc_of(recorded)["numbers"]["calls_seen"] == 1, texts(recorded)
    assert (bed.root / "scrimmage.recorded.toml").exists()  # the replies were recorded
    sent = received(recorded)
    assert reply_key not in sent and argument_key not in sent


def test_each_tool_gives_one_result_per_exit_code(
    bed: Bed, modes: Modes, fake_openai: Fake
) -> None:
    """test 0 1 2 3, check 0 2 3, record 0 1 2 3, init 0 2 3, show 0 2 3: one result each,
    an error exactly when the exit is 2 or 3, with the document whenever there is one."""
    results: dict[tuple[str, int], types.CallToolResult] = {}
    bed.edit("[agent]\n", "[agent]\nruns = 1\n")
    usd = {"max_cost_usd": 5}

    def one(name: str, code: int, arguments: dict[str, Any], *, mode: str = "") -> None:
        modes(mode)
        (results[(name, code)],) = call(bed, (name, arguments))

    bed.model(fake_openai(ACME * 2))
    one("test", 0, usd)
    one("show", 0, {})
    one("show", 2, {"target": "nowhere"})
    one("show", 3, {}, mode="show-raises")
    one("test", 2, {**usd, "compare": "nowhere"})  # a mistake check cannot see
    one("check", 0, {})
    one("check", 3, {}, mode="check-raises")
    bed.model(fake_openai(ACME * 2))
    bed.edit('expect   = ["shipped", ', 'expect   = ["refunded", ')
    one("test", 1, usd)
    bed.model(fake_openai(["500:error_500"]))
    one("test", 3, usd)
    bed.edit('name     = "order-status"', 'name     = ""')
    one("check", 2, {})
    record_bot(bed, "'paid'")
    bed.model(fake_openai(["chat_answer"]))
    one("record", 0, usd)
    one("record", 2, {**usd, "tests": ["nobody"]})
    record_bot(bed, "b'%PDF'")  # bytes, which a mock cannot hold: listed for a person
    bed.model(fake_openai(["chat_answer"]))
    one("record", 1, usd)
    bed.model(fake_openai(["500:error_500"]))
    one("record", 3, usd)
    one("init", 2, {})  # the config exists, and init never replaces a file
    one("init", 3, {}, mode="init-raises")
    bed.toml.unlink()
    one("init", 0, {})
    assert len(results) == 17
    for (name, code), result in results.items():
        doc = doc_of(result)
        assert (doc["command"], doc["exit"]) == (name, code), (name, code, texts(result))
        assert result.is_error is (code in (2, 3)), (name, code)
        assert doc["status"] == cli.STATUS[name][code]
        assert texts(result)[0].startswith(f"{name}: {doc['status']} (exit {code})")
        assert json.loads(texts(result)[1]) == doc  # the same document, as text


def test_the_test_result_equals_its_result_file_under_the_path_rule(
    bed: Bed, fake_openai: Fake
) -> None:
    bed.edit("[agent]\n", "[agent]\nruns = 1\n")
    bed.model(fake_openai(ACME * 2))
    (done,) = call(bed, ("test", {"max_cost_usd": 5}))
    doc = doc_of(done)
    assert doc["records"].startswith("./scrimmage-runs/"), doc["records"]
    assert doc["result_file"] == doc["records"] + "/result.json"
    saved = json.loads((bed.root / doc["result_file"]).read_text())
    assert str(bed.root) in json.dumps(saved)  # the file names the folder; the result does not
    home = os.path.expanduser("~")
    paths = mcp_server.path_rule([str(bed.root)], [home, os.path.realpath(home)])
    assert {k: v for k, v in doc.items() if k != "result_file"} == mcp_server.forwarded(
        saved, paths
    )
    assert str(bed.root) not in received(done) and home not in received(done)


def test_every_result_that_is_not_an_error_fits_its_output_schema(
    bed: Bed, fake_openai: Fake
) -> None:
    import jsonschema

    bed.edit("[agent]\n", "[agent]\nruns = 1\n")
    bed.model(fake_openai(ACME * 4))
    usd = {"max_cost_usd": 5}
    first, *rest = call(bed, ("test", usd), ("check", {}), ("show", {}))
    later = call(bed, ("test", {**usd, "compare": doc_of(first)["records"]}))
    shown = call(bed, ("show", {"compare": doc_of(first)["records"]}))
    for result in [first, *rest, *later, *shown]:
        assert not result.is_error, texts(result)
        doc = doc_of(result)
        jsonschema.Draft202012Validator(mcp_server.OUTPUT_SCHEMAS[doc["command"]]).validate(doc)
    assert doc_of(later[0])["compare"] is not None and doc_of(shown[0])["compare"] is not None


def test_a_mistake_or_infra_result_is_an_error_with_its_document(bed: Bed, modes: Modes) -> None:
    (mistake,) = call(bed, ("show", {"target": "nowhere"}))
    modes("show-raises")
    (infra,) = call(bed, ("show", {}))
    for result, status in ((mistake, "mistake"), (infra, "infra")):
        assert result.is_error and doc_of(result)["status"] == status
        assert json.loads(texts(result)[1]) == doc_of(result)
    assert "show broke" in doc_of(infra)["infra"][0]["message"]


def test_a_secret_in_an_infra_message_never_reaches_the_client(bed: Bed, modes: Modes) -> None:
    modes("check-raises")
    (result,) = call(bed, ("check", {}))
    doc = doc_of(result)
    assert result.is_error and doc["status"] == "infra"
    assert "the reader broke on [secret removed]" in doc["infra"][0]["message"]
    assert SK not in received(result)
    assert SK in bed.log.read_text() or SK not in bed.log.read_text()  # the log is the client's


def test_a_child_that_prints_no_document_gives_its_redacted_last_line(
    bed: Bed, modes: Modes
) -> None:
    modes("no-document")
    (result,) = call(bed, ("check", {}))
    assert result.is_error and result.structured_content is None
    (said,) = texts(result)
    assert "printed no document (exit 5)" in said
    tail = said.split("its last line on standard error: ")[1].split("\n")[0]
    assert tail == ("RuntimeError: [secret removed] at ./x" + " y" * 300)[:200]
    assert "Traceback" not in said and "agent.py" not in said and SK not in received(result)
    assert "no document" in bed.log.read_text() and SK not in bed.log.read_text()


def test_junk_on_the_childs_stdout_is_a_child_failure_and_the_server_goes_on(
    bed: Bed, modes: Modes
) -> None:
    modes("junk")
    junk, after = call(bed, ("check", {}), ("check", {}))
    assert junk.is_error and junk.structured_content is None
    assert "not JSON" in texts(junk)[0]
    assert not after.is_error and doc_of(after)["status"] == "valid"


def test_a_team_json_py_in_the_project_does_not_reach_the_child(bed: Bed) -> None:
    (bed.root / "json.py").write_text("raise SystemExit('the team json.py was imported')\n")
    (result,) = call(bed, ("check", {}))
    assert not result.is_error and doc_of(result)["status"] == "valid", texts(result)


def test_the_child_reads_nothing_from_the_protocol_stream(bed: Bed, modes: Modes) -> None:
    seen = bed.scratch / "stdin.txt"
    bed.env["STDIN_SEEN"] = str(seen)
    modes("read-stdin")
    first, second = call(bed, ("check", {}), ("check", {}))
    assert seen.read_text() == "/dev/null\n"  # its standard input is empty, not the protocol
    assert doc_of(first)["status"] == doc_of(second)["status"] == "valid"


def test_record_with_several_runs_gives_its_run_count_as_a_note(
    bed: Bed, fake_openai: Fake
) -> None:
    record_bot(bed, "'paid'")
    bed.model(fake_openai(["chat_answer"]))
    (result,) = call(bed, ("record", {"max_cost_usd": 5, "runs": 2}))
    doc = doc_of(result)
    line = "2 runs of each of 1 test; each calls these for real: tool:get_invoice"
    assert doc["notes"][0] == {"message": line}, doc["notes"]
    record_line = next(x for x in bed.log.read_text().splitlines() if ": record: " in x)
    assert record_line.endswith(", 0 bytes on standard error"), record_line


def test_a_child_that_fills_its_stderr_does_not_block(bed: Bed, modes: Modes) -> None:
    modes("loud")

    async def go() -> types.CallToolResult:
        with anyio.fail_after(120):  # a ceiling: a blocked child would never end on its own
            async with connect(bed) as session:
                return await session.call_tool("check", {})

    result = anyio.run(go)
    assert not result.is_error and doc_of(result)["status"] == "valid"
    assert f", {4 * 2**20} bytes on standard error" in bed.log.read_text()
