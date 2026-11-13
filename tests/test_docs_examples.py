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
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import ACME, EXAMPLE, doc_pages
from fakes.openai_server import FakeOpenAI, Guard, Rule
from ports import free_port
from scrimmage_core.cli import main
from scrimmage_core.outcomes import MISTAKE, OK, ROW_FAILED

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
    service: bool = False  # the page starts a service with its own line before its test


# The bot asks for the order, then answers, or says it could not if the tool failed.
ONE_TOOL = ["acme_get_order", "acme_answer|acme_admits"]
EXAMPLES = {
    "quickstart.md": Example(ACME, [OK, OK, ROW_FAILED, OK], start=EXAMPLE),
    "examples/function-agent.md": Example(ONE_TOOL, [OK]),
    "examples/script-agent.md": Example(ONE_TOOL, [MISTAKE, OK], base="examples/function-agent.md"),
    "examples/langgraph.md": Example(ONE_TOOL, [ROW_FAILED, OK]),
    "examples/mcp.md": Example(["docs_search", "docs_answer|docs_admits"], [OK]),
    "examples/web-request.md": Example(ONE_TOOL, [OK], base="examples/function-agent.md"),
    "examples/judging.md": Example(ONE_TOOL, [ROW_FAILED, OK], base="examples/function-agent.md"),
    "examples/one-model.md": Example(ONE_TOOL, [ROW_FAILED, OK], base="examples/function-agent.md"),
    "examples/service-agent.md": Example(
        ONE_TOOL, [OK, OK], base="examples/function-agent.md", service=True
    ),
}
PAGE_PORT = "127.0.0.1:8765"  # the service address the page shows, moved to a free port


def start_line(name: str) -> tuple[dict[str, str], list[str]]:
    """The page's own start line, as written: the variables set before scrimmage-run, and
    the command from scrimmage-run on, without a trailing &."""
    for lang, _, body in FENCE.findall((DOCS / name).read_text()):
        if lang != "bash" or "scrimmage-run " not in body:
            continue
        joined = body.replace("\\\n", " ")
        line = next(x for x in joined.splitlines() if "scrimmage-run " in x)
        words = [w for w in shlex.split(line) if w != "&"]
        at = words.index("scrimmage-run")
        env = dict(w.split("=", 1) for w in words[:at])
        return env, words[at:]
    raise AssertionError(f"{name} shows no scrimmage-run line")


@contextlib.contextmanager
def served(root: Path, page: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The page's service, started with the page's own line, on a free port, until the page
    ends. The process and every worker it started are gone, by pid, before this returns."""
    token = root / ".scrimmage-token"
    token.write_text(secrets.token_hex(32))
    token.chmod(0o600)
    control = f"http://127.0.0.1:{free_port()}"
    port = free_port()
    toml = root / "scrimmage.toml"
    toml.write_text(toml.read_text().replace(PAGE_PORT, f"127.0.0.1:{port}"))
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(token))
    monkeypatch.setenv("SCRIMMAGE_CONTROL_URL", control)
    bin_dir = Path(sys.executable).parent
    shown, words = start_line(page)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    env.update(shown)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        SCRIMMAGE_TOKEN_FILE=str(token),  # the page's file and address, here on free ports
        SCRIMMAGE_CONTROL_URL=control,
    )
    command = [str(bin_dir / words[0]), str(bin_dir / words[1]), *words[2:]]
    command[command.index(PAGE_PORT.rpartition(":")[2])] = str(port)
    proc = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready, said = threading.Event(), []

    def read() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            said.append(line)
            if "Application startup complete" in line:
                ready.set()
        ready.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert ready.wait(60) and proc.poll() is None, "".join(said)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(10)
        if proc.stderr is not None:
            proc.stderr.close()
        with pytest.raises(ProcessLookupError):  # the service is gone
            os.kill(proc.pid, 0)


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
    command: list[str] = []
    with contextlib.ExitStack() as stack:
        run_blocks(name, example, root, codes, command, stack, monkeypatch, capsys)
    assert codes == example.codes


def run_blocks(
    name: str,
    example: Example,
    root: Path,
    codes: list[int],
    command: list[str],
    stack: contextlib.ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each block of the page in order: a file is written, an Output is the command run."""
    started = False
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
        if example.service and command[:1] == ["test"] and not started:
            stack.enter_context(served(root, name, monkeypatch))
            started = True
        capsys.readouterr()
        codes.append(main(command))
        out = capsys.readouterr().out
        assert normal(out) == normal(block.body), f"{name}, Output {len(codes)}:\n{out}"


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
    assert pages == {*EXAMPLES, CHANGING, GUARDRAILS}


def test_the_landing_page_shows_the_function_agent_as_it_runs() -> None:
    """The home page's example is not run itself, so it must be the function agent's, unchanged."""
    shown = {lang: body for lang, _, body in FENCE.findall((DOCS / "index.md").read_text())}
    page = {b.lang: b.body for b in blocks("examples/function-agent.md")}
    output = next(b.body for b in blocks("examples/function-agent.md") if b.title == OUTPUT)
    assert shown["toml"].removeprefix("# scrimmage.toml\n") == page["toml"]
    assert shown["text"] == output
