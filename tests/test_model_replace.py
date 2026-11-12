"""replace on a model call's reply, in one process: the openai library, the real gate and a
mock transport standing in for the provider.

The real call still runs and is billed. The agent gets the reply with its text changed; the
line keeps the real reply, redacted, and the injected text, whole.
"""

import asyncio
import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

from conftest import Project
from fakes.openai_server import FakeOpenAI
from model_gate import GUARD, PRICE, SENDS, URL, Gate, Provider, ask, completion, guard
from scrimmage_core import config, outcomes, provider_openai, show, state
from scrimmage_core.cli import main
from scrimmage_core.config import Fault
from scrimmage_core.errors import ConfigError

SECRET = "sk-" + "a1b2c3d4" * 3  # a shape the redaction rules remove


@pytest.fixture(autouse=True)
def gated() -> Iterator[None]:
    yield
    httpx2.Client._send_single_request, httpx2.AsyncClient._send_single_request = SENDS
    state.install(None)


def replace(text: str = "safe", find: str | None = None, when: str = "always") -> Fault:
    return Fault("model", "replace", when, model=GUARD, text=text, find=find)


def test_replace_changes_the_guard_reply_before_the_library_reads_it() -> None:
    gate = Gate(replace())
    provider = Provider(gate.events)
    assert ask(guard(provider)) == "safe"
    assert len(provider.sent) == 1  # the real call ran, once
    line = gate.line
    assert (line["action"], line["under"]) == ("fault", "real")
    assert line["fault"] == {"do": "replace", "when": "always", "n": 1}
    assert line["changed"]["parts"] == ["choices[0].message.content"]
    assert json.loads(gate.result())["choices"][0]["message"]["content"] == "unsafe\nS14"


def test_replace_with_find_changes_every_occurrence() -> None:
    gate = Gate(replace("safe", find="unsafe"))
    reply = completion("unsafe\nS14")
    reply["choices"].append(  # type: ignore[attr-defined]
        {"index": 1, "message": {"role": "assistant", "content": "unsafe, unsafe"}}
    )
    provider = Provider(gate.events, lambda r: httpx2.Response(200, json=reply))
    got = guard(provider).chat.completions.create(
        model=GUARD, messages=[{"role": "user", "content": "x"}], n=2
    )
    assert [c.message.content for c in got.choices] == ["safe\nS14", "safe, safe"]
    assert gate.line["changed"]["count"] == 3
    assert gate.line["changed"]["parts"] == [
        "choices[0].message.content",
        "choices[1].message.content",
    ]


def test_replace_leaves_the_request_byte_for_byte() -> None:
    gate = Gate()
    plain = Provider(gate.events)
    ask(guard(plain))
    gate = Gate(replace())
    changed = Provider(gate.events)
    ask(guard(changed))
    assert changed.sent == plain.sent


def test_replace_reserves_before_it_sends() -> None:
    gate = Gate(replace())
    ask(guard(Provider(gate.events)))
    assert gate.events == ["reserve", "sent"]


def test_replace_settles_the_real_cost_and_releases_the_hold() -> None:
    gate = Gate(replace())
    ask(guard(Provider(gate.events)))
    real = provider_openai.cost(1000, 10, PRICE)
    assert gate.line["cost_usd"] == pytest.approx(real)
    assert gate.line["tokens"] == {"input": 1000, "output": 10, "cached": 0}
    ((ticket, usd),) = gate.sink.settles
    assert usd == pytest.approx(real)
    gate.budget.ledger.settle(ticket, usd)
    assert gate.budget.ledger.leaked() == []  # nothing is still held


def test_replace_on_an_async_call() -> None:
    gate = Gate(replace())
    provider = Provider(gate.events)

    async def go() -> str:
        http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(provider))
        async with openai.AsyncOpenAI(
            base_url=URL, api_key="k", max_retries=0, http_client=http
        ) as client:
            reply = await client.chat.completions.create(
                model=GUARD, messages=[{"role": "user", "content": "x"}]
            )
        return reply.choices[0].message.content or ""

    assert asyncio.run(go()) == "safe"
    assert gate.events == ["reserve", "sent"]
    assert gate.line["changed"]["parts"] == ["choices[0].message.content"]


def test_replace_on_a_gzip_reply() -> None:
    gate = Gate(replace())
    packed = gzip.compress(json.dumps(completion()).encode())
    headers = {"content-type": "application/json", "content-encoding": "gzip"}
    provider = Provider(
        gate.events, lambda r: httpx2.Response(200, headers=headers, content=packed)
    )
    raw = guard(provider).chat.completions.with_raw_response.create(
        model=GUARD, messages=[{"role": "user", "content": "x"}]
    )
    assert raw.parse().choices[0].message.content == "safe"
    assert "content-encoding" not in raw.headers
    assert raw.headers["content-length"] == str(len(raw.http_response.content))
    assert json.loads(gate.result())["choices"][0]["message"]["content"] == "unsafe\nS14"


