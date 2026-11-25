"""An agent written with the real anthropic library, run by `scrimmage test` against the fake
Anthropic server: a baseline row and a `status 529` row, plain and streamed."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import Project, Result
from fakes.anthropic_server import FakeAnthropic
from scrimmage.budget import Ledger

Fake = Callable[..., FakeAnthropic]

# A fake key in Anthropic's shape, written in parts so the commit guard, which looks for
# real keys, lets it in.
KEY = "sk-ant-" + "api03-FAKE-scrimmage-e1-key-0123456789abcdef"
MODEL = "claude-haiku-5-5"
INPUT, OUTPUT = 1.0, 5.0  # dollars per million tokens: a team's own price, not Anthropic's
TOLERANCE = 1e-8  # as tests/test_anthropic_billing.py

AGENT = '''"""A one-tool agent over the anthropic library, with its default retries."""

import anthropic

WEATHER = {
    "name": "get_weather",
    "description": "The weather in a city.",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
}


def get_weather(city):
    return "Sunny, 22 C"


def ask(client, messages, stream):
    args = dict(model="claude-haiku-5-5", max_tokens=256, messages=messages, tools=[WEATHER])
    if stream:
        with client.messages.stream(**args) as events:
            return events.get_final_message()
    return client.messages.create(**args)


def run(question, stream):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]
    try:
        for _ in range(4):
            reply = ask(client, messages, stream)
            calls = [b for b in reply.content if b.type == "tool_use"]
            if not calls:
                return "".join(b.text for b in reply.content if b.type == "text")
            messages.append(
                {"role": "assistant", "content": [b.model_dump() for b in reply.content]}
            )
            results = [
                {"type": "tool_result", "tool_use_id": c.id, "content": get_weather(**c.input)}
                for c in calls
            ]
            messages.append({"role": "user", "content": results})
    except anthropic.APIStatusError as error:
        return f"the model failed: {type(error).__name__}"
    return "no answer"


def plain(question):
    return run(question, False)


def streamed(question):
    return run(question, True)
'''

TOML = f"""[agent]
function = "claude_bot:FUNCTION"
tools = ["claude_bot:get_weather"]
runs = 2
max_cost_usd = 1.0
model_hosts = [{{host = "127.0.0.1", provider = "anthropic"}}]

[agent.prices]
{MODEL} = {{input = {INPUT}, output = {OUTPUT}}}

[[test]]
name = "weather"
question = "What is the weather in Paris?"
expect = ["Hello"]

[[test.mock]]
call = "tool:get_weather"
returns = "Sunny, 22 C"

[[test.fault]]
call = "model"
do = "status 529"
when = "first 3"
expect = ["OverloadedError"]
"""

MODES = {
    # the answer fixture is from another conversation: only its usage and text are used
    "plain": ["real_tool_use", "real_answer"],
    "streamed": ["real_stream_tool_use", "real_stream_answer"],
}


def run_agent(project: Project, fake: FakeAnthropic, function: str) -> Result:
    (project.root / "claude_bot.py").write_text(AGENT)
    project.toml.write_text(TOML.replace("FUNCTION", function))
    project.monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.base_url)
    project.monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    return project.test()


FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"


def dollars(name: str) -> float:
    """What a saved reply's usage costs at the test's price, worked out here by hand.

    A stream's input count is in its first event and its output count in its last."""
    stream = (FIXTURES / f"{name}.sse").exists()
    text = (FIXTURES / f"{name}.{'sse' if stream else 'json'}").read_text()
    if stream:
        events = [json.loads(m) for m in re.findall(r"^data: (.*)$", text, re.MULTILINE)]
        start = next(e for e in events if e["type"] == "message_start")["message"]["usage"]
        last = [e for e in events if e["type"] == "message_delta"][-1]["usage"]
        tokens_in, tokens_out = start["input_tokens"], last["output_tokens"]
    else:
        usage = json.loads(text)["usage"]
        tokens_in, tokens_out = usage["input_tokens"], usage["output_tokens"]
    return (tokens_in * INPUT + tokens_out * OUTPUT) / 1_000_000


