"""The Scrimmage MCP server, driven over real stdio by the SDK's own client.

Each test starts `scrimmage-mcp` in a copy of the example bot, with the fake model, and calls
its tools as a coding agent would. tests/mcp_shim is on the server's PYTHONPATH, so a test can
run a few lines of its own in the server, in each scrimmage command it starts, or in a run.
"""

import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from functools import partial
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
def bed(tmp_path: Path) -> Iterator[Bed]:
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
    made = Bed(root, env, scratch / "server.err", scratch)
    yield made
    for pid in ours(made):  # a stop test that fails must not leave its runs going for 600 s
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


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
              "--max-cost-usd": "passed by the check before a spend",
              "--service": "never passed, as a check by the server reads the file only",
              "--strict": "never passed, as it needs --service",
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


def test_a_link_inside_the_root_is_refused_by_show_as_on_the_cli(
    bed: Bed, fake_openai: Fake
) -> None:
    """The server lets a link that stays inside through, and passes it on as a link, so show
    refuses it, as scrimmage show does from a shell."""
    bed.edit("[agent]\n", "[agent]\nruns = 1\n")
    bed.model(fake_openai(ACME * 2))
    (done,) = call(bed, ("test", {"max_cost_usd": 5}))
    records = doc_of(done)["records"]
    (bed.root / "latest").symlink_to(bed.root / records)
    (direct, linked) = call(bed, ("show", {"target": records}), ("show", {"target": "latest"}))
    assert doc_of(direct)["exit"] == 0
    assert doc_of(linked)["exit"] == 2 and linked.is_error, texts(linked)
    by_hand = subprocess.run(
        [str(Path(sys.executable).parent / "scrimmage"), "show", "latest", "--json"],
        env=bed.env, cwd=bed.root, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert by_hand.returncode == 2  # the same refusal the CLI gives


# In the server: a link the path goes through is pointed elsewhere after the check and before
# the show child starts, as another process of the same user could.
SWAP_A_LINK = IN_SERVER + (
    "if server:\n"
    "    import os\n"
    "    import anyio\n"
    "    real = anyio.open_process\n"
    "    async def open_process(*args, **kwargs):\n"
    "        if args[0][4] == 'show':\n"
    "            os.unlink(os.environ['SWAP_LINK'])\n"
    "            os.symlink(os.environ['SWAP_TO'], os.environ['SWAP_LINK'])\n"
    "        return await real(*args, **kwargs)\n"
    "    anyio.open_process = open_process\n"
)


def test_a_link_higher_up_swapped_after_the_check_cannot_lead_outside(
    bed: Bed, fake_openai: Fake, tmp_path: Path
) -> None:
    bed.edit("[agent]\n", "[agent]\nruns = 1\n")
    bed.model(fake_openai(ACME * 2))
    (done,) = call(bed, ("test", {"max_cost_usd": 5}))
    records = (bed.root / doc_of(done)["records"]).resolve()
    outside = tmp_path / "outside-runs"  # a copy outside the root, with a marker in it
    shutil.copytree(records.parent, outside)
    result_file = outside / records.name / "result.json"
    saved = json.loads(result_file.read_text())
    saved["rows"][0]["test"] = "OUTSIDE-MARKER"
    result_file.write_text(json.dumps(saved))
    (bed.root / "deep").symlink_to(records.parent)  # two levels above the file named
    bed.shim(SWAP_A_LINK)
    bed.env.update(SWAP_LINK=str(bed.root / "deep"), SWAP_TO=str(outside))
    (shown,) = call(bed, ("show", {"target": f"deep/{records.name}/result.json"}))
    assert (bed.root / "deep").resolve() == outside  # the swap did happen
    assert doc_of(shown)["exit"] == 0, texts(shown)  # the folder checked, read as it was
    assert "OUTSIDE-MARKER" not in received(shown)


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


REPLY_KEY = "sk-proj-" + "Z9y8X7w6V5u4T3s2R1q0"
ARGUMENT_KEY = "sk-proj-" + "Q1w2E3r4T5y6U7i8O9p0"


def recorded_as_text(bed: Bed, fake_openai: Fake) -> tuple[types.CallToolResult, dict[str, Any]]:
    """record, with a scrimmage.recorded.toml the team wrote already there: record never
    replaces it, so its result carries the mocks word for word in mocks_text."""
    (bed.root / "scrimmage.recorded.toml").write_text("# the team's own notes\n")
    bed.model(fake_openai(["chat_answer"]))
    (recorded,) = call(bed, ("record", {"max_cost_usd": 5}))
    return recorded, doc_of(recorded)


def test_a_secret_in_a_recorded_reply_or_argument_never_reaches_the_client(
    bed: Bed, fake_openai: Fake
) -> None:
    """A real tool's reply reaches the coding agent in mocks_text; the key in it does not."""
    record_bot(bed, f"{{'status': 'paid on Oct 30', 'note': 'ref {REPLY_KEY}'}}")
    recorded, doc = recorded_as_text(bed, fake_openai)
    assert doc["numbers"]["calls_seen"] == 1, texts(recorded)
    assert "mocks_text" in doc, doc  # the replies reach the client, word for word
    assert "paid on Oct 30" in doc["mocks_text"] and "[secret removed]" in doc["mocks_text"]
    assert REPLY_KEY not in received(recorded)
    assert (bed.root / "scrimmage.recorded.toml").read_text() == "# the team's own notes\n"


def test_a_secret_in_a_recorded_argument_never_reaches_the_client(
    bed: Bed, fake_openai: Fake
) -> None:
    record_bot(bed, "'paid on Oct 30'", repr(ARGUMENT_KEY))
    recorded, doc = recorded_as_text(bed, fake_openai)
    assert doc["numbers"]["calls_seen"] == 1, texts(recorded)
    (manual,) = doc["tests"][0]["manual"]  # a mock of a secret argument could never match
    assert manual["code"] == "secret_changed_shape" and manual["call"] == "tool:get_invoice"
    assert "mocks_text" in doc and "[secret removed]" in doc["mocks_text"]
    assert ARGUMENT_KEY not in received(recorded)


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
    assert SK not in bed.log.read_text()  # the server's own log line holds no message text


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

    async def go() -> list[types.CallToolResult]:
        # A ceiling: a child that read the protocol stream would wait on it, and so would we.
        with anyio.fail_after(30):
            async with connect(bed) as session:
                return [await session.call_tool("check", {}) for _ in range(2)]

    first, second = anyio.run(go)
    # Its standard input is empty, not the protocol. The server passes /dev/null itself; with
    # mcp 2.3 a child that inherited it would see /dev/null too, as stdio_server puts it on fd 0
    # while it serves, so this guards the server's own choice against a later SDK.
    assert seen.read_text() == "/dev/null\n"
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
    assert ", 0 bytes on standard error," in record_line, record_line


def test_a_child_that_fills_its_stderr_does_not_block(bed: Bed, modes: Modes) -> None:
    modes("loud")

    async def go() -> types.CallToolResult:
        with anyio.fail_after(120):  # a ceiling: a blocked child would never end on its own
            async with connect(bed) as session:
                return await session.call_tool("check", {})

    result = anyio.run(go)
    assert not result.is_error and doc_of(result)["status"] == "valid"
    assert f", {4 * 2**20} bytes on standard error" in bed.log.read_text()


# A price at which one call's reservation, its 4096 output tokens, is about $0.04: no call
# fits under a cap of a few tenths of a cent, so a test stops on its budget with $0 spent.
PRICED = "[agent.prices]\ngpt-4o-mini = {input = 10, output = 10}\n\n[[test]]\n"


def capped(bed: Bed, cap: str) -> None:
    bed.edit("[agent]\n", f"[agent]\nmax_cost_usd = {cap}\nruns = 1\n")
    bed.edit("[[test]]\n", PRICED)


def guard_of(result: types.CallToolResult) -> dict[str, Any]:
    assert result.meta is not None, texts(result)
    return cast(dict[str, Any], result.meta[mcp_server.GUARD])


def test_no_budget_is_refused_before_any_run(bed: Bed, fake_openai: Fake) -> None:
    bed.shim(COUNT_STARTS)
    bed.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\n')
    fake = fake_openai([])
    bed.model(fake)
    refused = call(bed, ("test", {"max_cost_usd": 5}), ("record", {"max_cost_usd": 5}))
    for result in refused:
        assert result.is_error and result.structured_content is None
        assert (
            'max_cost_usd = "none"' in texts(result)[0] and "--allow-no-budget" in texts(result)[0]
        )
    assert fake.paths == [] and not (bed.root / "scrimmage-runs").exists()
    assert bed.starts() == 2  # the two checks, and nothing after them


def test_no_budget_runs_with_the_server_flag_capped_by_the_argument(
    bed: Bed, fake_openai: Fake
) -> None:
    capped(bed, '"none"')
    bed.model(fake_openai([]))
    (result,) = call(bed, ("test", {"max_cost_usd": 0.004}), server=("--allow-no-budget",))
    doc = doc_of(result)
    assert doc["budget_usd"] == 0.004 and doc["stopped_by"] == "budget", doc
    assert guard_of(result)["file_max_cost_usd"] is None
    assert guard_of(result)["max_cost_usd"] == 0.004


def test_record_runs_on_a_file_with_no_budget_is_checked_under_the_argument(
    bed: Bed, fake_openai: Fake
) -> None:
    """record --runs 2 refuses a file with no budget and a real tool, unless a cap is given;
    the check before the record passes the argument, so it is not refused when record runs."""
    record_bot(bed, "'paid'")
    bed.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\n')
    bed.model(fake_openai(["chat_answer"]))
    (result,) = call(bed, ("record", {"max_cost_usd": 5, "runs": 2}), server=("--allow-no-budget",))
    doc = doc_of(result)
    assert (doc["command"], doc["exit"], doc["budget_usd"]) == ("record", 0, 5.0), texts(result)
    assert doc["numbers"]["runs"] == 2


def test_max_cost_usd_is_required_on_test_and_record(bed: Bed) -> None:
    bed.shim(COUNT_STARTS)
    for result in call(bed, ("test", {}), ("record", {"runs": 2})):
        assert result.is_error and "'max_cost_usd' is a required property" in texts(result)[0]
    assert bed.starts() == 0


def test_a_file_cap_above_the_argument_is_refused(bed: Bed) -> None:
    bed.shim(COUNT_STARTS)
    (result,) = call(bed, ("test", {"max_cost_usd": 0.5}))  # the file's cap is the $5 default
    assert result.is_error and result.structured_content is None
    said = texts(result)[0]
    assert "$5" in said and "$0.5" in said and "at least $5" in said
    assert bed.starts() == 1 and not (bed.root / "scrimmage-runs").exists()  # only the check


RAISE_CAP = IN_CHILD + (
    "import atexit, os, pathlib\n"
    "def edit(old=os.environ['OLD_CAP'], new=os.environ['NEW_CAP']):\n"
    "    toml = pathlib.Path('scrimmage.toml')\n"
    "    toml.write_text(toml.read_text().replace(old, new))\n"
    "if child and sys.orig_argv[4] == 'check':\n"
    "    atexit.register(edit)  # after the check's document, before the test reads the file\n"
)


def test_a_cap_raised_after_the_check_is_still_held_to_the_argument(
    bed: Bed, fake_openai: Fake
) -> None:
    capped(bed, "0.004")
    bed.shim(RAISE_CAP)
    bed.env.update(OLD_CAP="max_cost_usd = 0.004", NEW_CAP="max_cost_usd = 5")
    bed.model(fake_openai([]))
    (result,) = call(bed, ("test", {"max_cost_usd": 0.004}))
    assert "max_cost_usd = 5" in bed.toml.read_text()  # the file did change after the check
    doc = doc_of(result)
    assert doc["budget_usd"] == 0.004 and doc["stopped_by"] == "budget", doc


def test_a_cap_changed_during_the_call_is_said_against_the_lower_of_file_and_argument(
    bed: Bed, fake_openai: Fake
) -> None:
    capped(bed, "0.002")
    bed.model(fake_openai([]))
    (same,) = call(bed, ("test", {"max_cost_usd": 0.004}))
    assert doc_of(same)["budget_usd"] == 0.002
    assert "is not the" not in texts(same)[0]  # the cap used is the lower one, as expected
    bed.shim(RAISE_CAP)
    bed.env.update(OLD_CAP="max_cost_usd = 0.002", NEW_CAP="max_cost_usd = 0.003")
    (changed,) = call(bed, ("test", {"max_cost_usd": 0.004}))
    assert doc_of(changed)["budget_usd"] == 0.003
    assert "the cost cap used, $0.003, is not the $0.002 the check found" in texts(changed)[0]


def test_a_failing_check_stops_a_spend_and_returns_the_check_document(bed: Bed) -> None:
    bed.shim(COUNT_STARTS)
    bed.edit('name     = "order-status"', 'name     = ""')
    for name in ("test", "record"):
        (result,) = call(bed, (name, {"max_cost_usd": 5}))
        doc = doc_of(result)
        assert result.is_error and (doc["command"], doc["exit"]) == ("check", 2)
        assert texts(result)[0].startswith(f"{name}: check found problems; nothing was run")
    assert bed.starts() == 2 and not (bed.root / "scrimmage-runs").exists()


def test_the_cap_entry_and_real_are_in_every_check_test_and_record_result(
    bed: Bed, fake_openai: Fake
) -> None:
    bed.edit("[agent]\n", "[agent]\nruns = 1\n")
    bed.model(fake_openai(ACME * 2))
    entry = {"kind": "function", "value": "acme.agent:answer"}
    results = call(bed, ("check", {}), ("test", {"max_cost_usd": 5}))
    record_bot(bed, "'paid'")
    bed.model(fake_openai(["chat_answer"]))
    results += call(bed, ("record", {"max_cost_usd": 6}))
    for result, usd, point, real in zip(
        results,
        (None, 5, 6),
        (entry, entry, {"kind": "function", "value": "acme.invoices:answer"}),
        ("tool:check_inventory", "tool:check_inventory", "tool:get_invoice"),
        strict=True,
    ):
        guard = guard_of(result)
        assert guard["max_cost_usd"] == usd and guard["file_max_cost_usd"] == 5.0
        assert guard["entry"] == point and guard["real"] == [real]
        assert guard["checked_by"]["status"] == "valid"
        assert guard["prices"] == {}  # the example names no price of its own
        assert guard["session_cap_usd"] == 10.0
        said = texts(result)[0]
        assert f"entry: function {point['value']}" in said and f"real: {real}" in said
        assert "cost cap: $5" in said or "of a $5 cap" in said, said
        assert usd is not None or "prices: the built-in table" in said, said


def test_the_session_ceiling_refuses_the_call_that_could_pass_it(
    bed: Bed, fake_openai: Fake
) -> None:
    capped(bed, "0.004")
    bed.shim(COUNT_STARTS)
    bed.model(fake_openai([]))
    usd = {"max_cost_usd": 0.004}
    first, second, third = call(
        bed, ("test", usd), ("test", usd), ("test", usd), server=("--max-session-usd", "0.01")
    )
    assert guard_of(first)["session_spent_usd"] == 0.004
    assert guard_of(second)["session_spent_usd"] == 0.008
    assert third.is_error and third.structured_content is None
    said = texts(third)[0]
    assert "$0.004" in said and "$0.0080" in said and "$0.01" in said
    assert bed.starts() == 4  # a check and a test for each of the first two


def test_a_stopped_spend_counts_at_least_its_argument(bed: Bed, fake_openai: Fake) -> None:
    capped(bed, "0.004")
    bed.model(fake_openai([]))
    (result,) = call(bed, ("test", {"max_cost_usd": 0.004}))
    doc = doc_of(result)
    assert doc["stopped_by"] == "budget" and doc["cost_usd"] == 0.0
    assert guard_of(result)["session_spent_usd"] == 0.004
    assert "session: spent $0.0040 of $10" in texts(result)[0]


def test_a_stopped_record_counts_at_least_its_argument(bed: Bed, fake_openai: Fake) -> None:
    """A record the budget stops is exit 2, a mistake: its stopped_by is what says it stopped,
    with holds that may still be in flight, so it counts at least its argument too."""
    record_bot(bed, "'paid'")
    bed.edit("[agent]\n", "[agent]\nmax_cost_usd = 0.004\n")
    bed.edit("[[test]]\n", PRICED)
    bed.model(fake_openai([]))
    (result,) = call(bed, ("record", {"max_cost_usd": 0.004}))
    doc = doc_of(result)
    assert (doc["exit"], doc["status"], doc["stopped_by"]) == (2, "mistake", "budget"), doc
    assert doc["cost_usd"] == 0.0 and doc["out"] is None
    assert guard_of(result)["session_spent_usd"] == 0.004
    assert "stopped by: budget" in texts(result)[0]


TEST_DIES = IN_CHILD + "if child and sys.orig_argv[4] == 'test':\n    import os\n    os._exit(5)\n"


def test_a_spend_with_no_document_counts_its_argument(bed: Bed) -> None:
    bed.shim(TEST_DIES)
    died, after = call(bed, ("test", {"max_cost_usd": 5}), ("check", {}))
    assert died.is_error and died.structured_content is None
    assert guard_of(died)["session_spent_usd"] == 5.0
    assert guard_of(after)["session_spent_usd"] == 5.0


async def appear(path: Path) -> None:
    """Wait for a marker a run writes; the ceiling only ends a test whose run never got there."""
    with anyio.fail_after(120):
        while not path.exists():
            await anyio.sleep(0.02)


def hang(bed: Bed, function: str = "acme.slow:hang") -> None:
    bed.edit('function = "acme.agent:answer"', f'function = "{function}"\nruns = 1\n')
    bed.edit(bed.toml.read_text().split("[[test.fault]]")[1], "")
    bed.edit("[[test.fault]]", "")


SECOND = {
    "test": {"max_cost_usd": 5},
    "record": {"max_cost_usd": 5},
    "init with probe": {"probe": "acme.agent:answer"},
}


@pytest.mark.parametrize("second", list(SECOND))
def test_a_second_spend_while_one_runs_is_refused(bed: Bed, second: str) -> None:
    hang(bed)

    async def go() -> types.CallToolResult:
        got: list[types.CallToolResult] = []
        async with connect(bed, "--allow-probe") as session, anyio.create_task_group() as group:
            group.start_soon(session.call_tool, "test", {"max_cost_usd": 5})
            await appear(bed.root / "hanging")
            got.append(await session.call_tool(second.split()[0], SECOND[second]))
            group.cancel_scope.cancel()
        return got[0]

    refused = anyio.run(go)
    assert refused.is_error and texts(refused) == [mcp_server.BUSY]


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def test_the_lock_is_held_until_a_cancelled_spend_has_stopped(bed: Bed) -> None:
    hang(bed, "acme.slow:stubborn")  # its run ignores SIGTERM, so only SIGKILL stops it

    async def go() -> tuple[int, int, types.CallToolResult]:
        async with connect(bed) as session:
            async with anyio.create_task_group() as group:
                group.start_soon(session.call_tool, "test", {"max_cost_usd": 5})
                await appear(bed.root / "ignoring")
                group.cancel_scope.cancel()  # the client cancels the call
            run = int((bed.root / "ignoring").read_text())
            busy = 0
            while True:
                after = await session.call_tool("record", {"max_cost_usd": 5, "tests": ["x"]})
                if texts(after) != [mcp_server.BUSY]:
                    return run, busy, after
                busy += 1
                await anyio.sleep(0.05)

    run, busy, after = anyio.run(go)
    assert busy > 0  # the first call's stop held the lock
    assert not alive(run)  # and the next spend started only once its run was gone
    assert doc_of(after)["command"] == "record" and doc_of(after)["exit"] == 2


def test_check_and_show_run_while_a_test_runs(bed: Bed) -> None:
    hang(bed)

    async def go() -> list[types.CallToolResult]:
        reads: list[types.CallToolResult] = []
        async with connect(bed) as session, anyio.create_task_group() as group:
            group.start_soon(session.call_tool, "test", {"max_cost_usd": 5})
            await appear(bed.root / "hanging")
            reads += [await session.call_tool("check", {}), await session.call_tool("show", {})]
            group.cancel_scope.cancel()
        return reads

    check, shown = anyio.run(go)
    assert doc_of(check)["status"] == "valid"
    assert doc_of(shown)["command"] == "show"  # a document, not a refusal


def test_probe_runs_only_with_the_server_flag(bed: Bed) -> None:
    bed.shim(COUNT_STARTS)
    (refused,) = call(bed, ("init", {"probe": "acme.agent:answer"}))
    assert refused.is_error and "probe" in texts(refused)[0] and bed.starts() == 0

    async def go() -> types.CallToolResult:
        async with connect(bed, "--allow-probe", config="draft.toml") as session:
            return await session.call_tool("init", {"probe": "acme.agent:answer"})

    probed = anyio.run(go)
    doc = doc_of(probed)
    assert doc["exit"] == 0 and doc["probe"]["ran"] is True, texts(probed)
    assert "probe ran your code up to its first model request" in texts(probed)[0]
    assert bed.starts() == 1


# Agents for the stop tests. Each writes a marker once it is where a stop should find it.
STOPPABLE = '''
import os, signal, sys, time
from pathlib import Path


def mark(name, text=None):
    Path(name + ".tmp").write_text(str(os.getpid()) if text is None else text)
    os.replace(name + ".tmp", name)


def answer(question):
    """Says it got SIGTERM, then goes."""
    def stopped(signum, frame):
        mark("got-sigterm")
        os._exit(0)
    signal.signal(signal.SIGTERM, stopped)
    mark("hanging")
    time.sleep(600)
    return "never"


def stubborn(question):
    """Ignores SIGTERM, so only SIGKILL stops it."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    mark(f"ignoring-{os.getpid()}")
    time.sleep(600)
    return "never"


def spawner(question):
    """Leaves a process in a session of its own, which unsets the call's variable."""
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if os.environ.get("UNSET_NONCE"):
            os.unsetenv("SCRIMMAGE_MCP_CALL")
            os.environ.pop("SCRIMMAGE_MCP_CALL", None)
            mark("unset")
        time.sleep(600)
        os._exit(0)
    mark("grandchild", str(pid))
    mark("hanging")
    time.sleep(600)
    return "never"
'''


def stoppable(bed: Bed, function: str, *, runs: int = 1) -> None:
    (bed.root / "acme" / "stoppable.py").write_text(STOPPABLE)
    hang(bed, f"acme.stoppable:{function}")
    if runs > 1:
        bed.edit("runs = 1\n", f"runs = {runs}\nparallel = {runs}\n")


def ours(bed: Bed) -> list[int]:
    """Every process that holds this test's environment: the server and all it started."""
    mark = f"STARTS={bed.env['STARTS']}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            with contextlib.suppress(OSError):
                if mark in (entry / "environ").read_bytes().split(b"\0") and alive(int(entry.name)):
                    found.append(int(entry.name))
    return found


def none_left(bed: Bed) -> bool:
    """Waits up to 30 s for every process the test started to be gone."""
    ceiling = time.monotonic() + 30
    while ours(bed):
        if time.monotonic() > ceiling:
            return False
        time.sleep(0.1)
    return True


async def cancel_when(bed: Bed, session: ClientSession, marker: str, **arguments: Any) -> None:
    """Call test, and cancel it from the client once the run writes its marker."""
    async with anyio.create_task_group() as group:
        group.start_soon(session.call_tool, "test", {"max_cost_usd": 5, **arguments})
        await appear(bed.root / marker)
        group.cancel_scope.cancel()


def log_lines(bed: Bed, command: str = "test") -> list[str]:
    return [line for line in bed.log.read_text().splitlines() if f": {command}: " in line]


def test_progress_arrives_while_a_test_runs(bed: Bed) -> None:
    stoppable(bed, "answer")
    seen: list[tuple[float, str | None]] = []
    got = anyio.Event()

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append((progress, message))
        got.set()

    async def go() -> None:
        async with connect(bed) as session, anyio.create_task_group() as group:
            group.start_soon(
                partial(session.call_tool, progress_callback=on_progress),
                "test",
                {"max_cost_usd": 5},
            )
            with anyio.fail_after(60):
                await got.wait()
            group.cancel_scope.cancel()

    anyio.run(go)
    progress, message = seen[0]
    assert progress >= mcp_server.PROGRESS_EVERY_S - 1
    assert message is not None and message.startswith("test running for ")
    assert message.endswith("; cost cap $5")


def test_a_cancelled_test_stops_its_child_by_sigterm_and_leaves_no_process(bed: Bed) -> None:
    stoppable(bed, "answer")

    async def go() -> None:
        async with connect(bed) as session:
            await cancel_when(bed, session, "hanging")

    anyio.run(go)
    assert (bed.root / "got-sigterm").exists()  # the run was stopped by SIGTERM, not SIGKILL
    assert none_left(bed)
    (line,) = log_lines(bed)
    # The child printed its document: it got SIGTERM, Scrimmage's own stop, first.
    assert "the call was cancelled" in line and line.endswith(", status infra"), line


def test_a_client_time_limit_stops_the_child_the_same_way(bed: Bed) -> None:
    stoppable(bed, "answer")

    async def go() -> None:
        async with connect(bed) as session:
            with pytest.raises(Exception, match=r"(?i)timed out|timeout"):
                await session.call_tool("test", {"max_cost_usd": 5}, read_timeout_seconds=8)
            await appear(bed.root / "got-sigterm")

    anyio.run(go)
    assert none_left(bed)
    (line,) = log_lines(bed)
    assert "the call was cancelled" in line and line.endswith(", status infra"), line


def test_the_servers_time_limit_returns_the_stopped_document(bed: Bed) -> None:
    stoppable(bed, "answer")
    (result,) = call(bed, ("test", {"max_cost_usd": 5}), server=("--call-timeout", "8"))
    doc = doc_of(result)
    assert result.is_error and doc["status"] == "infra"
    assert "stopped after the server's 8 s limit" in texts(result)[0]
    assert (bed.root / "got-sigterm").exists() and none_left(bed)


CHILD_IGNORES_SIGTERM = IN_CHILD + (
    "if child:\n"
    "    import signal\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    real = signal.signal\n"
    "    signal.signal = lambda s, h: None if s == signal.SIGTERM else real(s, h)\n"
)


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace(bed: Bed) -> None:
    stoppable(bed, "answer")
    bed.shim(CHILD_IGNORES_SIGTERM)
    cancelled: list[float] = []

    async def go() -> None:
        async with connect(bed) as session:
            await cancel_when(bed, session, "hanging")
            cancelled.append(time.monotonic())
            while not log_lines(bed):  # the stop's line, once the child is gone
                await anyio.sleep(0.1)

    anyio.run(go)
    waited = time.monotonic() - cancelled[0]
    assert waited >= mcp_server.STOP_GRACE_S - 1, waited  # it had its grace
    (line,) = log_lines(bed)
    assert "no document" in line and "exit -9" in line, line  # then SIGKILL
    assert none_left(bed)


def leftover_after_a_killed_child(bed: Bed, *, unset: bool) -> tuple[int, str]:
    """A run leaves a process in a session of its own, then the child is stopped so that its
    own stop never runs: it ignores SIGTERM, and gets SIGKILL after the grace. Linux then
    kills the run, whose runner died, but not the process it left."""
    stoppable(bed, "spawner")
    bed.shim(CHILD_IGNORES_SIGTERM)
    if unset:
        bed.env["UNSET_NONCE"] = "1"

    async def go() -> None:
        async with connect(bed) as session:
            await cancel_when(bed, session, "hanging")
            while not log_lines(bed):
                await anyio.sleep(0.1)

    anyio.run(go)
    (line,) = log_lines(bed)
    return int((bed.root / "grandchild").read_text()), line


def swept_count(line: str) -> int:
    """How many processes the sweep killed. At least the one the run left; the run itself
    also counts when Linux has not yet killed it for its dead runner, which load decides."""
    found = re.search(r", (\d+) left over and killed by the sweep", line)
    assert found, line
    return int(found.group(1))


def test_the_sweep_kills_runs_in_their_own_sessions_by_the_calls_nonce(bed: Bed) -> None:
    grandchild, line = leftover_after_a_killed_child(bed, unset=False)
    assert "exit -9" in line and swept_count(line) >= 1, line
    assert not alive(grandchild) and none_left(bed)


def test_a_run_that_unsets_the_nonce_is_still_swept(bed: Bed) -> None:
    grandchild, line = leftover_after_a_killed_child(bed, unset=True)
    assert (bed.root / "unset").exists()  # it had unset the variable before the sweep
    assert swept_count(line) >= 1, line
    assert not alive(grandchild) and none_left(bed)


def test_closing_stdin_with_four_runs_that_ignore_sigterm_leaves_no_process(bed: Bed) -> None:
    stoppable(bed, "stubborn", runs=4)

    async def go() -> None:
        async with connect(bed) as session, anyio.create_task_group() as group:
            group.start_soon(session.call_tool, "test", {"max_cost_usd": 5})
            with anyio.fail_after(120):
                while len(list(bed.root.glob("ignoring-*"))) < 4:
                    await anyio.sleep(0.05)
            group.cancel_scope.cancel()
        # Leaving the client closes the server's stdin, and the SDK kills it on its own
        # schedule: 2 s to exit, then SIGTERM, 2 s more, then SIGKILL.

    anyio.run(go)
    runs = [int(path.name.split("-")[1]) for path in bed.root.glob("ignoring-*")]
    assert len(runs) == 4
    assert none_left(bed), [pid for pid in runs if alive(pid)]


SERVER_PID = IN_SERVER + (
    "if server:\n"
    "    import os, pathlib\n"
    "    pathlib.Path(os.environ['SERVER_PID']).write_text(str(os.getpid()))\n"
)


def signal_stops(bed: Bed, signum: int) -> None:
    stoppable(bed, "answer")
    bed.shim(SERVER_PID)
    bed.env["SERVER_PID"] = str(bed.scratch / "server.pid")

    async def go() -> None:
        with contextlib.suppress(Exception):
            async with connect(bed) as session, anyio.create_task_group() as group:
                group.start_soon(session.call_tool, "test", {"max_cost_usd": 5})
                await appear(bed.root / "hanging")
                os.kill(int((bed.scratch / "server.pid").read_text()), signum)
                await appear(bed.root / "got-sigterm")
                group.cancel_scope.cancel()

    anyio.run(go)
    assert (bed.root / "got-sigterm").exists() and none_left(bed)


def test_sigterm_to_the_server_stops_a_running_child(bed: Bed) -> None:
    signal_stops(bed, signal.SIGTERM)


def test_sighup_to_the_server_stops_a_running_child(bed: Bed) -> None:
    signal_stops(bed, signal.SIGHUP)


LOOP_RAISES = IN_SERVER + (
    "if server:\n"
    "    import os, pathlib\n"
    "    import anyio\n"
    "    from scrimmage_core import mcp_server\n"
    "    real = mcp_server.Scrimmage.supervise\n"
    "    async def supervise(self, child):\n"
    "        async def boom():\n"
    "            while not pathlib.Path('hanging').exists():\n"
    "                await anyio.sleep(0.05)\n"
    "            raise RuntimeError('an error that escapes the loop')\n"
    "        if child.command == 'test':\n"
    "            self.group.start_soon(boom)\n"
    "        await real(self, child)\n"
    "    mcp_server.Scrimmage.supervise = supervise\n"
)


def test_an_error_in_a_server_task_is_caught_by_the_watch_task_and_signals_every_child(
    bed: Bed,
) -> None:
    stoppable(bed, "answer")
    bed.shim(LOOP_RAISES)

    async def go() -> None:
        # The server goes away once the child is stopped; the ceiling ends a server that hangs.
        with anyio.fail_after(120), contextlib.suppress(Exception):
            async with connect(bed) as session:
                await session.call_tool("test", {"max_cost_usd": 5})

    anyio.run(go)
    assert "scrimmage-mcp: stopped on an error" in bed.log.read_text()
    assert (bed.root / "got-sigterm").exists() and none_left(bed)


# In the server: the child exists, and the start has not returned yet, for 3 s.
SLOW_START = IN_SERVER + (
    "if server:\n"
    "    import os, pathlib\n"
    "    import anyio\n"
    "    real = anyio.open_process\n"
    "    async def open_process(*args, **kwargs):\n"
    "        proc = await real(*args, **kwargs)\n"
    "        if args[0][4] == 'test':  # the test, not the check before it\n"
    "            pathlib.Path(os.environ['STARTING']).touch()\n"
    "            await anyio.sleep(3)\n"
    "        return proc\n"
    "    anyio.open_process = open_process\n"
)


def test_a_cancel_while_the_child_starts_leaves_no_untracked_child(bed: Bed) -> None:
    stoppable(bed, "answer")  # a run that never ends by itself, so a leak shows
    bed.shim(SLOW_START)
    bed.env["STARTING"] = str(bed.scratch / "starting")

    async def go() -> None:
        async with connect(bed) as session:
            async with anyio.create_task_group() as group:
                group.start_soon(session.call_tool, "test", {"max_cost_usd": 5})
                await appear(bed.scratch / "starting")
                group.cancel_scope.cancel()
            # The server is still up, with no call running, so anything left is untracked.
            assert await none_left_but_the_server(bed)
            # The server logs the call last, after its stop, sweep and read.
            with anyio.fail_after(30):
                while not log_lines(bed):
                    await anyio.sleep(0.1)
            (_line,) = log_lines(bed)  # the cancelled call was stopped and logged, once

    anyio.run(go)
    assert none_left(bed)


async def none_left_but_the_server(bed: Bed) -> bool:
    ceiling = time.monotonic() + 30
    while len(ours(bed)) > 1:
        if time.monotonic() > ceiling:
            return False
        await anyio.sleep(0.1)  # the client must go on: it still sends the cancel
    return True


# In the server: SIGTERM to itself after the test's child exists and before its start has
# returned, so the shutdown's own SIGTERM to every tracked child misses it.
SIGTERM_WHILE_STARTING = IN_SERVER + (
    "if server:\n"
    "    import os, signal\n"
    "    import anyio\n"
    "    real = anyio.open_process\n"
    "    async def open_process(*args, **kwargs):\n"
    "        proc = await real(*args, **kwargs)\n"
    "        if args[0][4] == 'test':\n"
    "            os.kill(os.getpid(), signal.SIGTERM)\n"
    "            await anyio.sleep(1)\n"
    "        return proc\n"
    "    anyio.open_process = open_process\n"
)


def test_a_shutdown_while_a_child_starts_still_stops_that_child(bed: Bed) -> None:
    stoppable(bed, "answer")  # a run that never ends by itself, so a missed child shows
    bed.shim(SIGTERM_WHILE_STARTING)

    async def go() -> None:
        with anyio.fail_after(120), contextlib.suppress(Exception):  # the server goes away
            async with connect(bed) as session:
                await session.call_tool("test", {"max_cost_usd": 5})

    anyio.run(go)
    assert none_left(bed), ours(bed)  # by the call's nonce, and every other mark of the test
    (line,) = log_lines(bed)
    assert "the call was cancelled" in line, line


def test_end_of_input_sends_sigterm_before_any_await(bed: Bed) -> None:
    """End of input reaches the server only as the cancel of every call, so the stop must
    signal the child before its first await: a cancelled scope ends at that await."""
    stoppable(bed, "answer")

    async def go() -> None:
        async with connect(bed) as session, anyio.create_task_group() as group:
            group.start_soon(session.call_tool, "test", {"max_cost_usd": 5})
            await appear(bed.root / "hanging")
            group.cancel_scope.cancel()  # and the client closes the server's stdin

    anyio.run(go)
    ceiling = time.monotonic() + 30
    while not (bed.root / "got-sigterm").exists():
        assert time.monotonic() < ceiling, "the child never got SIGTERM"
        time.sleep(0.05)
    lines = log_lines(bed)
    assert not lines or (bed.root / "got-sigterm").stat().st_mtime <= bed.log.stat().st_mtime
    assert none_left(bed)


def test_the_server_answers_the_next_call_after_a_cancel(bed: Bed) -> None:
    stoppable(bed, "answer")

    async def go() -> types.CallToolResult:
        async with connect(bed) as session:
            await cancel_when(bed, session, "hanging")
            return await session.call_tool("check", {})

    after = anyio.run(go)
    assert doc_of(after)["status"] == "valid"


def test_the_docs_mcp_json_starts_the_server_and_lists_its_tools(bed: Bed) -> None:
    page = (Path(__file__).parents[1] / "docs" / "mcp-server.md").read_text()
    block = page.split("```json\n")[1].split("```")[0]
    entry = json.loads(block)["mcpServers"]["scrimmage"]
    assert entry["type"] == "stdio"
    venv = bed.root / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "scrimmage-mcp").symlink_to(SERVER)  # the project's own environment, as installed

    async def go() -> list[types.Tool]:
        params = StdioServerParameters(
            command=str(bed.root / entry["command"]), args=entry["args"], env=bed.env, cwd=bed.root
        )
        with bed.log.open("a") as errlog:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                return (await session.list_tools()).tools

    assert [tool.name for tool in anyio.run(go)] == list(mcp_server.COMMANDS)
