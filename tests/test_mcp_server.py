"""The Scrimmage MCP server, driven over real stdio by the SDK's own client.

Each test starts `scrimmage-mcp` in a copy of the example bot, with the fake model, and calls
its tools as a coding agent would. tests/mcp_shim is on the server's PYTHONPATH, so a test can
run a few lines of its own in the server, in each scrimmage command it starts, or in a run.
"""

import contextlib
import importlib.util
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
                "test", {"max_cost_usd": 0.5}, progress_callback=on_progress
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