def rows(result: Result, name: str) -> list[list[dict[str, Any]]]:
    return [lines for lines in result.records() if lines[-1]["row"] == name]


def model_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [x for x in lines if x.get("kind") == "model"]


def every_byte(root: Path) -> bytes:
    """What the test left on disk: every record, blob and log."""
    return b"".join(f.read_bytes() for f in (root / "scrimmage-runs").rglob("*") if f.is_file())


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["plain", "streamed"])
def test_an_anthropic_agent_runs_a_baseline_and_a_529_row_it_sees_after_three_attempts(
    project: Project, fake_anthropic: Fake, mode: str
) -> None:
    replies = MODES[mode]
    fake = fake_anthropic(replies)
    result = run_agent(project, fake, mode)
    assert result.code == 0, result.out + result.err

    baseline, faulted = rows(result, "baseline"), rows(result, "fault 1")
    assert len(baseline) == len(faulted) == 2  # runs = 2
    for lines in baseline:
        calls = model_lines(lines)
        assert [(x["call"], x["action"], x["cost_estimated"]) for x in calls] == [
            ("model:anthropic", "real", False)
        ] * 2
        # Each line's cost is its usage times the team's price, worked out by hand.
        assert [x["cost_usd"] for x in calls] == [
            pytest.approx(dollars(name), abs=TOLERANCE) for name in replies
        ]
        assert "Hello" in lines[-1]["summary"]["answer"]
    for lines in faulted:
        calls = model_lines(lines)
        # The library retried twice by itself: first 3 outlasts that, and no more.
        assert [(x["action"], x["fault"]["n"]) for x in calls] == [
            ("fault", 1),
            ("fault", 2),
            ("fault", 3),
        ]
        assert lines[-1]["summary"]["answer"] == "the model failed: OverloadedError"
        assert lines[-1]["summary"]["fault_fired"] is True
    # Only the baseline's four calls reached the server: a faulted attempt sends nothing.
    assert len(fake.requests) == 4


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["plain", "streamed"])
def test_the_budget_settles_the_sum_of_the_lines_costs(
    project: Project, fake_anthropic: Fake, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settled: list[float] = []
    settle = Ledger.settle

    def spy(self: Ledger, reservation: int, usd: float) -> None:
        settled.append(usd)
        settle(self, reservation, usd)

    monkeypatch.setattr(Ledger, "settle", spy)
    result = run_agent(project, fake_anthropic(MODES[mode]), mode)
    assert result.code == 0, result.out + result.err
    lines_total = 0.0
    for lines in result.records():
        by_line = sum(x.get("cost_usd", 0.0) for x in model_lines(lines))
        summary: dict[str, Any] = lines[-1]["summary"]  # type: ignore[assignment]
        assert summary["cost_usd"] == pytest.approx(by_line, abs=TOLERANCE)
        lines_total += by_line
    # What the budget was charged, call by call, is what the lines say the calls cost, and
    # that is the usage of the four replies the server sent, at the team's price.
    assert lines_total > 0
    assert sum(settled) == pytest.approx(lines_total, abs=TOLERANCE)
    assert lines_total == pytest.approx(2 * sum(dollars(n) for n in MODES[mode]), abs=TOLERANCE)


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["plain", "streamed"])
def test_no_record_blob_or_log_holds_the_api_key(
    project: Project, fake_anthropic: Fake, mode: str
) -> None:
    fake = fake_anthropic(MODES[mode])
    result = run_agent(project, fake, mode)
    assert result.code == 0, result.out + result.err
    # The scan means something only if the key was sent: the fake saw it in the header.
    # The library puts the key in a header, as a Bearer token or as x-api-key.
    sent = [{k.lower(): v for k, v in headers.items()} for headers, _ in fake.received]
    assert sent
    for header in sent:
        assert KEY in (header.get("x-api-key"), header.get("authorization", "")[len("Bearer ") :])
    kept = every_byte(project.root)
    assert len(kept) > 1000
    for found in (kept, result.out.encode(), result.err.encode()):
        assert KEY.encode() not in found
        assert b"sk-ant-" not in found
