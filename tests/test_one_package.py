"""Scrimmage installs as one package, `scrimmage`, which imports as `scrimmage_core` and gives
three commands. The docs install that one package and name no other."""

import importlib.metadata
import re
import shlex

from conftest import doc_pages
from test_docs_examples import DOCS, FENCE

COMMANDS = {
    "scrimmage": "scrimmage_core.entry:main",
    "scrimmage-run": "scrimmage_core.run_command:main",
    "scrimmage-mcp": "scrimmage_core.mcp_server:main",
}
# The names that went: the second package and the short command.
GONE = re.compile(r"scrimmage-core|(?<![\w-])scrim(?![\w-])")


def test_one_distribution_installs_and_it_is_named_scrimmage() -> None:
    names = {
        re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower()
        for d in importlib.metadata.distributions()
    }
    assert {n for n in names if n.startswith("scrim")} == {"scrimmage"}
    dist = importlib.metadata.distribution("scrimmage")
    assert dist.version == "0.1.0.dev0"  # above the 0.0.0 placeholder on PyPI
    # Only the extras need anything: the package itself depends on nothing.
    needs = dist.requires or []
    assert needs and all("extra ==" in need for need in needs), needs


def test_the_installed_commands_are_scrimmage_scrimmage_run_and_scrimmage_mcp() -> None:
    dist = importlib.metadata.distribution("scrimmage")
    scripts = {e.name: e.value for e in dist.entry_points if e.group == "console_scripts"}
    assert scripts == COMMANDS
    assert {e.group for e in dist.entry_points} == {"console_scripts"}


def test_the_install_page_names_one_package_and_no_scrim() -> None:
    text = (DOCS / "install.md").read_text()
    installs = [
        shlex.split(line)
        for lang, _, body in FENCE.findall(text)
        if lang == "bash"
        for line in body.splitlines()
        if re.match(r"(uv )?pip install ", line)
    ]
    assert len(installs) == 3, installs  # pip, uv, and a wheel with uv
    for words in installs:
        named = [w for w in words[words.index("install") + 1 :] if not w.startswith("-")]
        if "--find-links" in words:
            named.remove(words[words.index("--find-links") + 1])
        assert named in (["./packages/scrimmage"], ["scrimmage"]), words
    assert not GONE.search(text), GONE.findall(text)


def test_no_page_names_the_old_package_or_the_short_command() -> None:
    # The changelog is history: it says what each name was.
    pages = [p for p in doc_pages(DOCS) if p.name != "changelog.md"]
    assert len(pages) > 10
    found = {str(p.relative_to(DOCS)): GONE.findall(p.read_text()) for p in pages}
    assert {page: names for page, names in found.items() if names} == {}
    assert GONE.search("pip install scrimmage-core") and GONE.search("`scrim test`")
    assert not GONE.search("scrimmage-run, scrimmage, scrimmage_core, scrimmage-runs")


def test_no_docs_command_installs_scrimmage_from_the_index() -> None:
    # PyPI holds a 0.0.0 placeholder named scrimmage, and pip prefers it to our dev release.
    # So a command installs Scrimmage from a path, or from a wheel folder with --no-index.
    installs = [
        (str(page.relative_to(DOCS)), shlex.split(line))
        for page in doc_pages(DOCS)
        if page.name != "changelog.md"
        for lang, _, body in FENCE.findall(page.read_text())
        if lang == "bash"
        for line in body.splitlines()
        if re.match(r"(uv )?pip install ", line)
    ]
    assert len(installs) >= 5, installs
    from_index = [
        (page, words)
        for page, words in installs
        if "--no-index" not in words
        and any(re.match(r"scrimmage(\[[\w,]*\])?($|[<>=!~ ])", w) for w in words[2:])
    ]
    assert from_index == []


def test_the_package_ships_the_repo_license_under_apache_2() -> None:
    meta = importlib.metadata.metadata("scrimmage")
    assert meta["License-Expression"] == "Apache-2.0"
    # The wheel takes LICENSE from the package folder: a copy of the repo's, kept the same.
    root = DOCS.parent
    shipped = (root / "packages" / "scrimmage" / "LICENSE").read_bytes()
    assert shipped == (root / "LICENSE").read_bytes()
    assert shipped.lstrip().startswith(b"Apache License\n")
