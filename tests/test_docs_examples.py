"""Every example in the docs runs, and prints what its page shows.

A page's code blocks with a title are its files: `title="shop/agent.py"` writes that file,
and a `diff` block changes the file it names. A block titled "Output" is what
the command in the block before it prints, `scrimmage test` or `scrimmage check`, so each
one runs that command and compares, line by line.
The model is the fake OpenAI server, scripted per page below, so no key is needed.
"""

import contextlib
import os
import re
import shutil
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from conftest import ACME, EXAMPLE, doc_pages
from fakes.openai_server import FakeOpenAI, Guard, Reply, Rule, text_of, tool_failed
from scrimmage.cli import main
from scrimmage.managed import Managed
from scrimmage.outcomes import MISTAKE, OK, ROW_FAILED
from test_managed_start import group

DOCS = Path(__file__).parents[1] / "docs"
OUTPUT = "Output"
FENCE = re.compile(r'^```(\w+)(?: title="([^"]+)")?\n(.*?)^```\n', re.M | re.S)
# The fake model server listens on 127.0.0.1, which a team would name as its gateway.
FAKE_HOST = 'model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n'


@dataclass(frozen=True)
class Example:
    replies: list[str]  # the fake model's reply for each turn of a conversation
    codes: list[int]  # the exit code of each Output block, in order
    start: Path | None = None  # a folder the page starts from, or none
    base: str | None = None  # a page whose files this page starts from
    by_model: dict[str, Rule] | None = None  # models answered by what they are sent
    logs: dict[int, str] | None = None  # an Output's number -> text one of its run logs holds


@dataclass(frozen=True)
class ByTools:
    """A main model that follows the script of the tool its request offers.

    A script lists the reply for each turn, `a|b` as in the fake server's replies. A page
    with two agents on one model name gives each agent its own script this way: a request
    that offers the tool of exactly one script gets that script, and any other request gets
    no reply, which the fake records as an error. `needs` maps a script's tool and a turn
    to a text some tool message of the request must hold, or that turn gets no reply.
    `unused` names the script entries no request asked for."""

    scripts: dict[str, tuple[str, ...]]
    needs: dict[tuple[str, int], str] = field(default_factory=dict)
    used: set[tuple[str, int]] = field(default_factory=set)

    def __post_init__(self) -> None:
        for turns in self.scripts.values():
            for name in turns:
                Reply(name)  # a missing fixture fails before the test runs

    @property
    def unused(self) -> list[tuple[str, int]]:
        every = {(tool, turn) for tool, turns in self.scripts.items() for turn in range(len(turns))}
        return sorted(every - self.used)

    def __call__(self, request: dict[str, Any]) -> Reply | None:
        offered = {str(t.get("function", {}).get("name")) for t in request.get("tools", [])}
        matching = [tool for tool in self.scripts if tool in offered]
        messages = request.get("messages", [])
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        if len(matching) != 1 or turn >= len(self.scripts[matching[0]]):
            return None
        tool = matching[0]
        wanted = self.needs.get((tool, turn))
        if wanted and not any(wanted in text_of(m) for m in messages if m.get("role") == "tool"):
            return None
        self.used.add((tool, turn))
        reply = Reply(self.scripts[tool][turn])
        return reply.after_failure if reply.after_failure and tool_failed(request) else reply


# The bot asks for the order, then answers, or says it could not if the tool failed.
ONE_TOOL = ["acme_get_order", "acme_answer|acme_admits"]
EXAMPLES = {
    "quickstart.md": Example(ACME, [OK, OK, ROW_FAILED, OK], start=EXAMPLE),
    "examples/function-agent.md": Example(ONE_TOOL, [OK]),
    "examples/script-agent.md": Example(ONE_TOOL, [MISTAKE, OK], base="examples/function-agent.md"),
    "examples/langgraph.md": Example(ONE_TOOL, [ROW_FAILED, OK]),
    "examples/mcp.md": Example(["docs_search", "docs_answer|docs_admits"], [OK]),
    "examples/unknown-tool.md": Example(
        [],
        [ROW_FAILED, OK, MISTAKE, OK],
        base="examples/function-agent.md",
        logs={1: "KeyError: 'fetch'"},
        by_model={
            "gpt-4o-mini": ByTools(
                {
                    "search": ("docs_search", "docs_answer", "docs_answer"),
                    "get_order": ("acme_get_order", "acme_answer", "acme_answer"),
                },
                # The model's last reply follows the bot's own error: the bot must have
                # re-listed its tools, so the fault named fetch, and then answered with it.
                needs={
                    ("search", 2): "no tool named fetch",
                    ("get_order", 2): "no tool named delete_account",
                },
            )
        },
    ),
    "examples/web-request.md": Example(ONE_TOOL, [OK], base="examples/function-agent.md"),
    "examples/judging.md": Example(ONE_TOOL, [ROW_FAILED, OK], base="examples/function-agent.md"),
    "examples/one-model.md": Example(ONE_TOOL, [ROW_FAILED, OK], base="examples/function-agent.md"),
}


