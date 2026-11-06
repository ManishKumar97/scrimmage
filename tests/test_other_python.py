"""Children on another Python, and stdio MCP servers, against real second venvs."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import jsonschema
import pytest

from conftest import Project, Result
from derail_sdk.control import ControlServer
from derail_sdk.records import RecordStore
from derail_sdk.run_command import BOOT
from test_bootstrap import this_python
from test_scripts import RECORD, calls, lines_of, script, summary_of, write_config

ROOT = Path(__file__).parents[1]
REPORT = "from acme import tools\nprint(tools.refund_order('1234'))\n"
RUNS_OTHER = """
import subprocess, sys
done = subprocess.run(sys.argv[2:] or {command!r}, capture_output=True, text=True)
print(repr((done.returncode, done.stdout, done.stderr)))
"""
TOKEN = "e" * 64


def uv(*args: str, cwd: Path) -> None:
    subprocess.run(["uv", *args], cwd=cwd, check=True, capture_output=True, timeout=300)


@pytest.fixture(scope="session")
def other_version(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Another Python version, with pip, and no Derail: as a team's cloud tool would be."""
    version = "3.12" if sys.version_info[:2] == (3, 13) else "3.13"
    folder = tmp_path_factory.mktemp("other-version")
    uv("venv", "--no-project", "--seed", "--python", version, "venv", cwd=folder)
    return folder / "venv" / "bin" / "python"


@pytest.fixture(scope="session")
def other_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The same binary as this Python, with its own packages: Derail, and mcp 1.x."""
    folder = tmp_path_factory.mktemp("other-venv")
    base = Path(sys.base_prefix) / "bin" / "python3"
    uv("venv", "--no-project", "--python", str(base), "venv", cwd=folder)
    python = folder / "venv" / "bin" / "python"
    packages = [str(ROOT / "packages" / name) for name in ("derail", "derail-marker")]
    uv("pip", "install", "--python", str(python), "mcp<2", *packages, cwd=folder)
    return python


def plain_run(command: list[str]) -> tuple[int, str, str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DERAIL_")}
    done = subprocess.run(command, capture_output=True, text=True, env=clean, timeout=60)
    return done.returncode, done.stdout, done.stderr


def run_child(project: Project, command: list[str], extra: str = "") -> Result:
    """A bot that starts command, and answers with exactly what came back."""
    return script(project, RUNS_OTHER.format(command=command), extra)


@pytest.mark.design("other.pip_untouched")
def test_pip_on_another_python_runs_untouched_and_prints_nothing_extra(
    project: Project, other_version: Path
) -> None:
    pip = [str(other_version.parent / "pip"), "--version"]
    result = run_child(project, pip)
    assert result.code == 0, result.out
    assert summary_of(result)["answer"] == repr(plain_run(pip))
    assert "child" not in json.dumps(lines_of(result))


@pytest.mark.design("other.children_set_pip_untouched")
def test_with_children_set_a_python_with_no_derail_still_runs_untouched(
    project: Project, other_version: Path
) -> None:
    pip = [str(other_version), "-m", "pip", "--version"]
    result = run_child(project, pip, "children = true")
    assert summary_of(result)["answer"] == repr(plain_run(pip)), result.out


def imported(python: Path) -> list[str]:
    code = "import sys; print('derail_sdk' in sys.modules, flush=True)"
    return [str(python), "-c", code]


SHELL_BOT = """#!/bin/sh
{other} -c "print('tool ok')" > tool.out
exec {python} bot.py "$1"
"""


@pytest.mark.design("claim.other_python_first")
@pytest.mark.parametrize("which", ["other_version", "other_venv"])
def test_a_python_before_the_bot_in_a_wrapper_does_not_take_the_run(
    project: Project, request: pytest.FixtureRequest, which: str
) -> None:
    """With no SDK, or under a shell, it is a tool. The first Python with the SDK claims."""
    python = cast(Path, request.getfixturevalue(which))
    (project.root / "run.sh").write_text(SHELL_BOT.format(other=python, python=sys.executable))
    (project.root / "bot.py").write_text("print('the answer')\n")
    write_config(project, "sh run.sh {question}")
    result = project.test()
    if which == "other_version":
        assert summary_of(result)["answer"] == "the answer", result.out
        assert (project.root / "tool.out").read_text() == "tool ok\n"
    else:  # it has the SDK, so it claims, and the bot on another Python is then not joined
        assert (project.root / "tool.out").read_text() == "tool ok\n"
        assert summary_of(result)["outcome"] == "answered", result.out


@pytest.mark.design("claim.uv_build")
def test_a_package_build_uv_starts_does_not_stop_the_run(project: Project) -> None:
    """uv builds the project with a Python of its own before it starts the bot."""
    packages = ROOT / "packages"
    (project.root / "pyproject.toml").write_text(
        f"""
