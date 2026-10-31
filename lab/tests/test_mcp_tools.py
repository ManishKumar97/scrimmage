import asyncio
import json
import os
import sys
from pathlib import Path
from typing import cast

import fake_tools
from fake_model import FakeClient, text_reply, tool_reply
from fake_tools import text_of
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from openai import AsyncOpenAI

from faultline_lab import ida, mcp_client, mcp_logs
from faultline_lab.models import OpenAIModel
from faultline_lab.settings import Settings
from faultline_lab.tools import Registry

SERVER = StdioServerParameters(
    command=sys.executable, args=[str(Path(__file__).parent / "mcp_test_server.py")]
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def with_server(use):
    async with mcp_client.mcp_tools(SERVER, Registry()) as tools:
        return await use(tools)


def test_server_tools_join_the_registry_with_the_servers_schema():
    async def use(tools: Registry):
        return (
            list(tools.tools),
            tools.specs(),
            await tools.call("echo", '{"text": "hi"}'),
        )

    names, specs, result = asyncio.run(with_server(use))

    assert names == ["pid", "echo", "fail", "hang"]
    echo = next(s for s in specs if s.name == "echo")
    assert echo.description == "Echo the text."
    assert echo.parameters["required"] == ["text"]
    assert result == ("echo hi", True)


def test_mcp_errors_become_tool_errors():
    async def use(tools: Registry):
        return [
            await tools.call("fail", '{"reason": "no cluster"}'),
            await tools.call("echo", '{"text": 5}'),
            await tools.call("echo", "{}"),
            await tools.call("echo", "[1]"),
            await tools.call("nope", "{}"),
        ]

    failed, wrong_type, missing, not_object, unknown = asyncio.run(with_server(use))

    assert failed[1] is False
    assert failed[0].startswith("Error: fail raised RuntimeError: ")
    assert "no cluster" in failed[0]
    for output, ok in (wrong_type, missing):
        assert not ok
        assert output.startswith("Error: echo raised RuntimeError: ")
    assert not_object == ("Error: bad arguments [1] (not a JSON object).", False)
    assert unknown[1] is False


def test_the_server_stops_when_the_block_ends():
    pids: list[int] = []

    async def use(tools: Registry):
        output, ok = await tools.call("pid", "{}")
        assert ok
        pids.append(int(output))

    asyncio.run(with_server(use))

    assert not alive(pids[0])


def test_the_server_stops_when_a_tool_call_is_cancelled():
    pids: list[int] = []

    async def scenario():
        async def use(tools: Registry):
            pids.append(int((await tools.call("pid", "{}"))[0]))
            await tools.call("hang", "{}")

        task = asyncio.create_task(with_server(use))
        while not pids:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)  # the hang call is now in flight
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert not alive(pids[0])


def test_the_server_stops_when_the_block_raises():
    pids: list[int] = []

    async def use(tools: Registry):
        pids.append(int((await tools.call("pid", "{}"))[0]))
        raise KeyError("boom")

    try:
        asyncio.run(with_server(use))
    except KeyError as exc:
        assert exc.args == ("boom",)
    else:
        raise AssertionError("the error was swallowed")

    assert not alive(pids[0])


def run_loop(tools: Registry, client: FakeClient, budget: ida.Budget):
    model = OpenAIModel(cast(AsyncOpenAI, client), "fake")
    return ida.run("why?", model, budget, tools=tools)


def test_the_loop_calls_an_mcp_tool():
    client = FakeClient(
        tool_reply(("echo", json.dumps({"text": "hi"}))), text_reply("done")
    )

    async def use(tools: Registry):
        return await run_loop(tools, client, ida.Budget())

    result = asyncio.run(with_server(use))

    assert (result.stop_reason, result.steps) == ("done", 2)
    assert "echo hi" in json.dumps(client.requests[1]["messages"])


