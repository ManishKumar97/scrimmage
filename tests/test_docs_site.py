"""The Schemas page is built from every schema that ships, and says what each key holds."""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from conftest import EXAMPLE, doc_pages
from test_docs_examples import DOCS, EXAMPLES, FENCE, OUTPUT

ROOT = Path(__file__).parents[1]


def hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("schemas_hook", ROOT / "docs/hooks/schemas.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.design("docs.schemas_generated")
def test_every_shipped_schema_is_on_the_page_with_every_table() -> None:
    schemas = hook()
    files = schemas.shipped()
    shipped = ROOT / "packages/scrimmage-core/src/scrimmage_core/schemas"
    assert {p.name for p in files} == {p.name for p in shipped.glob("*.json")}
    page = schemas.render(files)
    for path in files:
        schema = json.loads(path.read_text())
        renderer = schemas.Renderer(path.name, schema)
        assert f"## {schema['title']}" in page
        assert f"({schemas.PUBLISHED}/{path.name})" in page
        for name, definition in schema.get("$defs", {}).items():
            if renderer.is_table(definition):
                assert f"{{#{renderer.anchor(name)}}}" in page, name
    assert schemas.MARKER in (ROOT / "docs" / schemas.PAGE).read_text()


def test_no_key_on_the_page_has_an_empty_value_column() -> None:
    page = hook().render(hook().shipped())
    rows = re.findall(r"<tr><td><code>([^<]+)</code></td><td>(.*?)</td>", page)
    assert rows
    assert [key for key, kind in rows if not kind] == []


def fences(name: str) -> list[tuple[str, str, str]]:
    """Every code block of a page: its language, its title and its text."""
    return FENCE.findall((DOCS / name).read_text())


@pytest.mark.design("docs.citations_exist")
def test_every_test_a_page_cites_exists() -> None:
    """A "Checked by" line, or a command, names tests/<file>.py::<test>; each must be real."""
    cited = {
        (page, path, name)
        for page in (str(p.relative_to(DOCS)) for p in doc_pages(DOCS))
        for path, name in re.findall(r"(tests/\w+\.py)::(\w+)", (DOCS / page).read_text())
    }
    assert len({c for c in cited if c[0] == "security.md"}) >= 20
    missing = [c for c in cited if f"def {c[2]}(" not in (ROOT / c[1]).read_text()]
    assert missing == []


def test_every_security_statement_says_what_checks_it() -> None:
    """Each section that states a guarantee ends its statements with a "Checked by" line."""
    page = (DOCS / "security.md").read_text()
    guarantees = page.split("## What Scrimmage does not protect against")[0]
    for section in guarantees.split("\n## ")[1:]:
        assert "*Checked by: `tests/" in section, section.splitlines()[0]


def test_the_quickstart_shows_the_example_as_it_is() -> None:
    blocks = fences("quickstart.md")
    untitled = {lang: body for lang, title, body in blocks if not title}
    example = EXAMPLE / "scrimmage.toml"
    shown = [line for line in example.read_text().splitlines() if "model_hosts" not in line]
    shown = [line for line in shown if "fake model server in the tests" not in line]
    assert untitled["toml"].splitlines() == shown

    agent = (EXAMPLE / "acme" / "agent.py").read_text().splitlines()
    snippet = [line for line in untitled["python"].splitlines() if line.strip() not in ("", "...")]
    assert snippet[0].startswith("# acme/agent.py")
    assert [line for line in snippet[1:] if line not in agent] == []

    # Each key a record line shows is a key the record schema allows, and real records are
    # checked against that schema in tests/test_schemas.py.
    schema = json.loads(
        (ROOT / "packages/scrimmage-core/src/scrimmage_core/schemas/record.v1.json").read_text()
    )
    defs = schema["$defs"]
    call_keys = set(defs["call"]["properties"])
    summary_keys = set(defs["summary"]["properties"]["summary"]["properties"])
    for line in untitled["json"].splitlines():
        shown_line = json.loads(re.sub(r",\s*\.\.\.\}", "}", line))
        if "summary" in shown_line:
            assert set(shown_line["summary"]) <= summary_keys, shown_line
        else:
            assert set(shown_line) <= call_keys, shown_line


def test_every_scrimmage_command_on_a_run_page_is_the_one_the_harness_runs() -> None:
    for name in EXAMPLES:
        blocks = fences(name)
        for i, (lang, title, body) in enumerate(blocks):
            if lang == "bash":
                commands = [line for line in body.splitlines() if line.startswith("scrimmage")]
                assert commands in ([], ["scrimmage test"], ["scrimmage check"]), (name, body)
            if title == OUTPUT:
                assert blocks[i - 1][0] == "bash", (name, i)
                assert blocks[i - 1][2] in ("scrimmage test\n", "scrimmage check\n"), (name, i)


def test_the_quickstart_installs_as_the_install_page_says() -> None:
    quick = fences("quickstart.md")[0][2].splitlines()
    install = [
        line
        for lang, _, body in fences("install.md")
        if lang == "bash"
        for line in body.splitlines()
    ]
    assert "pip install ./packages/scrimmage ./packages/scrimmage-core" in install
    assert "pip install ./packages/scrimmage ./packages/scrimmage-core openai" in quick
    for line in ("python -m venv .venv", ". .venv/bin/activate"):
        assert line in install and line in quick


@pytest.mark.slow
def test_the_maintainers_notes_folder_is_not_built_into_the_site(tmp_path: Path) -> None:
    """A private/ folder under docs/ is left out of the site: no page, no word of it."""
    pytest.importorskip("mkdocs")
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "docs", docs, ignore=shutil.ignore_patterns("private"))
    (docs / "private").mkdir()
    (docs / "private" / "secret.md").write_text("# Notes\n\nzebracrossing12345\n")
    config = (ROOT / "mkdocs.yml").read_text()
    config = config.replace("docs_dir: docs", f"docs_dir: {docs}")
    config = config.replace("site_dir: site", f"site_dir: {tmp_path / 'site'}")
    config = config.replace("docs/hooks/schemas.py", str(ROOT / "docs/hooks/schemas.py"))
    (tmp_path / "mkdocs.yml").write_text(config)
    run = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-f", str(tmp_path / "mkdocs.yml")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert run.returncode == 0, run.stderr
    built = [p for p in (tmp_path / "site").rglob("*") if p.is_file()]
    assert built
    assert [p for p in built if "private" in p.relative_to(tmp_path / "site").parts] == []
    assert [p for p in built if b"zebracrossing12345" in p.read_bytes()] == []