[project]
name = "acme-bot"
version = "0"
requires-python = ">=3.11"
dependencies = ["derail", "derail-marker"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["acme"]

[tool.uv.sources]
derail = {{path = "{packages / "derail"}", editable = true}}
derail-marker = {{path = "{packages / "derail-marker"}", editable = true}}
"""
    )
    (project.root / "bot.py").write_text("print('the answer')\n")
    project.monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    project.monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    project.monkeypatch.delenv("UV_LOCKED", raising=False)
    version = "{}.{}".format(*sys.version_info)
    write_config(
        project, f"uv run --python {version} python bot.py {{question}}", "run_timeout = 90"
    )
    result = project.test()
    assert summary_of(result)["outcome"] == "answered", result.out
    assert summary_of(result)["answer"] == "the answer"


CROSS_BOT = """
import subprocess, sys
subprocess.run([sys.executable, "report.py"])
print("the answer")
"""


@pytest.mark.design("other.bot_on_its_own_venv")
@pytest.mark.parametrize("how", ["direct", "wrapper"])
def test_a_bot_on_another_venv_with_derail_keeps_its_children(
    project: Project, other_venv: Path, how: str
) -> None:
    """derail test runs here, so the folder on PYTHONPATH is not the bot's own Derail."""
    (project.root / "bot.py").write_text(CROSS_BOT)
    (project.root / "report.py").write_text("from acme import tools\ntools.check_inventory('1')\n")
    (project.root / "run.sh").write_text(f'#!/bin/sh\nexec {other_venv} bot.py "$1"\n')
    command = "sh run.sh {question}" if how == "wrapper" else f"{other_venv} bot.py {{question}}"
    write_config(project, command)
    result = project.test()
    assert summary_of(result)["outcome"] == "answered", result.out
    assert [x["call"] for x in calls(result)] == ["tool:check_inventory"]


@pytest.mark.design("other.grandchildren_with_children_set")
def test_with_children_set_grandchildren_across_venvs_stay_joined(
    project: Project, other_venv: Path
) -> None:
    (project.root / "bot.py").write_text(CROSS_BOT)
    (project.root / "report.py").write_text(
        f"import subprocess\nsubprocess.run([{str(other_venv)!r}, 'grandchild.py'])\n"
    )
    (project.root / "grandchild.py").write_text(REPORT)
    write_config(project, f"{sys.executable} bot.py {{question}}", "children = true")
    result = project.test()
    assert result.code == 2 and [x["call"] for x in calls(result)] == ["tool:refund_order"]


@pytest.mark.design("claim.no_python_claimed")
def test_a_wrapper_around_python_with_no_sdk_is_an_infrastructure_error(
    project: Project, other_version: Path
) -> None:
    (project.root / "run.sh").write_text(f'#!/bin/sh\nexec {other_version} bot.py "$1"\n')
    (project.root / "bot.py").write_text("print('the answer')\n")
    write_config(project, "sh run.sh {question}")
    result = project.test()
    assert result.code == 3 and summary_of(result)["outcome"] == "infrastructure error"


@pytest.mark.design("other.copies_are_one_python")
def test_python_and_python3_in_a_venv_of_copies_are_one_python(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    venv = [sys.executable, "-m", "venv", "--copies", "--without-pip", str(folder)]
    subprocess.run(venv, check=True, timeout=60)
    # By path, as a system Python's own sitecustomize takes the name once ours chains to it.
    code = (
        "import importlib.util as util, sys;"
        "spec = util.spec_from_file_location('boot', sys.argv[1]);"
        "boot = util.module_from_spec(spec); spec.loader.exec_module(boot);"
        "print(boot._python())"
    )
    boot = str(BOOT / "sitecustomize.py")

    def identity(name: str) -> str:
        command = [str(folder / "bin" / name), "-c", code, boot]
        done = subprocess.run(command, env={}, capture_output=True, text=True)
        return done.stdout

    assert (folder / "bin" / "python").resolve() != (folder / "bin" / "python3").resolve()
    assert identity("python") == identity("python3") != ""


@pytest.mark.design("other.same_binary_is_another_python")
def test_a_venv_on_the_same_binary_is_another_python_even_with_derail_installed(
    project: Project, other_venv: Path
) -> None:
    result = run_child(project, imported(other_venv))
    assert summary_of(result)["answer"] == repr((0, "False\n", "")), result.out


@pytest.mark.design("other.children_join")
def test_with_children_set_a_python_with_derail_joins(project: Project, other_venv: Path) -> None:
    result = run_child(project, imported(other_venv), "children = true")
    assert summary_of(result)["answer"] == repr((0, "True\n", "")), result.out


@pytest.mark.design("other.children_refused")
def test_with_children_set_a_python_with_derail_is_gated(
    project: Project, other_venv: Path
) -> None:
    (project.root / "report.py").write_text(REPORT)
    result = run_child(project, [str(other_venv), "report.py"], "children = true")
    assert result.code == 2, result.out
    assert [x["call"] for x in calls(result)] == ["tool:refund_order"]


@pytest.mark.design("other.tool_kept_out")
@pytest.mark.parametrize("which", ["other_version", "other_venv"])
def test_a_listed_tool_on_another_python_does_not_run_and_the_run_says_so(
    project: Project, request: pytest.FixtureRequest, which: str
) -> None:
    python = cast(Path, request.getfixturevalue(which))
    (project.root / "report.py").write_text(REPORT)
    result = run_child(project, [str(python), "report.py"])
    assert result.code == 2, result.out
    assert summary_of(result)["outcome"] == "refused call"
    answer = str(summary_of(result)["answer"])
    assert "REFUND" not in answer and "ImportError" in answer
    assert "real refund" not in answer
    line = next(x for x in lines_of(result) if x.get("notice") == "other_python")
    assert "acme.tools holds a tool under [agent] tools" in result.out
    details = cast(dict[str, object], line["process"])
    assert details["python"] == str(python) and "report.py" in str(details["command"])
    validator = jsonschema.Draft202012Validator(RECORD, format_checker=jsonschema.FormatChecker())
    validator.validate(line)


@pytest.mark.design("short_name.other_python")
def test_a_shorter_name_for_a_listed_tool_is_kept_out_on_another_python(
    project: Project, other_version: Path
) -> None:
    (project.root / "deep").mkdir()
    (project.root / "deep" / "tools.py").write_text("print('THE WRITE RAN')\n")
    (project.root / "deep" / "run.py").write_text("import tools\n")
    (project.root / "bot.py").write_text(
        RUNS_OTHER.format(command=[str(other_version), "deep/run.py"])
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit('"acme.tools:refund_order"]', '"deep.tools:pay"]')
    result = project.test()
    assert summary_of(result)["outcome"] == "refused call", result.out
    assert "THE WRITE RAN" not in str(summary_of(result)["answer"])


@pytest.mark.design("main.tool_on_another_python")
@pytest.mark.parametrize("how", ["script", "-m", "-um", "-mpays"])
def test_a_tool_in_the_program_on_another_python_stops_it(
    project: Project, other_version: Path, how: str
) -> None:
    (project.root / "pays.py").write_text("print('THE WRITE RAN')\ndef pay(order): ...\n")
    run = {"script": ["pays.py"], "-mpays": ["-mpays"]}.get(how, [how, "pays"])
    (project.root / "bot.py").write_text(RUNS_OTHER.format(command=[str(other_version), *run]))
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit('"acme.tools:refund_order"]', '"acme.tools:refund_order", "pays:pay"]')
    result = project.test()
    assert result.code == 2 and summary_of(result)["outcome"] == "refused call", result.out
    assert "THE WRITE RAN" not in str(summary_of(result)["answer"])


def test_a_secret_on_another_pythons_command_line_is_not_recorded(
    project: Project, other_version: Path
) -> None:
    secret = f"sk-{'c' * 40}"
    (project.root / "report.py").write_text(REPORT)
    run_child(project, [str(other_version), "report.py", secret])
    records = (project.root / "derail-runs").glob("*/*.jsonl")
    assert not any(secret in record.read_text() for record in records)


@pytest.mark.design("other.unrelated_import_passes")
def test_a_module_that_is_not_a_tools_passes_on_another_python(
    project: Project, other_version: Path
) -> None:
    (project.root / "report.py").write_text("import acme.models\nprint('ok')\n")
    result = run_child(project, [str(other_version), "report.py"])
    assert summary_of(result)["answer"] == repr((0, "ok\n", "")), result.out


def test_the_keep_out_works_with_nowhere_to_report(tmp_path: Path, other_version: Path) -> None:
    (tmp_path / "held.py").write_text("print('the write ran')\n")
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(BOOT),
        "DERAIL_TOKEN": TOKEN,
        "DERAIL_PYTHON": "the run's Python",
        "DERAIL_TOOLS": "held:write",
    }
    done = subprocess.run(
        [str(other_version), "-c", "import held"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 1 and "the write ran" not in done.stdout
    assert "cannot gate it" in done.stderr


MCP_ONE_SERVER = """
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("docs")

@server.tool()
def search(q: str) -> str:
    Path(sys.argv[1]).write_text(q)
    return "real " + q

server.run("stdio")
"""
MCP_CLIENT = """
import asyncio, os, sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=sys.argv[2], args=["server.py", str(Path("received").resolve())],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.list_tools()
        result = await session.call_tool("search", {"q": "refunds"})
        print(result.content[0].text)

asyncio.run(main())
"""


@pytest.mark.design("other.mcp_one_server")
def test_a_stdio_server_on_mcp_1_in_another_venv_serves(project: Project, other_venv: Path) -> None:
    (project.root / "server.py").write_text(MCP_ONE_SERVER)
    (project.root / "bot.py").write_text(MCP_CLIENT)
    command = f"{sys.executable} bot.py {{question}} {other_venv}"
    write_config(project, command)
    allow_mcp(project, "search")
    result = project.test()
    assert result.code == 0, result.out + result.err
    assert summary_of(result)["answer"] == "real refunds"
    assert (project.root / "received").read_text() == "refunds"


SERVER_WITH_TOOL = """
from acme import tools
from mcp.server.mcpserver import MCPServer

server = MCPServer("docs")

@server.tool(structured_output=False)
def pay(order: str) -> str:
    return tools.refund_order(order)

@server.tool(structured_output=False)
def look(sku: str) -> str:
    return tools.get_order(sku)

server.run("stdio")
"""


def allow_mcp(project: Project, tool: str) -> None:
    project.edit('real = ["tool:check_inventory"]', f'real = ["mcp:docs/{tool}"]')


def stdio_client_calling(tool: str, argument: str) -> str:
    return (
        MCP_CLIENT.replace('"search", {"q": "refunds"}', f'"{tool}", {{"{argument}": "1"}}')
        .replace('["server.py", str(Path("received").resolve())]', '["server.py"]')
        .replace("sys.argv[2]", "sys.executable")
    )


@pytest.mark.design("mcp.stdio_server_joins")
def test_a_stdio_server_started_with_the_environment_joins_like_a_child(project: Project) -> None:
    (project.root / "server.py").write_text(SERVER_WITH_TOOL)
    (project.root / "bot.py").write_text(stdio_client_calling("pay", "order"))
    write_config(project, f"{sys.executable} bot.py {{question}}")
    allow_mcp(project, "pay")
    result = project.test()
    assert result.code == 2, result.out
    refused = [x for x in calls(result) if x["action"] == "refused"]
    assert [x["call"] for x in refused] == ["tool:refund_order"]
    process = [x for x in lines_of(result) if x.get("notice") == "process"]
    assert any(
        "server.py" in str(cast(dict[str, object], x["process"])["command"]) for x in process
    )
    assert refused[0]["proc"] != next(x for x in process if "bot.py" in str(x["process"]))["proc"]


@pytest.mark.design("mcp.stdio_server_mock")
def test_a_stdio_server_gets_the_tests_mocks_and_its_stream_stays_clean(project: Project) -> None:
    (project.root / "server.py").write_text(SERVER_WITH_TOOL)
    (project.root / "bot.py").write_text(stdio_client_calling("look", "sku"))
    mock = '[[test.mock]]\ncall = "tool:get_order"\nreturns = "mocked in the server"\n'
    write_config(project, f"{sys.executable} bot.py {{question}}", body=mock)
    allow_mcp(project, "look")
    result = project.test()
    assert result.code == 0, result.out + result.err
    assert summary_of(result)["answer"] == "mocked in the server"


@pytest.mark.design("mcp.stream_stays_clean")
def test_joining_prints_nothing_to_a_stdio_servers_stream(tmp_path: Path) -> None:
    server = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    server.start()
    try:
        program = (
            "import sys\n"
            "from acme_tools import write\n"
            "try:\n"
            "    write()\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, file=sys.stderr)\n"
        )
        (tmp_path / "acme_tools.py").write_text("def write():\n    raise SystemExit(9)\n")
        identity = this_python()
        env = {k: v for k, v in os.environ.items() if not k.startswith("DERAIL_")}
        env.update(
            PYTHONPATH=str(BOOT),
            DERAIL_TOKEN=TOKEN,
            DERAIL_CONTROL_URL=server.url,
            DERAIL_RUN="no-such-run",
            DERAIL_PYTHON=identity,
            DERAIL_TOOLS="acme_tools:write",
        )
        done = subprocess.run(
            [sys.executable, "-c", program],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            timeout=60,
        )
    finally:
        server.stop()
    assert done.stdout == b""
    assert done.stderr.strip() == b"DerailRefused"