def test_a_longer_replacement_is_read_whole() -> None:
    long = "safe " * 4000
    gate = Gate(replace(long))
    raw = guard(Provider(gate.events)).chat.completions.with_raw_response.create(
        model=GUARD, messages=[{"role": "user", "content": "x"}]
    )
    assert raw.parse().choices[0].message.content == long
    # The length the reply declares is the new body's, not the real one's.
    assert raw.headers["content-length"] == str(len(raw.http_response.content))


def stream_reply(request: httpx2.Request) -> httpx2.Response:
    chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": GUARD}
    events = [
        {**chunk, "choices": [{"index": 0, "delta": {"content": "unsafe"}}]},
        {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def test_replace_on_a_streamed_reply_is_a_mistake_and_not_fired() -> None:
    gate = Gate(replace())
    stream = guard(Provider(gate.events, stream_reply)).chat.completions.create(
        model=GUARD, messages=[{"role": "user", "content": "x"}], stream=True
    )
    text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
    assert text == "unsafe"  # the stream reached the agent whole and unchanged
    line = gate.line
    assert line["action"] == "real" and "fault" not in line
    assert line["unchanged"]["why"].startswith("replace on a model call needs a reply that is not")
    assert "turn streaming off for this model" in line["mistake"]


def test_replace_on_a_responses_call_is_a_mistake_and_not_fired() -> None:
    gate = Gate(replace())
    reply = {
        "id": "resp_1", "object": "response", "created_at": 0, "model": GUARD,
        "status": "completed", "output": [], "usage": {"input_tokens": 5, "output_tokens": 1,
        "total_tokens": 6},
    }  # fmt: skip
    provider = Provider(gate.events, lambda r: httpx2.Response(200, json=reply))
    guard(provider).responses.create(model=GUARD, input="x")
    line = gate.line
    assert line["action"] == "real" and "fault" not in line
    assert "works on Chat Completions only, and this call is responses" in line["mistake"]


def test_replace_on_a_reply_with_only_tool_calls_is_unchanged_and_written_real() -> None:
    gate = Gate(replace())
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    reply = completion(None, tool_calls=[call])
    provider = Provider(gate.events, lambda r: httpx2.Response(200, json=reply))
    got = guard(provider).chat.completions.create(
        model=GUARD, messages=[{"role": "user", "content": "x"}]
    )
    assert got.choices[0].message.tool_calls is not None
    line = gate.line
    assert (line["action"], line["under"]) == ("real", "real") and "fault" not in line
    assert line["unchanged"] == {
        "do": "replace",
        "why": "the reply has only tool calls and no text",
    }
    assert "mistake" not in line


def test_replace_on_a_refusal_with_null_content_is_unchanged() -> None:
    gate = Gate(replace())
    reply = completion(None, refusal="I can't help with that.")
    provider = Provider(gate.events, lambda r: httpx2.Response(200, json=reply))
    got = guard(provider).chat.completions.create(
        model=GUARD, messages=[{"role": "user", "content": "x"}]
    )
    assert got.choices[0].message.refusal == "I can't help with that."
    assert gate.line["action"] == "real"
    assert gate.line["unchanged"]["why"] == "the reply is a refusal with no text"


def test_replace_with_a_find_not_in_the_reply_is_unchanged() -> None:
    gate = Gate(replace("safe", find="maybe"))
    assert ask(guard(Provider(gate.events))) == "unsafe\nS14"
    line = gate.line
    assert line["action"] == "real" and "fault" not in line
    assert line["unchanged"]["why"] == "find is not in the result"


def test_an_unchanged_attempt_uses_its_number_in_first_n() -> None:
    gate = Gate(replace(when="first 1"))
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    replies = iter([completion(None, tool_calls=[call]), completion()])
    provider = Provider(gate.events, lambda r: httpx2.Response(200, json=next(replies)))
    client = guard(provider)
    client.chat.completions.create(model=GUARD, messages=[{"role": "user", "content": "x"}])
    assert ask(client) == "unsafe\nS14"  # the second call is the second: first 1 is spent
    first, second = [x for x in gate.sink.lines if x.get("kind") == "model"]
    assert "unchanged" in first and "fault" not in first
    assert second["action"] == "real" and "under" not in second


def load(tmp_path: Path, fault: str, agent: str = "") -> config.Config:
    path = tmp_path / "scrimmage.toml"
    path.write_text(
        f'[agent]\nfunction = "bot:answer"\n{agent}\n'
        '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["ok"]\n\n'
        f"[[test.fault]]\n{fault}"
    )
    return config.load(path)


def test_replace_on_a_model_fault_needs_a_model_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs the model's name, or it would rewrite every"):
        load(tmp_path, 'call = "model"\ndo = "replace"\ntext = "safe"\nexpect = ["x"]\n')
    named = load(tmp_path, f'call = "model"\nmodel = "{GUARD}"\ndo = "replace"\ntext = "safe"\n'
                 'expect = ["x"]\n')  # fmt: skip
    assert named.tests[0].faults[0].text == "safe"
    # slow takes a model name or not.
    assert load(tmp_path, 'call = "model"\ndo = "slow 2s"\nwhen = "first 1"\n').tests


@pytest.mark.parametrize("do", ['"replace"\ntext = "safe"', '"slow 2s"'])
def test_replace_and_slow_on_moderations_are_refused_at_load(tmp_path: Path, do: str) -> None:
    with pytest.raises(ConfigError, match="works on a chat call, not on model:openai/moderations"):
        load(tmp_path, f'call = "model:openai/moderations"\ndo = {do}\nexpect = ["x"]\n')


def test_append_on_a_model_call_is_still_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="append does not work on a model call"):
        load(tmp_path, f'call = "model"\nmodel = "{GUARD}"\ndo = "append"\ntext = "x"\n')


def test_a_carried_model_fault_with_the_budget_off_warns_at_load(tmp_path: Path) -> None:
    fault = f'call = "model"\nmodel = "{GUARD}"\ndo = "replace"\ntext = "safe"\nexpect = ["x"]\n'
    off = load(tmp_path, fault, 'max_cost_usd = "none"')
    (warning,) = config.carried_model_warnings(off)
    assert "fault 1 of 't' makes the real model call, billed" in warning
    assert "a price of 0 under [agent.prices]" in warning
    assert config.carried_model_warnings(load(tmp_path, fault)) == []
    # A timeout sends nothing, so it costs nothing to warn about.
    timeout = f'call = "model"\nmodel = "{GUARD}"\ndo = "timeout"\nexpect = ["x"]\n'
    assert config.carried_model_warnings(load(tmp_path, timeout, 'max_cost_usd = "none"')) == []


def saved(gate: Gate, folder: Path) -> Path:
    """The gate's side-folder items written out as a records folder holds them, for show."""
    (folder / "blobs").mkdir()
    for sha, data in gate.sink.blobs.items():
        (folder / "blobs" / sha).write_bytes(data)
    return folder


def test_a_model_replace_line_keeps_the_real_reply_redacted_and_the_injected_text_whole(
    tmp_path: Path,
) -> None:
    injected = f"safe {SECRET}"
    gate = Gate(replace(injected))
    provider = Provider(
        gate.events, lambda r: httpx2.Response(200, json=completion(f"no {SECRET}"))
    )
    assert ask(guard(provider)) == injected  # the agent got the injected text as written
    line = gate.line
    assert SECRET not in json.dumps(line["result"])  # the real reply's preview is redacted
    real = json.loads(gate.result())["choices"][0]["message"]["content"]
    assert real.startswith("no ") and "safe" not in real  # the line holds the real reply
    assert line["changed"]["text"]["preview"] == injected  # the fault's own text is whole
    # show puts the two side by side, from the line and the reply it points to.
    shown = show.came_back(line, saved(gate, tmp_path))
    assert "replace gave the agent" in shown and SECRET not in shown.partition("gave")[0]


def test_a_replaced_model_reply_is_not_looked_for_in_later_requests() -> None:
    """The delivery note is about a tool result reaching a model; a changed model reply is
    read by the agent, so a later request that holds its text is not marked."""
    gate = Gate(replace())
    client = guard(Provider(gate.events))
    ask(client)
    ask(client, "the guard said safe")
    assert gate.run.injected == []
    assert not [x for x in gate.sink.lines if x.get("carries_injected")]


def test_show_puts_what_the_model_said_beside_what_replace_gave_the_agent(tmp_path: Path) -> None:
    gate = Gate(replace())
    ask(guard(Provider(gate.events)))
    shown = show.came_back(gate.line, saved(gate, tmp_path))
    assert shown == "the model said unsafe S14; replace gave the agent: safe"


def test_show_reads_a_reply_only_from_a_real_blobs_folder_and_by_its_hash(tmp_path: Path) -> None:
    gate = Gate(replace())
    ask(guard(Provider(gate.events)))
    (tmp_path / "real").mkdir()
    real = saved(gate, tmp_path / "real")
    sha = gate.line["result"]["sha256"]
    # A blobs folder that is a link to another folder is not read.
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / "blobs").symlink_to(real / "blobs")
    assert "the model said" not in show.came_back(gate.line, tmp_path / "linked")
    # Nor is an item that is a link.
    (tmp_path / "item" / "blobs").mkdir(parents=True)
    (tmp_path / "item" / "blobs" / sha).symlink_to(real / "blobs" / sha)
    assert "the model said" not in show.came_back(gate.line, tmp_path / "item")
    # Nor an item whose content is not the one its name is the hash of.
    (tmp_path / "other" / "blobs").mkdir(parents=True)
    swapped = gate.result().replace("unsafe", "sneaky")
    (tmp_path / "other" / "blobs" / sha).write_text(swapped)
    assert "the model said" not in show.came_back(gate.line, tmp_path / "other")
    assert show.came_back(gate.line, real).startswith("the model said unsafe S14")


