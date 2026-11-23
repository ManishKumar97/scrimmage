"""Each Anthropic fixture is the file SOURCE.md says it is: a hand edit changes its hash."""

import hashlib
import re
from pathlib import Path

DIRECTORY = Path(__file__).parent / "fixtures" / "anthropic"
ROW = re.compile(r"^\| `(?P<file>[^`]+)` \|.*\| `(?P<sha>[0-9a-f]{64})` \|$")


def listed() -> dict[str, str]:
    found: dict[str, str] = {}
    for line in (DIRECTORY / "SOURCE.md").read_text().splitlines():
        row = ROW.match(line)
        if row:
            assert row["file"] not in found, f"{row['file']} is listed twice"
            found[row["file"]] = row["sha"]
    return found


def test_every_fixture_has_a_sha256_line_and_the_hash_matches() -> None:
    rows = listed()
    files = {p.name for p in DIRECTORY.iterdir() if p.name != "SOURCE.md"}
    assert files == set(rows), (
        f"unlisted: {sorted(files - set(rows))}, gone: {sorted(set(rows) - files)}"
    )
    for name, sha in rows.items():
        assert hashlib.sha256((DIRECTORY / name).read_bytes()).hexdigest() == sha, name


def test_a_file_named_real_is_a_capture_and_one_named_doc_is_not() -> None:
    """real_* files are saved replies; doc_* files come from the docs. SOURCE.md says which."""
    text = (DIRECTORY / "SOURCE.md").read_text()
    for name in listed():
        assert name.startswith(("real_", "doc_")), name
    real_section = text.split("## Captures not made")[0]
    for name in listed():
        if name.startswith("real_"):
            assert name in real_section, f"{name} is not in the captures section"
        else:
            assert name not in real_section, f"{name} is no capture"
