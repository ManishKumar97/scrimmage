import asyncio
import sys
from pathlib import Path

from mcp.client.stdio import StdioServerParameters

from faultline_lab.mcp_client import launched, mcp_tools
from faultline_lab.settings import Settings
from faultline_lab.tools import Registry

SERVER = StdioServerParameters(
    command=sys.executable, args=[str(Path(__file__).parent / "mcp_test_server.py")]
)


def test_without_settings_the_server_launches_as_it_is():
    assert launched(SERVER, [], []) == SERVER.model_copy(update={"env": {}})


def test_a_prefix_goes_before_the_servers_command(monkeypatch):
    monkeypatch.setenv("WRAP_ME", "1")

    server = launched(SERVER, ["wrap", "--"], ["WRAP_ME", "NOT_SET"])

    assert server.command == "wrap"
    assert server.args == ["--", sys.executable, *SERVER.args]
    assert server.env == {"WRAP_ME": "1"}


def test_a_prefixed_server_still_serves_its_tools():
    async def scenario():
        async with mcp_tools(launched(SERVER, ["env"], []), Registry()) as tools:
            return await tools.call("echo", '{"text": "hi"}')

    assert asyncio.run(scenario()) == ("echo hi", True)


def test_the_settings_read_the_prefix_and_names_as_json_lists(monkeypatch):
    monkeypatch.setenv("MCP_COMMAND_PREFIX", '["faultline", "mcp-wrap", "--"]')
    monkeypatch.setenv("MCP_PASS_ENV", '["FAULTLINE_TRACE_DIR"]')

    settings = Settings(provider="groq", groq_api_key="g", _env_file=None)  # pyright: ignore[reportCallIssue]

    assert settings.mcp_command_prefix == ["faultline", "mcp-wrap", "--"]
    assert settings.mcp_pass_env == ["FAULTLINE_TRACE_DIR"]