@pytest.mark.slow
def test_show_reads_what_the_model_said_from_a_real_shaped_reply(
    project: Project, fake_openai: Any
) -> None:
    """A real completion is pretty-printed, so a line's preview ends before its text: show
    reads the text from the reply the line points to."""
    fake: FakeOpenAI = fake_openai(["rail_real_shape"])
    raw = (Path(__file__).parent / "fixtures" / "openai" / "rail_real_shape.json").read_text()
    assert len(raw) > outcomes.PREVIEW and '"content"' not in raw[: outcomes.PREVIEW]
    (project.root / "bot.py").write_text(
        "import openai\n\n\ndef answer(question):\n"
        "    reply = openai.OpenAI(max_retries=0).chat.completions.create(\n"
        '        model="gpt-4.1-mini", messages=[{"role": "user", "content": question}]\n'
        "    )\n    return reply.choices[0].message.content\n"
    )
    project.toml.write_text(
        '[agent]\nfunction = "bot:answer"\nmax_cost_usd = "none"\n'
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n\n'
        '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["No"]\n\n'
        '[[test.fault]]\ncall = "model"\nmodel = "gpt-4.1-mini"\ndo = "replace"\n'
        'text = "maybe"\nexpect = ["maybe"]\n'
    )
    result = project.test(fake)
    assert result.code == 0, result.out + result.err
    run = str(next(r for r in result.records() if r[-1]["row"] == "fault 1")[-1]["run"])
    (record,) = (project.root / "scrimmage-runs").glob(f"*/{run}.jsonl")
    folder = record.parent
    project.capsys.readouterr()
    assert main(["show", str(folder), "--run", run]) == 0
    out = project.capsys.readouterr().out
    assert "the model said No; replace gave the agent: maybe" in out, out
    assert "chatcmpl-" not in out


