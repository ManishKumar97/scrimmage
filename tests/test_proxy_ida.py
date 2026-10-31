"""The IDA through the proxy, model and MCP, gives the same diagnosis as without it."""

import asyncio
import json
import sys
import threading
from pathlib import Path

from ida_harness import fake_llm, ida, ida_env, needs_cluster, run

from faultline.proxy.model import ModelProxy

FAULTLINE = str(Path(sys.executable).parent / "faultline")


@needs_cluster
def test_the_ida_answers_the_same_through_the_model_proxy(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    with fake_llm(tmp_path) as upstream:
        proxy = ModelProxy(upstream)
        url = asyncio.run_coroutine_threadsafe(proxy.start(), loop).result(5)
        try:
            direct = run(ida(), ida_env(tmp_path, GROQ_BASE_URL=upstream), tmp_path)
            proxied = run(ida(), ida_env(tmp_path, GROQ_BASE_URL=url), tmp_path)
        finally:
            asyncio.run_coroutine_threadsafe(proxy.stop(), loop).result(5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()

    assert direct.returncode == 0, direct.stderr
    assert "[stop] done after 2 steps" in direct.stderr
    assert "[tool] get_logs" in direct.stderr
    assert proxied.returncode == 0, proxied.stderr
    assert proxied.stdout == direct.stdout
    assert "ROOT CAUSE SERVICE: cart" in proxied.stdout
    assert proxy.steps.number == 2  # both model calls went through the proxy


@needs_cluster
def test_the_ida_answers_the_same_with_its_mcp_servers_wrapped(tmp_path):
    wrap = json.dumps([FAULTLINE, "mcp-wrap", "--"])
    with fake_llm(tmp_path) as upstream:
        plain = run(ida(), ida_env(tmp_path, GROQ_BASE_URL=upstream), tmp_path)
        wrapped = run(
            ida(),
            ida_env(tmp_path, GROQ_BASE_URL=upstream, MCP_COMMAND_PREFIX=wrap),
            tmp_path,
        )

    assert plain.returncode == 0, plain.stderr
    assert wrapped.returncode == 0, wrapped.stderr
    assert "[tool] get_logs" in wrapped.stderr
    assert "-> error" not in wrapped.stderr
    assert wrapped.stdout == plain.stdout
