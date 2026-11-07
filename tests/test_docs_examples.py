"""Every example in the docs runs, and prints what its page shows.

A page's code blocks with a title are its files: `title="shop/agent.py"` writes that file,
and a `diff` block changes the file it names. A block titled "Output" is what
`scrimmage test` prints at that point, so each one runs the test and compares, line by line.
The model is the fake OpenAI server, scripted per page below, so no key is needed.
"""

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import ACME, EXAMPLE
from scrimmage_core.cli import main
from scrimmage_core.runner import MISTAKE, OK, ROW_FAILED
from fakes.openai_server import FakeOpenAI

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


# The bot asks for the order, then answers, or says it could not if the tool failed.
ONE_TOOL = ["acme_get_order", "acme_answer|acme_admits"]
EXAMPLES = {
    "quickstart.md": Example(ACME, [OK, ROW_FAILED, OK], start=EXAMPLE),
    "examples/function-agent.md": Example(ONE_TOOL, [OK]),
    "examples/script-agent.md": Example(ONE_TOOL, [MISTAKE, OK], base="examples/function-agent.md"),
    "examples/langgraph.md": Example(ONE_TOOL, [ROW_FAILED, OK]),
    "examples/mcp.md": Example(["docs_search", "docs_answer|docs_admits"], [OK]),
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
    if "model_hosts" not in text:
        toml.write_text(text.replace("[agent]\n", "[agent]\n" + FAKE_HOST, 1))


@pytest.mark.design("docs.examples_run")
@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_the_page_runs_and_prints_what_it_shows(
    name: str,
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = EXAMPLES[name]
    root = tmp_path / "project"
    if example.start:
        shutil.copytree(example.start, root, ignore=shutil.ignore_patterns("__pycache__"))
    root.mkdir(exist_ok=True)
    for block in blocks(example.base) if example.base else []:
        if block.title != OUTPUT:
            apply(root, block)
    fake = fake_openai(example.replies)
    monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.chdir(root)
    codes: list[int] = []
    for block in blocks(name):
        if block.title != OUTPUT:
            apply(root, block)
            continue
        with_fake_host(root / "scrimmage.toml")
        capsys.readouterr()
        codes.append(main(["test"]))
        out = capsys.readouterr().out
        assert normal(out) == normal(block.body), f"{name}, Output {len(codes)}:\n{out}"
    assert codes == example.codes


def test_every_page_with_an_output_is_run() -> None:
    pages = {
        str(p.relative_to(DOCS))
        for p in DOCS.rglob("*.md")
        if any(b.title == OUTPUT for b in blocks(str(p.relative_to(DOCS))))
    }
    assert pages == set(EXAMPLES)