@pytest.mark.slow
def test_record_never_runs_a_carried_model_fault(project: Project, fake_openai: Any) -> None:
    fake: FakeOpenAI = fake_openai(["chat_answer"])
    (project.root / "bot.py").write_text(
        "import openai\n\n\ndef answer(question):\n"
        "    reply = openai.OpenAI(max_retries=0).chat.completions.create(\n"
        '        model="gpt-4o-mini", messages=[{"role": "user", "content": question}]\n'
        "    )\n    return reply.choices[0].message.content\n"
    )
    project.toml.write_text(
        '[agent]\nfunction = "bot:answer"\nmax_cost_usd = "none"\n'
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n\n'
        '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["Hello"]\n\n'
        '[[test.fault]]\ncall = "model"\nmodel = "gpt-4o-mini"\ndo = "replace"\n'
        'text = "INJECTED"\nexpect = ["x"]\n\n'
        '[[test.fault]]\ncall = "model"\ndo = "slow 30s"\nexpect = ["x"]\n'
    )
    project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    project.capsys.readouterr()
    main(["record", str(project.toml), "--json"])
    doc = json.loads(project.capsys.readouterr().out)
    assert doc["command"] == "record"
    files = list((project.root / "scrimmage-runs").glob("*/*.jsonl"))
    lines = [json.loads(x) for f in files for x in f.read_text().splitlines()]
    models = [x for x in lines if x.get("kind") == "model"]
    assert [(x["action"], "under" in x, "changed" in x) for x in models] == [("real", False, False)]
    assert all(x.get("row", "baseline") == "baseline" for x in lines)


def test_replace_never_reaches_the_status_parse() -> None:
    """A carried fault goes the real path; the fault builder, which reads a status from the
    fault word, is for timeout and status alone and would stop a replace."""
    gate = Gate(replace())
    assert ask(guard(Provider(gate.events))) == "safe"
    assert gate.events == ["reserve", "sent"]