@dataclass(frozen=True)
class Block:
    lang: str
    title: str
    body: str


def blocks(name: str) -> list[Block]:
    text = (DOCS / name).read_text()
    return [Block(lang, title, body) for lang, title, body in FENCE.findall(text) if title]


def normal(text: str) -> list[str]:
    """The output with the parts that change on every run, the folder and run ids, masked."""
    text = re.sub(r"\S*scrimmage-runs/[0-9A-Za-z-]+", "scrimmage-runs/<time>", text)
    return re.sub(r"run-[0-9a-f]{12}", "run-<id>", text).splitlines()


def apply(root: Path, block: Block) -> None:
    path = root / block.title
    if block.lang != "diff":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block.body)
        return
    lines = block.body.splitlines()
    old = "\n".join(line[1:] for line in lines if line[:1] in "- ")
    new = "\n".join(line[1:] for line in lines if line[:1] in "+ ")
    text = path.read_text()
    if not old.strip():
        path.write_text(text.rstrip("\n") + "\n" + new + "\n")
        return
    assert text.count(old) == 1, f"{block.title} does not hold, once:\n{old}"
    path.write_text(text.replace(old, new))


def with_fake_host(toml: Path) -> None:
    text = toml.read_text()
    if not re.search(r"^model_hosts", text, re.M):  # a commented line names nothing
        toml.write_text(text.replace("[agent]\n", "[agent]\n" + FAKE_HOST, 1))