def test_the_run_deadline_ends_an_mcp_tool_and_the_server():
    pids: list[int] = []
    client = FakeClient(tool_reply(("hang", "{}")), text_reply("never"))

    async def use(tools: Registry):
        pids.append(int((await tools.call("pid", "{}"))[0]))
        return await run_loop(tools, client, ida.Budget(max_seconds=1))

    result = asyncio.run(with_server(use))

    assert result.stop_reason == "time_limit"
    assert not alive(pids[0])


def test_a_call_timeout_fails_one_hung_tool_call_and_the_run_goes_on():
    async def use():
        async with mcp_client.mcp_tools(SERVER, Registry(), call_timeout=0.5) as tools:
            hung = await tools.call("hang", "{}")
            after = await tools.call("echo", '{"text": "still here"}')
            return hung, after

    hung, after = asyncio.run(use())

    assert hung == (
        "Error: hang raised MCPError: Request 'tools/call' timed out",
        False,
    )
    assert after == ("echo still here", True)


def test_the_call_timeout_is_a_setting(monkeypatch):
    monkeypatch.setenv("TOOL_TIMEOUT", "2.5")

    settings = Settings(provider="groq", groq_api_key="g", _env_file=None)  # pyright: ignore

    assert settings.tool_timeout_seconds == 2.5


def test_get_logs_over_mcp_reports_cluster_errors_to_the_model(monkeypatch):
    cluster = fake_tools.fake_cluster(monkeypatch)
    cluster.add("cart", {"cart-a": "boom\n"})

    async def scenario():
        async with Client(mcp_logs.server) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            ok = await client.call_tool("get_logs", {"service": "cart"})
            bad = await client.call_tool("get_logs", {"service": "order"})
            return names, ok, bad

    names, ok, bad = asyncio.run(scenario())

    assert names == ["get_logs"]
    assert not ok.is_error
    assert "boom" in text_of(ok)
    assert bad.is_error
    assert "no deployment named 'order' in astronomy-shop" in text_of(bad)


def test_a_server_that_never_answers_fails_the_start(monkeypatch):
    monkeypatch.setattr(mcp_client, "START_TIMEOUT", 0.5)
    silent = StdioServerParameters(
        command=sys.executable, args=["-c", "import time; time.sleep(60)"]
    )

    async def start():
        async with mcp_client.mcp_tools(silent, Registry(), "metrics MCP"):
            pass

    try:
        asyncio.run(start())
    except RuntimeError as exc:
        assert "metrics MCP server did not start within 0.5s" in str(exc)
    else:
        raise AssertionError("the start did not time out")


def test_the_caller_sets_the_start_timeout():
    silent = StdioServerParameters(
        command=sys.executable, args=["-c", "import time; time.sleep(60)"]
    )

    async def start():
        async with mcp_client.mcp_tools(silent, Registry(), "logs MCP", None, 0.5):
            pass

    try:
        asyncio.run(start())
    except RuntimeError as exc:
        assert "logs MCP server did not start within 0.5s" in str(exc)
    else:
        raise AssertionError("the start did not time out")


def test_the_start_timeout_is_a_setting_with_room_for_a_loaded_machine(monkeypatch):
    groq = {"provider": "groq", "groq_api_key": "g", "_env_file": None}
    monkeypatch.delenv("MCP_START_TIMEOUT", raising=False)
    assert Settings(**groq).mcp_start_timeout == 60  # pyright: ignore
    assert mcp_client.START_TIMEOUT == 60

    monkeypatch.setenv("MCP_START_TIMEOUT", "90")
    assert Settings(**groq).mcp_start_timeout == 90  # pyright: ignore


def test_the_server_gets_the_kube_config_path(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/tmp/some-config")
    env_server = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import os; print(os.environ.get('KUBECONFIG'))"],
    )
    seen = {}

    class Spy:
        def __init__(self, params):
            seen["env"] = params.env
            raise RuntimeError("stop")

    monkeypatch.setattr(mcp_client, "Client", Spy)

    async def start():
        async with mcp_client.mcp_tools(env_server, Registry()):
            pass

    try:
        asyncio.run(start())
    except RuntimeError:
        pass

    assert seen["env"] == {"KUBECONFIG": "/tmp/some-config"}
