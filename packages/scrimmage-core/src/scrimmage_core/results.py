"""The result file: the document `scrimmage test --json` prints, kept in the records folder.

`write` puts it there once the report is built. `read` is the only way anything reads it back:
it opens without following a link and refuses a file that is not the caller's own, or too big.
Standard library and the plan only: nothing here starts a program or opens a socket.
"""

import contextlib
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import cast

from scrimmage_core import plan as planner
from scrimmage_core.config import Agent, Fault, Test
from scrimmage_core.records import Refused, read_regular

RESULT_FILE = "result.json"
MAX_BYTES = 16 * 1024 * 1024
ENVELOPE_VERSION = 1  # cli.ENVELOPE_VERSION; the result is the envelope's own document
HASH_VERSION = 1  # the format of test_sha256; a change to its inputs bumps this


def write(folder: Path, doc: dict[str, object]) -> Path:
    """The document into `folder`, owner-only, whole or not at all."""
    target = folder / RESULT_FILE
    temp = folder / f".result-{secrets.token_hex(6)}.tmp"
    text = json.dumps(doc, indent=2) + "\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temp, target)
    except BaseException:  # a full disk, or Ctrl-C: no half file and no temp file stays
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    return target


def read(path: Path) -> tuple[dict[str, object] | None, list[str]]:
    """A records folder's result, or the result file itself, and the problems with it.

    The document is None when there are problems. A document of another envelope version is a
    problem: it is not read as if it were this one.
    """
    try:
        if path.is_dir() and not path.is_symlink():
            path = path / RESULT_FILE
        doc: object = json.loads(read_regular(path, MAX_BYTES).decode("utf-8"))
    except Refused as exc:
        return None, [str(exc)]
    except FileNotFoundError:
        return None, [
            f"{path}: has no {RESULT_FILE}; it is a record, a test that did not finish, or from "
            "before Scrimmage wrote one - run the earlier side again"
        ]
    except (OSError, ValueError) as exc:  # a missing file, bad UTF-8 and bad JSON alike
        return None, [f"{path}: cannot read it as a result: {exc}"]
    if not isinstance(doc, dict):
        return None, [f"{path}: is not a result document"]
    found = cast(dict[str, object], doc)
    if found.get("v") != ENVELOPE_VERSION:
        return None, [f"{path}: is of document version {found.get('v')!r}, not {ENVELOPE_VERSION}"]
    return found, []


def row_hash(test: Test, fault: Fault | None, agent: Agent) -> str:
    """What a row was tested by, so a comparison can tell a changed test from a changed agent.

    The agent's own code, prompts and model are left out: changing those is what a comparison
    measures. `tools` is hashed as the file lists it, so another order is another hash.
    """
    rules = test.rules_for(fault)
    doc = {
        "question": test.question,
        "rules": {
            "expect": [list(item) for item in rules.expect],
            "forbid": list(rules.forbid),
            "pass_rate": rules.pass_rate,
        },
        "mocks": [planner.mock_json(mock) for mock in test.mocks],
        "run_timeout": agent.run_timeout,
        "real": list(agent.real),
        "tools": list(agent.tools),
    }
    canonical = json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