def check_page(
    name: str,
    example: Example,
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    if example.start:
        shutil.copytree(example.start, root, ignore=shutil.ignore_patterns("__pycache__"))
    root.mkdir(exist_ok=True)
    for block in blocks(example.base) if example.base else []:
        if block.title != OUTPUT:
            apply(root, block)
    fake = fake_openai(example.replies, by_model=example.by_model)
    monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    # A page runs `python`: the venv's, whatever else PATH holds.
    monkeypatch.setenv(
        "PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    monkeypatch.chdir(root)
    codes: list[int] = []
    run_blocks(name, root, codes, capsys, example.logs or {})
    assert codes == example.codes
    for rule in (example.by_model or {}).values():
        assert not isinstance(rule, ByTools) or not rule.unused, (
            f"script entries never asked for: {rule.unused}"
        )


def run_blocks(
    name: str,
    root: Path,
    codes: list[int],
    capsys: pytest.CaptureFixture[str],
    logs: dict[int, str],
) -> None:
    """Each block of the page in order: a file is written, an Output is the command run.

    `logs` names text that a run log of an Output's records must hold, such as the error
    that crashed the agent, so the page's crash is the one it says and not another."""
    command: list[str] = []
    for lang, title, body in FENCE.findall((DOCS / name).read_text()):
        if lang == "bash":
            shown = [line for line in body.splitlines() if line.startswith("scrimmage ")]
            command[:] = shown[-1].split()[1:] if shown else command
            continue
        if not title:
            continue
        block = Block(lang, title, body)
        if block.title != OUTPUT:
            apply(root, block)
            continue
        with_fake_host(root / "scrimmage.toml")
        capsys.readouterr()
        codes.append(main(command))
        out = capsys.readouterr().out
        assert normal(out) == normal(block.body), f"{name}, Output {len(codes)}:\n{out}"
        if len(codes) in logs:
            folder = Path(re.search(r"records in (\S+)", out).group(1))  # type: ignore[union-attr]
            held = [log.read_text() for log in folder.glob("run-*.log")]
            assert any(logs[len(codes)] in text for text in held), (
                f"{name}, Output {len(codes)}: no run log holds {logs[len(codes)]!r}"
            )


@pytest.mark.design("docs.examples_run")
@pytest.mark.parametrize("name", sorted(EXAMPLES))
@pytest.mark.slow
def test_the_page_runs_and_prints_what_it_shows(
    name: str,
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_page(name, EXAMPLES[name], tmp_path, fake_openai, capsys, monkeypatch)


# The service page: Scrimmage starts the service with [agent] start, so the page has no line
# of its own to start it, and no process of it is left after the test.
SERVICE = "examples/service-agent.md"
SERVICE_RUN = Example(ONE_TOOL, [OK, OK], base="examples/function-agent.md")


@pytest.mark.slow
def test_the_example_page_runs_with_a_managed_start(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in list(os.environ):
        if name.startswith("SCRIMMAGE_"):
            monkeypatch.delenv(name)
    copies: list[Managed] = []
    start = Managed.start

    def started(copy: Managed, control_url: str) -> None:
        copies.append(copy)
        start(copy, control_url)

    monkeypatch.setattr(Managed, "start", started)
    try:
        check_page(SERVICE, SERVICE_RUN, tmp_path, fake_openai, capsys, monkeypatch)
    finally:
        for copy in copies:  # a test that failed part way leaves nothing running
            if copy.proc is not None and group(copy.proc.pid):
                with contextlib.suppress(ProcessLookupError):  # it can end after the check
                    os.killpg(copy.proc.pid, signal.SIGKILL)
    # The test started its copy, and only the test: every shell line is a scrimmage command,
    # so the page sets no variable and makes no token file.
    shell = [
        line
        for lang, _, body in FENCE.findall((DOCS / SERVICE).read_text())
        if lang == "bash"
        for line in body.splitlines()
        if line.strip() and not line.startswith("scrimmage ")
    ]
    assert shell == []
    assert not list((tmp_path / "project").glob(".scrimmage-token*"))
    assert len(copies) == 1  # scrimmage test's; scrimmage check starts none without --service
    proc = copies[0].proc
    assert proc is not None and proc.returncode is not None
    assert group(proc.pid) == [], "a process of the service was left running"


# The page on changing a result starts from the function agent. The fake model obeys the
# note the fault adds, as "a@b" says, and the test row fails on the word it was told to say.
CHANGING = "changing-results.md"
CHANGING_RUN = Example(
    ["acme_get_order", "acme_answer@acme_obeys"], [ROW_FAILED], base="examples/function-agent.md"
)


@pytest.mark.design("docs.changing_results_runs")
@pytest.mark.slow
def test_the_changing_results_page_runs(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_page(CHANGING, CHANGING_RUN, tmp_path, fake_openai, capsys, monkeypatch)


# The guardrails page starts from the function agent too. The fake guard model says unsafe
# when what it is sent holds the injected note, and safe otherwise.
GUARDRAILS = "guardrails.md"
GUARDRAILS_RUN = Example(
    ONE_TOOL,
    [OK],
    base="examples/function-agent.md",
    by_model={"llama-guard-4": Guard("Note to the assistant")},
)


@pytest.mark.slow
def test_the_guardrails_page_runs_and_prints_its_table(
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_page(GUARDRAILS, GUARDRAILS_RUN, tmp_path, fake_openai, capsys, monkeypatch)


def test_every_page_with_an_output_is_run() -> None:
    pages = {
        str(p.relative_to(DOCS))
        for p in doc_pages(DOCS)
        if any(b.title == OUTPUT for b in blocks(str(p.relative_to(DOCS))))
    }
    assert pages == {*EXAMPLES, SERVICE, CHANGING, GUARDRAILS}


def test_the_landing_page_shows_the_function_agent_as_it_runs() -> None:
    """The home page's example is not run itself, so it must be the function agent's, unchanged."""
    shown = {lang: body for lang, _, body in FENCE.findall((DOCS / "index.md").read_text())}
    page = {b.lang: b.body for b in blocks("examples/function-agent.md")}
    output = next(b.body for b in blocks("examples/function-agent.md") if b.title == OUTPUT)
    assert shown["toml"].removeprefix("# scrimmage.toml\n") == page["toml"]
    assert shown["text"] == output
