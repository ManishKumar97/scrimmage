"""scrimmage record: one real baseline run per test, its real calls written as mocks.

The team's file is never changed. The output is a copy beside it, the team's file byte for
byte plus the mocks, read back with tomllib and compared before it is written. Each call is
written at its lowest level that can be written whole: a tool's web requests rather than
the tool, so the tool's code still runs in a test. What cannot be written is listed with a
code, and as a commented skeleton in the file. The numbers that prove the slice are the
calls written over the calls seen.
"""

import dataclasses
import difflib
import json
import os
import re
import shutil
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from scrimmage_core import capture, labels, outcomes, toml_write
from scrimmage_core import config as settings
from scrimmage_core import runner as running
from scrimmage_core.control import proc_key
from scrimmage_core.decide import READ_METHODS
from scrimmage_core.errors import ConfigError
from scrimmage_core.outcomes import PREVIEW
from scrimmage_core.redact import REMOVED, URL_PART_REMOVED
from scrimmage_core.state import BLOB_CAP, BLOB_WAIT_S

MAX_RUNS = 1000  # the runner builds every row before it checks the budget
CAP = 64 * 1024  # a mock's value, as TOML, is at most this long: a reviewer must read it
BIG_DIFF = 1000  # added lines a person can review at once
SHOWN = 200  # how much of a call's arguments a comment shows
REFUSED, RAISED, STATUS, DIFFERS, NONDETERMINISTIC = (
    "refused",
    "raised",
    "status",
    "differs",
    "nondeterministic",
)
UNSAVABLE, NULL, TOO_LARGE, BINARY = (
    "unsavable",
    "null",
    "too_large",
    "binary",
)
INCOMPLETE_REPLY, SECRET_CHANGED_SHAPE, MODEL_INSIDE, NOT_FINISHED = (
    "incomplete_reply",
    "secret_changed_shape",
    "model_inside",
    "not_finished",
)
MANUAL_CODES = (
    REFUSED,
    RAISED,
    STATUS,
    DIFFERS,
    NONDETERMINISTIC,
    UNSAVABLE,
    NULL,
    TOO_LARGE,
    BINARY,
    INCOMPLETE_REPLY,
    SECRET_CHANGED_SHAPE,
    MODEL_INSIDE,
    NOT_FINISHED,
)
OUTSIDE_TOOL, CREDENTIAL = "outside_tool", "credential"
SKIPPED_CODES = (OUTSIDE_TOOL, CREDENTIAL)
# A tool whose call ended so gets no mock of its own, and keeps running for real: its value
# would freeze a failure into every run, as a 5xx must not be, or copy a body whose secret
# could not be taken out. A redirect is not a failure, so a 3xx does not block.
NO_TOOL_MOCK = (STATUS, RAISED, SECRET_CHANGED_SHAPE)
NOT_DATA = (401, 403, 407, 408, 429)
MODEL_BENEATH, FROZEN_BENEATH, CREDENTIAL_BENEATH = "model", "frozen", "credential"
# A sibling that failed or could not be saved, beside a success with the same arguments, means
# the call's reply is not one fixed value, so a mock of the success would say what is not so.
SIBLING_CODES = (
    RAISED,
    STATUS,
    NULL,
    TOO_LARGE,
    UNSAVABLE,
    SECRET_CHANGED_SHAPE,
)
HEADER_START = "# Written by scrimmage record from "
SERVICE_NOT_RECORDED = (
    "scrimmage record does not work with an agent inside a web service in this version; "
    "record the same agent as a function or a command, then copy its mocks; nothing was run"
)
CUSTOMER_DATA = (
    "the mocks hold replies from real systems, which can hold customer data; "
    "check them before you commit them"
)
TABLE_HEADER = re.compile(r"\[\[?\s*[A-Za-z_][A-Za-z0-9_-]*(\s*\.\s*[A-Za-z0-9_-]+)*\s*\]\]?")
Fields = dict[str, object]
Line = dict[str, object]


class Incomplete(Exception):
    """A saved value or body never arrived, so the test's record cannot be trusted."""


@dataclass
class Listed:
    """Why a call cannot be written as a mock."""

    code: str
    why: str
    blocks: bool | None = None  # no tool above it gets a mock: by default, from its code

    def __post_init__(self) -> None:
        if self.blocks is None:
            self.blocks = self.code in NO_TOOL_MOCK


@dataclass
class Saved:
    """One real call that can be written as a mock."""

    key: tuple[str, ...]  # calls with one key share a mock
    fields: Fields  # the mock, as the config file reads it
    reply: str  # what a caller gets back, as sorted JSON, to compare calls by
    where: str  # the call, with its arguments or its method and path, for the comment
    method: str | None = None
    path: str | None = None
    asked: tuple[str, str] = ("", "")  # a web call's query and body hashes
    removed: list[Fields] = field(default_factory=list[Fields])
    run: int = 0  # the run, by submission order, whose call made it


Outcome = Saved | Listed


@dataclass
class Node:
    """One gated call, and the gated calls it made."""

    line: Line
    children: "list[Node]" = field(default_factory=lambda: list[Node]())
    run: int = 0  # the run, by submission order, this call belongs to

    @property
    def kind(self) -> str:
        return str(self.line.get("kind"))

    @property
    def action(self) -> str:
        return str(self.line.get("action"))

    @property
    def label(self) -> str:
        return str(self.line.get("call"))


@dataclass
class Verdict:
    """What one call means for the tools above it.

    The invariant: a tool mock is written only if nothing forbidden is anywhere beneath the
    tool. forbidden names what is there, and every level hands it up whole, so a call at any
    depth, of a kind added later too, stops every tool above it from being mocked.
    """

    whole: bool  # this call is written whole at this level or below
    mocks: list[Saved]
    forbidden: set[str] = field(default_factory=set[str])


@dataclass
class Written:
    """A mock this test's output file will hold."""

    saved: Saved
    calls: int
    runs: int = 1  # the distinct runs that saw this mock's key


@dataclass
class ManualEntry:
    call: str
    code: str
    message: str
    method: str | None
    path: str | None
    calls: int = 0
    instead: str | None = None  # the mock of the tool written in place of this call
    shown: str | None = None  # the call's arguments, cleaned, for the skeleton
    args: dict[str, object] | None = None  # the arguments exactly, when a mock could hold them


@dataclass
class ArgumentSet:
    """One tool or MCP call's exact arguments, over every run of a test."""

    runs: set[int] = field(default_factory=set[int])
    shown: str | None = None  # the arguments, cleaned, for the text
    code: str | None = None  # why it has no mock, if listed
    key: tuple[str, ...] | None = None  # the key of its tool-level mock, if it has one


@dataclass
class TestRecord:
    """What record found for one test."""

    test: settings.Test
    results: list[running.RunResult]
    written: list[Written] = field(default_factory=list[Written])
    kept: list[Fields] = field(default_factory=list[Fields])
    skipped: dict[tuple[object, ...], Fields] = field(
        default_factory=dict[tuple[object, ...], Fields]
    )
    manual: dict[tuple[object, ...], ManualEntry] = field(
        default_factory=dict[tuple[object, ...], ManualEntry]
    )
    seen: int = 0
    done: int = 0
    trouble: str | None = None  # why nothing is written for this test
    per_run: dict[int, tuple[int, int]] = field(default_factory=dict[int, tuple[int, int]])
    sets: dict[str, dict[str, "ArgumentSet"]] = field(
        default_factory=dict[str, dict[str, "ArgumentSet"]]
    )
    asked: int = 1  # the runs asked for, of which len(results) started
    cut: bool = False  # a stop of the whole command left this test with fewer runs than asked

    @property
    def result(self) -> running.RunResult:
        """The first run that did not answer, else the first: what the test's line shows."""
        return next((r for r in self.results if r.outcome != "answered"), self.results[0])

    @property
    def not_started(self) -> int:
        return max(self.asked - len(self.results), 0)


@dataclass
class Plan:
    """What the command will run, once its flags have been checked."""

    config: settings.Config  # the baselines to run, with --refresh's mocks taken out
    full: settings.Config  # the team's file as read
    places: dict[str, list[int]]  # each test's mocks, by their place in the team's file
    refreshed: dict[str, list[int]]  # places of the mocks --refresh takes out


def data_status(status: int) -> bool:
    """A reply a mock may copy: 2xx, and 4xx other than the ones that say the recording's
    credentials or the moment were wrong. An outage written as a mock is a fault nobody put
    there; a report may say "wrong" only for a fault Scrimmage injected."""
    return 200 <= status < 300 or (400 <= status < 500 and status not in NOT_DATA)


def holds_none(value: object) -> bool:
    """Whether a value holds None anywhere, which TOML has no form for."""
    if isinstance(value, dict):
        return any(holds_none(item) for item in cast(dict[str, object], value).values())
    if isinstance(value, list):
        return any(holds_none(item) for item in cast(list[object], value))
    return value is None


def toml_size(value: object) -> int:
    return len(toml_write.inline(value).encode())


def value_problem(name: str, value: object) -> Listed | None:
    """Why a value cannot sit in a mock: what TOML has no form for, or what is too big. A ${
    is written as the literal $${, so it reads back as the value it was."""
    if name == "args" and holds_none(value):
        return Listed(UNSAVABLE, "its arguments hold None, which TOML cannot write")
    if holds_none(value):
        return Listed(NULL, f"{name} holds None, which TOML cannot write")
    size = toml_size(value)
    if size > CAP:
        return Listed(TOO_LARGE, f"{name} is {size} bytes as TOML, over the {CAP} cap")
    return None


def blob_data(folder: Path, entry: Fields) -> bytes:
    """A side-folder item's content. One that never arrived leaves the record incomplete."""
    if entry.get("unsaved"):
        raise Incomplete(
            f"a burst of calls filled the queue of saved values for over {BLOB_WAIT_S:g} seconds, "
            "so a saved value was lost; each call saves two values and the queue holds 64, so "
            "make fewer calls at once in this test"
        )
    try:
        return (folder / "blobs" / str(entry["sha256"])).read_bytes()
    except OSError as exc:
        raise Incomplete(f"the side-folder item {entry.get('sha256')} is missing") from exc


def oversize(entry: Fields) -> int | None:
    """An item's size if it is over the cap, read from the line: it is never opened then."""
    size = int(cast(int, entry.get("bytes") or 0))
    return size if size > CAP or entry.get("cut") else None


def too_large(name: str, size: int) -> Listed:
    return Listed(TOO_LARGE, f"{name} is {size} bytes, over the {CAP} cap")


def tool_outcome(line: Line, folder: Path) -> Outcome:
    """A real tool or MCP call's mock, from its line's recorded field, or why not."""
    call = str(line["call"])
    recorded = cast(Fields | None, line.get("recorded"))
    if recorded is None:
        return Listed(capture.SCRIMMAGE_ERROR, "the call's value was not recorded")
    if "code" in recorded:
        return Listed(str(recorded["code"]), str(recorded["why"]))
    entries = {"returns": recorded["value"], "args": recorded["args"]}
    values: Fields = {}
    for name, entry in entries.items():
        size = oversize(cast(Fields, entry))
        if size is not None:
            return too_large(name, size)
        values[name] = json.loads(blob_data(folder, cast(Fields, entry)))
    for name, value in values.items():
        problem = value_problem(name, value)
        if problem is not None:
            return problem
    fields: Fields = {"call": call, "recorded": True}
    if "type" in recorded:
        fields["type"] = recorded["type"]
    fields.update(args=values["args"], returns=values["returns"])
    args = json.dumps(values["args"], ensure_ascii=False, sort_keys=True)
    reply = json.dumps({"returns": values["returns"], "type": recorded.get("type")}, sort_keys=True)
    return mock_outcome(
        Saved(
            (call, args),
            fields,
            reply,
            f"{call} with {shown(args)}",
            removed=cast(list[Fields], recorded.get("removed") or []),
        )
    )


def shown(text: str) -> str:
    return text if len(text) <= SHOWN else text[:SHOWN] + "..."


def web_outcome(line: Line, folder: Path) -> Outcome:
    """A real web request's mock, from its line, or why not. C.J3.12 and C.J3.13."""
    recorded = cast(Fields | None, line.get("recorded"))
    if recorded is not None and "code" in recorded:
        return Listed(str(recorded["code"]), str(recorded["why"]))
    if line.get("error"):
        kind = str(line["error"]).split(":", maxsplit=1)[0]
        return Listed(RAISED, f"its request failed with {kind}")
    if line.get("reply_incomplete"):
        return Listed(INCOMPLETE_REPLY, "the agent did not read the reply to its end")
    if recorded is None:
        return Listed(capture.SCRIMMAGE_ERROR, "the reply was not recorded")
    method, path, status = (
        str(line.get("method")),
        str(line.get("path")),
        int(cast(int, line.get("status") or 0)),
    )
    if not data_status(status):
        redirect = 300 <= status < 400
        return Listed(
            STATUS,
            f"it got status {status}, which a mock must not copy",
            blocks=False if redirect else None,
        )
    if recorded.get("path_changed"):
        return Listed(SECRET_CHANGED_SHAPE, "its path holds a secret, so a mock could not match")
    if not recorded.get("utf8"):
        return Listed(
            BINARY, "its reply is not UTF-8 text, or is compressed in a form not unpacked"
        )
    fields: Fields = {
        "call": str(line["call"]),
        "method": method,
        "path": path,
        "recorded": True,
        "status": status,
    }
    entry = cast(Fields, line["result"])
    size = oversize(entry)
    if size is not None:
        return too_large("the reply", size)
    removed = list(cast(list[Fields], recorded.get("removed") or []))
    kind = cast(str | None, recorded.get("content_type"))
    problem: Listed | None = None
    if method != "HEAD":
        body = blob_data(folder, entry).decode()
        removed, problem = web_body(fields, body, removed, kind)
    if problem is not None:
        return problem
    reply = json.dumps({k: v for k, v in fields.items() if k != "recorded"}, sort_keys=True)
    query, body_sha = (
        str(recorded.get("query_sha256")),
        str(cast(Fields, line["args"]).get("sha256")),
    )
    return mock_outcome(
        Saved(
            (str(line["call"]), method, path),
            fields,
            reply,
            f"{line['call']} {method} {path}",
            method=method,
            path=path,
            asked=(query, body_sha),
            removed=removed,
        )
    )


def web_body(
    fields: Fields, body: str, removed: list[Fields], kind: str | None
) -> tuple[list[Fields], Listed | None]:
    """Add the reply's body to the mock, as json if TOML can hold it, else as exact text."""
    if body == "":
        return removed, header_only(fields, kind)
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return removed, text_form(fields, body, kind)
    cleaned, more = capture.clean(parsed)
    all_removed = removed + more
    where = capture.embedded_secret(cleaned)
    if where is not None:
        return all_removed, Listed(
            SECRET_CHANGED_SHAPE,
            f"a JSON text inside its body holds a secret (under {where}), and an exact text "
            "cannot be cleaned",
        )
    if not toml_can_hold(cleaned):
        if all_removed:
            return all_removed, Listed(
                SECRET_CHANGED_SHAPE,
                "its JSON body holds a null or a value TOML has no form for, so it must stay "
                "exact, and a secret was removed from it",
            )
        return removed, text_form(fields, body, kind)
    problem = value_problem("json", cleaned)
    if problem is not None:
        return all_removed, problem
    fields["json"] = cleaned
    add_header(fields, kind, "application/json")
    return all_removed, None


def header_only(fields: Fields, kind: str | None) -> None:
    """An empty body: the mock sends no content type by itself, so one the reply had is kept."""
    add_header(fields, kind, None)


def text_form(fields: Fields, body: str, kind: str | None) -> Listed | None:
    problem = value_problem("text", body)
    if problem is not None:
        return problem
    fields["text"] = body
    add_header(fields, kind, "text/plain; charset=utf-8")
    return None


def add_header(fields: Fields, kind: str | None, default: str | None) -> None:
    """content-type only, and only when a mock would not send the same media type by itself."""
    if not kind or not kind.isascii() or not kind.isprintable():
        return
    if default is not None and media_type(kind) == media_type(default):
        return
    fields["headers"] = {"content-type": kind}


def media_type(kind: str) -> str:
    return kind.split(";", maxsplit=1)[0].strip().lower()


def toml_can_hold(value: object) -> bool:
    if isinstance(value, dict):
        return all(toml_can_hold(item) for item in cast(dict[str, object], value).values())
    if isinstance(value, list):
        return all(toml_can_hold(item) for item in cast(list[object], value))
    if value is None:
        return False
    return not isinstance(value, int) or capture.INT64[0] <= value <= capture.INT64[1]


def mock_outcome(saved: Saved) -> Outcome:
    """The mock, if the file reader takes it: a bad path or a key it rejects is listed."""
    try:
        settings.read_mock(saved.fields, "the mock")
    except ConfigError as exc:
        return Listed(UNSAVABLE, f"the file reader refuses it: {capture.cleaned_text(str(exc))}")
    return saved


def outcome_of(node: Node, folder: Path) -> Outcome:
    try:
        if node.kind == "http":
            return web_outcome(node.line, folder)
        error = node.line.get("error")
        if error:
            return error_listing(str(error))
        return tool_outcome(node.line, folder)
    except RecursionError:  # JSON nested deeper than the stack can walk
        return Listed(UNSAVABLE, "its value is nested too deep")


def error_listing(error: str) -> Listed:
    kind = error.split(":", maxsplit=1)[0]
    if kind in ("ScrimmageStopped", "ScrimmageRefused"):
        return Listed(NOT_FINISHED, "it was still going when the run ended")
    return Listed(
        RAISED, f'it raised {kind}; do = "error" on a [[test.fault]] makes a tool raise in a test'
    )


def credential_or_outside(line: Line) -> Listed:
    """A web request no real tool call links: never written, and listed by why."""
    recorded = cast(Fields, line.get("recorded") or {})
    if recorded.get("code") == CREDENTIAL:
        return Listed(CREDENTIAL, str(recorded.get("why")))
    return Listed(OUTSIDE_TOOL, "it was made outside any real tool call")


def tree(lines: list[Line]) -> tuple[list[Node], list[Node]]:
    """The calls as a tree, by call_id and parent within one process, and those whose parent's
    line never came, as a run killed during that tool leaves them."""
    by_id: dict[tuple[str, object], Node] = {}
    nodes: list[Node] = []
    roots: list[Node] = []
    orphans: list[Node] = []
    for line in lines:
        node = Node(line)
        nodes.append(node)
        if "call_id" in line:
            by_id[(proc_key(line.get("proc")), line["call_id"])] = node
    for node in nodes:
        parent = node.line.get("parent")
        if parent is None:
            roots.append(node)
            continue
        owner = by_id.get((proc_key(node.line.get("proc")), parent))
        if owner is None:
            orphans.append(node)
        else:
            owner.children.append(node)
    return roots, orphans


def walk(nodes: list[Node]) -> list[Node]:
    out: list[Node] = []
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node.children))
    return out


def arguments_key(line: Line, folder: Path) -> str:
    """A tool or MCP call's arguments as canonical JSON, the key a mock uses. Read from the
    preview if it holds them whole, else from the side-folder item if one is there; a hash of
    the whole text only when neither can be read."""
    arguments = cast(Fields, line.get("args") or {})
    size = int(cast(int, arguments.get("bytes") or 0))
    preview = arguments.get("preview")
    texts: list[str] = []
    if isinstance(preview, str) and size <= PREVIEW and len(preview.encode()) == size:
        texts.append(preview)
    elif 0 < size <= BLOB_CAP and isinstance(arguments.get("sha256"), str):
        try:
            texts.append((folder / "blobs" / str(arguments["sha256"])).read_bytes().decode())
        except (OSError, UnicodeDecodeError):
            texts = []  # no item to read: the hash below stands for the arguments
    for text in texts:
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True)
        except (ValueError, RecursionError):
            pass
    return f"sha256:{arguments.get('sha256')}"


def runs_text(names: list[str]) -> str:
    """Up to three run ids, then a count of the rest."""
    if len(names) <= 3:
        return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)
    return f"{', '.join(names[:3])} and {len(names) - 3} more"


class Recorder:
    """Turns one test's lines, over all its runs, into its mocks, its listed calls and its
    numbers. Each run has its own tree: a call_id restarts in every run, and a pid can be
    reused, so one tree over all the lines could attach a call to another run's tool."""

    def __init__(self, test: settings.Test, results: list[running.RunResult], folder: Path) -> None:
        self.record = TestRecord(test, results)
        self.folder = folder
        self.outcomes: dict[int, Outcome] = {}
        self.nodes: dict[int, Node] = {}
        self.argkeys: dict[int, str] = {}
        self.chosen: list[Saved] = []
        self.places: list[int] = []
        self.forbidden: dict[tuple[str, str], set[str]] = {}
        self.raised: set[int] = set()  # calls on a key that raised in some run

    def run(
        self, runs: list[tuple[running.RunResult, list[Line]]], places: list[int]
    ) -> TestRecord:
        self.places = places
        roots: list[Node] = []
        orphans: list[Node] = []
        everything: list[Node] = []
        for index, (_, lines) in enumerate(runs):
            mine, lost = tree([line for line in lines if "call" in line])
            found = walk(mine) + lost
            for node in found:
                node.run = index
            roots += mine
            orphans += lost
            everything += found
        self.classify(everything)
        self.find_raised()
        self.settle_groups()
        self.settle_siblings()
        self.settle_web_runs()
        # A first pass learns what is forbidden beneath each key in any run, and is thrown away.
        sets = self.record.sets
        self.pass_over(roots, orphans)
        self.record = TestRecord(
            self.record.test, self.record.results, sets=sets, asked=self.record.asked
        )
        for chosen in sets.values():
            for each in chosen.values():
                each.code = None
        self.chosen = []
        self.pass_over(roots, orphans)
        self.write_groups()
        self.mark_bugs()
        return self.record

    def pass_over(self, roots: list[Node], orphans: list[Node]) -> None:
        for node in roots:
            self.root(node)
        for node in orphans:
            self.note_orphan(node)

    def classify(self, nodes: list[Node]) -> None:
        for node in nodes:
            if not self.recordable(node):
                continue
            self.nodes[id(node)] = node
            outcome = (
                credential_or_outside(node.line)
                if node.kind == "http" and node.line.get("parent") is None
                else outcome_of(node, self.folder)
            )
            if isinstance(outcome, Saved):
                outcome.run = node.run
            self.outcomes[id(node)] = outcome
            if node.kind in ("tool", "mcp"):
                key = outcome.key[1] if isinstance(outcome, Saved) else self.derived_key(node)
                self.argkeys[id(node)] = key
                chosen = self.record.sets.setdefault(node.label, {}).setdefault(key, ArgumentSet())
                chosen.runs.add(node.run)
                if chosen.shown is None:
                    chosen.shown = shown_arguments(node.line)

    def derived_key(self, node: Node) -> str:
        return arguments_key(node.line, self.folder)

    def recordable(self, node: Node) -> bool:
        """Whether a real call can have a mock: a tool or MCP call that was a tool call, or a
        web request, not to a model host."""
        if node.action != "real":
            return False
        if node.kind == "http":
            return "provider" not in node.line
        # An MCP line with neither a saved value nor an error is the protocol's own, not a tool.
        return node.kind == "tool" or (
            node.kind == "mcp" and ("recorded" in node.line or "error" in node.line)
        )

    def run_ids(self, members: list[Node]) -> str:
        ids = self.record.results
        return runs_text([ids[i].run for i in sorted({m.run for m in members})])

    def find_raised(self) -> None:
        """A tool key that raised in one run and has calls in another: none of its calls writes
        its web requests. Decided per key, whatever happens to the other calls' replies."""
        groups: dict[tuple[str, str], list[Node]] = {}
        for node_id in self.outcomes:
            node = self.nodes[node_id]
            if node.kind in ("tool", "mcp"):
                groups.setdefault((node.label, self.argkeys[node_id]), []).append(node)
        for members in groups.values():
            raised = [
                n
                for n in members
                if isinstance(o := self.outcomes[id(n)], Listed) and o.code == RAISED
            ]
            if raised and len(raised) < len(members):
                self.raised.update(id(n) for n in members)

    def settle_groups(self) -> None:
        """Several calls with one key share a mock if they got equal replies, in one run or
        across runs. If not, tool and MCP calls are nondeterministic, and web calls are too
        unless their queries or bodies differ: a web mock cannot match those, so that is
        differs."""
        groups: dict[tuple[str, ...], list[Node]] = {}
        for node_id, outcome in self.outcomes.items():
            if isinstance(outcome, Saved):
                groups.setdefault(outcome.key, []).append(self.nodes[node_id])
        for members in groups.values():
            found = [cast(Saved, self.outcomes[id(n)]) for n in members]
            if len({s.reply for s in found}) == 1:
                continue
            per_asked: dict[tuple[str, str], set[str]] = {}
            for saved in found:
                per_asked.setdefault(saved.asked, set()).add(saved.reply)
            same_question_differs = any(len(replies) > 1 for replies in per_asked.values())
            code = NONDETERMINISTIC if same_question_differs else DIFFERS
            why = (
                "the same call got different replies"
                if code == NONDETERMINISTIC
                else "calls with different queries or bodies got different replies, and a web "
                "mock cannot match a query or a body"
            )
            if code == NONDETERMINISTIC and len(self.record.results) > 1:
                why += f" in {self.run_ids(members)}"
            for member in members:
                self.outcomes[id(member)] = Listed(code, why)

    def settle_siblings(self) -> None:
        """A tool or MCP call that succeeded once and failed once on the same arguments, in one
        run or across runs, has no one reply, so the group is nondeterministic. not_finished
        is not in the rule: an orphan did not fail."""
        groups: dict[tuple[str, str], list[Node]] = {}
        for node_id in self.outcomes:
            node = self.nodes[node_id]
            if node.kind in ("tool", "mcp"):
                groups.setdefault((node.label, self.argkeys[node_id]), []).append(node)
        for members in groups.values():
            found = [self.outcomes[id(n)] for n in members]
            listed = [o for o in found if isinstance(o, Listed) and o.code in SIBLING_CODES]
            if not listed or not any(isinstance(o, Saved) for o in found):
                continue
            blocks = any(o.blocks for o in listed)
            why = "the same arguments succeeded once and failed once, so no one reply stands"
            for member in members:
                self.outcomes[id(member)] = Listed(NONDETERMINISTIC, why, blocks=blocks)

    def settle_web_runs(self) -> None:
        """A web key with a call saved in one run and a call listed in another has no one reply
        either. Inside one run the web rules stay as they were."""
        groups: dict[tuple[str, str, str], list[Node]] = {}
        for node_id in self.outcomes:
            node = self.nodes[node_id]
            if node.kind == "http":
                key = (node.label, str(node.line.get("method")), str(node.line.get("path")))
                groups.setdefault(key, []).append(node)
        for members in groups.values():
            saved = [n for n in members if isinstance(self.outcomes[id(n)], Saved)]
            listed = [
                n
                for n in members
                if isinstance(o := self.outcomes[id(n)], Listed) and o.code not in SKIPPED_CODES
            ]
            blocks = any(cast(Listed, self.outcomes[id(n)]).blocks for n in listed)
            why = "the same request got a reply in one run and none to copy in another"
            if not any(n.run != s.run for s in saved for n in listed):
                continue
            for node in saved:  # every run's, the run that listed a call too
                self.outcomes[id(node)] = Listed(NONDETERMINISTIC, why, blocks=blocks)

    def root(self, node: Node) -> None:
        if node.action == "mock":
            self.kept(node)
        elif node.action == "refused":
            self.refused(node)
        elif node.action != "real":
            return
        elif node.kind == "http" and "provider" not in node.line:
            self.root_web(node)
        elif node.kind in ("tool", "mcp") and id(node) in self.outcomes:
            verdict = self.level(node)
            self.tally(node, verdict.whole)
            self.chosen += verdict.mocks

    def tally(self, node: Node, whole: bool | None = None) -> None:
        """Count a call that needed a mock, for the test and for its run; whole if written."""
        self.record.seen += 1
        done = 1 if whole else 0
        self.record.done += done
        seen, got = self.record.per_run.get(node.run, (0, 0))
        self.record.per_run[node.run] = (seen + 1, got + done)

    def root_web(self, node: Node) -> None:
        outcome = self.outcomes[id(node)]
        if cast(Listed, outcome).code == CREDENTIAL:
            self.skip(node, CREDENTIAL)
            return
        self.tally(node)
        self.skip(node, OUTSIDE_TOOL)

    def note_orphan(self, node: Node) -> None:
        if node.action == "real" and node.kind in ("tool", "mcp", "http"):
            self.tally(node)
            self.manual(node, Listed(NOT_FINISHED, "the tool that made it never finished"))

    def level(self, node: Node) -> Verdict:
        """This call's verdict. A tool with gated calls under it is written as those calls when
        each can be, so its code still runs in a test. A tool with a call that cannot be written
        falls back to a mock of itself, only when nothing forbidden is beneath it (see Verdict).
        """
        own = self.outcomes.get(id(node))
        if isinstance(own, Listed) and own.code == CREDENTIAL:
            self.skip(node, CREDENTIAL)
            return Verdict(True, [], {CREDENTIAL_BENEATH})
        key = (node.label, self.argkeys.get(id(node), ""))
        here: set[str] = (
            self.forbidden.setdefault(key, set()) if id(node) in self.argkeys else set[str]()
        )
        if not node.children:
            verdict = self.leaf(node, own, set(here))
            here |= verdict.forbidden
            return verdict
        verdicts = [self.child_verdict(child) for child in node.children]
        forbidden = {name for verdict in verdicts for name in verdict.forbidden}
        here |= forbidden
        forbidden = set(here)  # what any run found beneath a call on this key
        mocks = [saved for verdict in verdicts for saved in verdict.mocks]
        if isinstance(own, Listed) and (node.line.get("error") or id(node) in self.raised):
            self.manual(node, own)
            return Verdict(False, [], forbidden | self.own_forbidden(own))
        if MODEL_BENEATH in forbidden:
            self.manual(node, Listed(MODEL_INSIDE, "a call under it reached the model"))
            return Verdict(False, mocks, forbidden)
        if FROZEN_BENEATH in forbidden:
            return Verdict(False, mocks, forbidden)
        if all(verdict.whole for verdict in verdicts):
            return Verdict(True, mocks, forbidden)
        if CREDENTIAL_BENEATH in forbidden:  # its value could hold what that request fetched
            return Verdict(False, mocks, forbidden)
        if isinstance(own, Saved):
            self.mark_instead(node.children, own)
            return Verdict(True, [own], set())
        if own is None:
            return Verdict(False, [], forbidden)
        self.manual(node, own)
        return Verdict(False, [], forbidden | self.own_forbidden(own))

    def own_forbidden(self, own: Listed) -> set[str]:
        return {FROZEN_BENEATH} if own.blocks else set()

    def leaf(self, node: Node, own: Outcome | None, beneath: set[str]) -> Verdict:
        if isinstance(own, Saved) and beneath & {MODEL_BENEATH, CREDENTIAL_BENEATH, FROZEN_BENEATH}:
            self.manual(node, self.forbidden_listing(node, beneath))
            return Verdict(False, [], beneath)
        if isinstance(own, Saved):
            return Verdict(True, [own])
        if own is None:
            return Verdict(False, [])
        self.manual(node, own)
        return Verdict(False, [], self.own_forbidden(own))

    def forbidden_listing(self, node: Node, beneath: set[str]) -> Listed:
        """Why a clean call gets no mock: another run found something forbidden beneath it."""
        if MODEL_BENEATH in beneath:
            what, code = "reached the model", MODEL_INSIDE
        elif CREDENTIAL_BENEATH in beneath:
            what, code = "made a credential request", NONDETERMINISTIC
        else:
            what, code = "made a call that cannot be written", NONDETERMINISTIC
        runs = self.record.sets[node.label][self.argkeys[id(node)]].runs
        ids = runs_text([self.record.results[i].run for i in sorted(runs)])
        # This call was clean; only another run of it found the forbidden call beneath.
        why = f"a call under it {what} in another run"
        if len(self.record.results) > 1:
            why += f"; this call was made in {ids}"
        return Listed(code, why)

    def child_verdict(self, child: Node) -> Verdict:
        """A call a tool made. What is not a kind known here is forbidden, so a new one is safe."""
        if child.action == "mock":
            self.kept(child)
            return Verdict(True, [])
        if child.action == "refused":
            self.refused(child)
            return Verdict(False, [], {FROZEN_BENEATH})
        if child.kind == "model" or (child.kind == "http" and "provider" in child.line):
            return Verdict(False, [], {MODEL_BENEATH})
        if child.kind == "process":
            return Verdict(False, [])
        if id(child) in self.outcomes:
            return self.level(child)
        return Verdict(False, [], {FROZEN_BENEATH})

    def mark_instead(self, children: list[Node], own: Saved) -> None:
        """Say on each listed call under this tool that the tool's own mock stands for it."""
        for child in walk(children):
            found = self.outcomes.get(id(child))
            if isinstance(found, Listed):
                entry = self.record.manual.get(self.entry_key(child, found))
                if entry is not None:
                    entry.instead = own.where

    def kept(self, node: Node) -> None:
        call, index = node.label, int(cast(int, node.line["mock"]))
        entry: Fields = {"call": call, "mock": self.places[index] + 1}
        if entry not in self.record.kept:
            self.record.kept.append(entry)

    def refused(self, node: Node) -> None:
        if node.kind == "model" or node.line.get("reason") == "poisoned":
            return
        error = str(node.line.get("error") or "it was refused")
        why = error.removeprefix("ScrimmageRefused: ")
        self.manual(node, Listed(REFUSED, why))

    def skip(self, node: Node, code: str) -> None:
        key: tuple[object, ...] = (node.label, node.line.get("method"), node.line.get("path"), code)
        entry = self.record.skipped.setdefault(
            key,
            {
                "call": node.label,
                "method": node.line.get("method"),
                "path": node.line.get("path"),
                "code": code,
                "calls": 0,
            },
        )
        entry["calls"] = int(cast(int, entry["calls"])) + 1

    def entry_key(self, node: Node, listed: Listed) -> tuple[object, ...]:
        """One skeleton per call and code; a tool or MCP call also per set of arguments."""
        return (
            node.label,
            node.line.get("method"),
            node.line.get("path"),
            listed.code,
            listed.why,
            self.argkeys.get(id(node)),
        )

    def manual(self, node: Node, listed: Listed) -> None:
        method, path = node.line.get("method"), node.line.get("path")
        if listed.code == capture.SCRIMMAGE_ERROR:
            return  # a bug in Scrimmage, not a call for the team: mark_bugs makes it exit 3
        key = self.entry_key(node, listed)
        if id(node) in self.argkeys:
            chosen = self.record.sets[node.label][self.argkeys[id(node)]]
            chosen.code = chosen.code or listed.code
        if key not in self.record.manual:
            self.record.manual[key] = ManualEntry(
                node.label,
                listed.code,
                listed.why,
                None if method is None else str(method),
                None if path is None else str(path),
                # A web line's arguments are its request body, which holds what no rule cleans.
                shown=None if node.kind == "http" else shown_arguments(node.line),
                args=None if node.kind not in ("tool", "mcp") else exact_arguments(node.line),
            )
        self.record.manual[key].calls += 1

    def write_groups(self) -> None:
        counts: Counter[tuple[str, ...]] = Counter(saved.key for saved in self.chosen)
        runs: dict[tuple[str, ...], set[int]] = {}
        for saved in self.chosen:
            runs.setdefault(saved.key, set()).add(saved.run)
        seen: set[tuple[str, ...]] = set()
        for saved in self.chosen:  # first seen: by run, then by line
            if saved.key not in seen:
                seen.add(saved.key)
                self.record.written.append(Written(saved, counts[saved.key], len(runs[saved.key])))
        for item in self.record.written:
            if "args" in item.saved.fields:
                label, key = item.saved.key
                self.record.sets.get(label, {}).get(key, ArgumentSet()).key = item.saved.key

    def mark_bugs(self) -> None:
        bugs = [o for o in self.outcomes.values() if isinstance(o, Listed)]
        bugs = [o for o in bugs if o.code == capture.SCRIMMAGE_ERROR]
        if bugs:
            self.record.trouble = f"a bug in Scrimmage while recording: {bugs[0].why}"
            self.record.written = []


def shown_arguments(line: Line) -> str | None:
    """A call's arguments for a skeleton's comment, cleaned by both rules. A preview that was
    cut is left out, as its text could stop in the middle of a secret with no key to name it,
    and so are arguments with a secret in JSON text, which no rule can take out of a string."""
    preview = cast(Fields, line.get("args") or {}).get("preview")
    if not isinstance(preview, str):
        return None
    try:
        parsed = json.loads(preview)
    except ValueError:
        return None
    cleaned, _ = capture.clean(parsed)
    if capture.embedded_secret(cleaned) is not None:
        return None
    return shown(json.dumps(cleaned, ensure_ascii=False, sort_keys=True))


def exact_arguments(line: Line) -> dict[str, object] | None:
    """A call's arguments exactly as a mock would hold them, or None. None unless the whole
    preview was read, cleaning took nothing out, the gate's redaction left nothing in it and
    the arguments were plain: a line written from anything less could never match the call."""
    arguments = cast(Fields, line.get("args") or {})
    preview = arguments.get("preview")
    if not isinstance(preview, str) or line.get("args_not_plain"):
        return None
    if REMOVED in preview or URL_PART_REMOVED in preview:
        return None
    try:
        parsed = cast(object, json.loads(preview))
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    cleaned, removed = capture.clean(cast(dict[str, object], parsed))
    if removed or capture.embedded_secret(cleaned) is not None:
        return None
    if value_problem("args", cleaned) is not None:
        return None
    return cast(dict[str, object], cleaned)


def runs_problems(config: settings.Config, runs: str) -> tuple[int, list[str]]:
    """How many runs of each test, or why --runs cannot be that. Text, not int(): int() takes
    "1_0", " 5" and a full-width digit."""
    if re.fullmatch(r"[1-9][0-9]*", runs, flags=re.ASCII) is None:
        return 1, [f"--runs: expected a whole number of 1 or more, got {runs!r}"]
    count = int(runs)
    if count > MAX_RUNS:
        return 1, [f"--runs: at most {MAX_RUNS} runs of each test, got {count}"]
    real = list(config.agent.real)
    if count > 1 and config.agent.max_cost_usd is None and real:
        return count, [
            f"--runs {count} with no max_cost_usd would call {' and '.join(real)} for real "
            f"{count} times with no limit on model spend; set max_cost_usd in [agent], or "
            "record with --runs 1"
        ]
    return count, []


def prepare(
    config: settings.Config, names: list[str], refresh: list[str], runs: str = "1"
) -> tuple[Plan | None, list[str]]:
    """The baselines to run, or the problems with the flags, before any run."""
    if config.agent.service:
        return None, [SERVICE_NOT_RECORDED]
    known = [test.name for test in config.tests]
    problems = [
        f"--test {name!r} names no test in this file; the tests are {', '.join(known)}"
        for name in names
        if name not in known
    ]
    count, more = runs_problems(config, runs)
    problems += more
    chosen = [test for test in config.tests if not names or test.name in names]
    for label in refresh:
        problems += refresh_problems(label, chosen, config)
    if problems:
        return None, problems
    tests: list[settings.Test] = []
    places: dict[str, list[int]] = {}
    refreshed: dict[str, list[int]] = {}
    for test in chosen:
        taken = {
            i for i, mock in enumerate(test.mocks) if any(mock.call == label for label in refresh)
        }
        kept = [i for i in range(len(test.mocks)) if i not in taken]
        places[test.name] = kept
        refreshed[test.name] = sorted(taken)
        mocks = tuple(test.mocks[i] for i in kept)
        tests.append(dataclasses.replace(test, mocks=mocks, faults=()))
    agent = dataclasses.replace(config.agent, runs=count)
    baselines = dataclasses.replace(config, agent=agent, tests=tuple(tests))
    return Plan(baselines, config, places, refreshed), []


def refresh_problems(label: str, tests: list[settings.Test], config: settings.Config) -> list[str]:
    """--refresh runs a label for real, so it is refused for one that would not run for real."""
    try:
        labels.check(label, "--refresh")
    except ConfigError as exc:
        return [str(exc)]
    mocks = [mock for test in tests for mock in test.mocks if mock.call == label]
    if not mocks:
        return [f"--refresh {label}: no test mocks this label"]
    real = list(config.agent.real)
    kind = labels.kind_of(label)
    if kind in ("tool", "mcp"):
        if labels.matches_real(real, label):
            return []
        return [f"--refresh {label}: it is not under [agent] real, so it would never run for real"]
    if kind != "http":
        return [f"--refresh {label}: only tool, MCP and web labels can be recorded again"]
    for mock in mocks:
        read = mock.method in READ_METHODS
        if not (read or labels.matches_real(real, label, mock.method, mock.path)):
            return [
                f"--refresh {label}: its mock on {mock.method or 'any method'} is a write that "
                "is not under [agent] real, so it would never run for real"
            ]
    return []


@dataclass
class Result:
    """What the command found, once. The text and the JSON are two renderings of it."""

    code: int
    out: str | None
    team: str
    folder: Path
    records: list[TestRecord]
    messages: list[tuple[str, str]]
    above: list[tuple[str, str]]
    spent: float
    budget: float | None
    mocks_text: str | None = None
    diff: list[str] = field(default_factory=list[str])
    runs: int = 1  # the runs asked for, of each test
    stopped_by: str | None = None  # the reason a refusal stopped the record, as in a test

    @property
    def seen(self) -> int:
        return sum(r.seen for r in self.records)

    @property
    def done(self) -> int:
        """Calls written whole: none when no file was written."""
        return 0 if self.out is None else sum(r.done for r in self.records)

    @property
    def wrote(self) -> int:
        return sum(len(r.written) for r in self.records if r.trouble is None)

    @property
    def ran(self) -> int:
        """The runs that started, over all tests."""
        return sum(len(r.results) for r in self.records)


def budget_stop(results: list[running.RunResult], stopped_by: str | None) -> bool:
    return stopped_by in running.BUDGET_REASONS or any(
        r.outcome in ("budget reached", "not reserved") for r in results
    )


NO_RESULT_TAIL = "; rows without runs have no result"


def budget_stopped(
    plan: Plan,
    path: Path,
    runner: running.Runner,
    by_name: dict[str, list[running.RunResult]],
    report: running.Report,
) -> Result:
    """A stop on the budget writes nothing, whatever finished: some tests would look whole and
    an earlier record's mocks would be lost to a half-done one."""
    asked = plan.config.agent.runs
    records = [
        TestRecord(test, mine, trouble="the budget stopped the record", asked=asked, cut=True)
        for test in plan.config.tests
        if (mine := by_name.get(test.name))
    ]
    counts = [
        f"{sum(r.outcome == 'answered' for r in by_name.get(test.name, []))} of {asked}"
        for test in plan.config.tests
    ]
    parts = [
        f"{count} runs of {test.name}" if i == 0 else f"{count} of {test.name}"
        for i, (count, test) in enumerate(zip(counts, plan.config.tests, strict=True))
    ]
    told = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    line = (
        f"the budget stopped the record after {told} (${report.spent:.4f} spent); nothing was "
        "written, and an earlier record's file is kept; raise max_cost_usd or lower --runs"
    )
    messages = [(kind, text) for kind, text in report.messages if not text.endswith(NO_RESULT_TAIL)]
    messages.append((outcomes.MISTAKES, line))
    return Result(
        max(report.code, outcomes.MISTAKE),
        None,
        str(path),
        runner.folder,
        records,
        messages,
        report.above,
        report.spent,
        plan.full.agent.max_cost_usd,
        runs=asked,
        stopped_by=report.stopped_by or "budget",
    )


def finish(
    plan: Plan,
    path: Path,
    runner: running.Runner,
    results: list[running.RunResult],
    report: running.Report,
) -> Result:
    """Group and write each test's mocks, one test at a time, and read the file back."""
    folder = runner.folder
    records: list[TestRecord] = []
    by_name: dict[str, list[running.RunResult]] = {}
    for found_run in results:
        by_name.setdefault(found_run.row.test.name, []).append(found_run)
    if budget_stop(results, report.stopped_by):
        return budget_stopped(plan, path, runner, by_name, report)
    notes: list[tuple[str, str]] = []
    for test in plan.config.tests:
        mine = by_name.get(test.name)
        if not mine:
            notes.append((outcomes.NOTES, f"{test.name}: it did not run, so nothing was written"))
            continue
        records.append(record_test(test, mine, folder, plan, runner, notes, report.stopped_by))
    messages: list[tuple[str, str]] = [(outcomes.WARNINGS, CUSTOMER_DATA), *notes]
    messages += report.messages
    messages += [(outcomes.NOTES, text) for text in real_with_args(plan, records)]
    code = report.code
    for found in records:
        code = max(code, test_code(found, messages))
    if any(r.manual for r in records):
        code = max(code, outcomes.ROW_FAILED)
    result = Result(
        code,
        None,
        str(path),
        folder,
        records,
        messages,
        report.above,
        report.spent,
        plan.full.agent.max_cost_usd,
        runs=plan.config.agent.runs,
        stopped_by=report.stopped_by,
    )
    if not records or all(r.trouble is not None for r in records):
        return result
    return write_file(result, plan, path)


def real_with_args(plan: Plan, records: list[TestRecord]) -> list[str]:
    """Labels under real by name that now have a mock with args: a call with other arguments
    runs for real there. Under tool:* the same call is refused, so it is not said."""
    labels_written = dict.fromkeys(
        str(item.saved.fields["call"])
        for found in records
        for item in found.written
        if "args" in item.saved.fields
    )
    return [
        f"{label} is under real and now has a mock with args: a call with other arguments runs "
        "for real; remove it from real if that call should be refused"
        for label in labels_written
        if label in plan.full.agent.real
    ]


def record_test(
    test: settings.Test,
    results: list[running.RunResult],
    folder: Path,
    plan: Plan,
    runner: running.Runner,
    notes: list[tuple[str, str]],
    stopped_by: str | None = None,
) -> TestRecord:
    """One test's mocks from all its runs. A run that may have lost lines, or a test a stop of
    the whole command cut short, writes nothing."""
    places = plan.places[test.name]
    asked = plan.config.agent.runs
    recorder = Recorder(test, results, folder)
    record = recorder.record
    record.asked = asked
    outs = {r.outcome for r in results}
    if "infrastructure error" in outs:
        record.trouble = "its record is incomplete"
        return record
    if "stopped" in outs:
        record.trouble = "another run stopped this one, so its record is not whole"
        return record
    if stopped_by is not None and len(results) < asked:
        record.cut = True
        record.trouble = (
            f"stopped by {stopped_by}; {record.not_started} of {asked} runs did not start, "
            "so its record is not whole"
        )
        return record
    runs = [(r, runner.store.lines(r.run)) for r in results]
    try:
        record = recorder.run(runs, places)
    except Incomplete as exc:
        record = recorder.record
        record.trouble, record.written = f"its record is incomplete: {exc}", []
        return record
    for result in results:
        if result.outcome != "answered":
            continue
        for broken in result.judgement.broken:
            text = (
                f"{test.name}: the recorded run's answer broke a rule: {broken.text}; "
                "check the mocks before trusting them"
            )
            if (outcomes.NOTES, text) not in notes:
                notes.append((outcomes.NOTES, text))
    return record


def test_code(found: TestRecord, messages: list[tuple[str, str]]) -> int:
    """The test's part of the exit code, and the lines that go with it. Every run decides."""
    name = found.test.name
    outs = [r.outcome for r in found.results]
    if found.trouble is not None:
        if "infrastructure error" not in outs and ("stopped" in outs or found.cut):
            # a refusal or a stop elsewhere ended it, and the report says which
            messages.append(
                (outcomes.NOTES, f"{name}: {found.trouble}, so nothing was written for it")
            )
            return outcomes.OK
        messages.append(
            (outcomes.INFRA_ERRORS, f"{name}: {found.trouble}, so nothing was written for it")
        )
        return outcomes.INFRA
    code = outcomes.OK
    if "refused call" in outs:
        first, missing = outs.index("refused call") + 1, found.not_started
        text = (
            f"{name}: run {first} stopped at a refusal; {missing} of {found.asked} runs did not "
            "start"
            if missing
            else f"{name}: the run stopped at the refusal, so calls after it were not seen"
        )
        messages.append((outcomes.NOTES, text))
    if any(outcome in ("crashed", "timed out") for outcome in outs):
        code = outcomes.ROW_FAILED
    return code


def write_file(result: Result, plan: Plan, path: Path) -> Result:
    """Insert each test's mocks, read the whole back, and only then put the file beside it."""
    when = datetime.now(UTC).date().isoformat()
    built = [block_text(found, when) for found in result.records]
    team_text = path.read_bytes().decode()
    out = path.with_name(f"{path.stem}.recorded.toml")
    new_text, problem = spliced(team_text, plan, result.records, built, when, path.name)
    if problem is not None:
        return layout_refused(result, problem, built)
    written_before = out.exists() or out.is_symlink()
    if written_before and not holds_record_header(out):
        return not_ours(result, out, built)
    if written_before and result.wrote == 0:
        note = f"{out} is kept as it was: this record wrote no mocks, so it would only lose them"
        result.messages.insert(1, (outcomes.NOTES, note))
        return result
    team_lines, new_lines = team_text.splitlines(), new_text.splitlines()
    result.diff = list(
        difflib.unified_diff(team_lines, new_lines, str(path), str(out), lineterm="")
    )
    added = sum(1 for line in result.diff if line.startswith("+") and not line.startswith("+++"))
    warning = batch_warning(added)
    if warning is not None:
        result.messages.insert(1, (outcomes.WARNINGS, warning))
    if written_before:
        note = f"replaced {out}, which an earlier record wrote; rename a file to keep it"
        result.messages.insert(1, (outcomes.NOTES, note))
    write_atomically(path, out, new_text)  # the last step: nothing below it can fail
    result.out = str(out)
    return result


def batch_warning(added: int) -> str | None:
    if added <= BIG_DIFF:
        return None
    return (
        f"the diff adds {added} lines, too long to review at once; record in batches with "
        "--test, and rename each batch's file before the next, as the next replaces it"
    )


def holds_record_header(out: Path) -> bool:
    """Whether a file already there is one scrimmage record wrote, by its header."""
    try:
        text = out.read_text(errors="replace")
    except OSError:
        return False
    for line in text.split("\n"):
        if not line.startswith("#"):
            return False
        if line.startswith(HEADER_START):
            return True
    return False


def not_ours(result: Result, out: Path, built: list[str]) -> Result:
    """A file of that name that record did not write is the team's: it is never replaced."""
    result.code = max(result.code, outcomes.MISTAKE)
    result.mocks_text = "".join(built)
    result.messages.append(
        (
            outcomes.MISTAKES,
            f"{out} exists and scrimmage record did not write it; nothing was written, and the "
            "mocks are printed. Move that file away and run record again",
        )
    )
    return result


def layout_refused(result: Result, problem: str, built: list[str]) -> Result:
    """The file's own layout cannot be extended safely: the mocks are printed, nothing written."""
    result.code = max(result.code, outcomes.MISTAKE)
    result.mocks_text = "".join(built)
    result.messages.append(
        (
            outcomes.MISTAKES,
            f"{problem}; nothing was written, and the mocks are printed for you to copy in",
        )
    )
    return result


def block_text(found: TestRecord, when: str) -> str:
    """One test's mocks and skeletons, as they go in the file."""
    if found.trouble is not None:
        return ""
    parts: list[str] = []
    for item in found.written:
        saved = item.saved
        ending = "1 call" if item.calls == 1 else f"{item.calls} calls"
        first = found.results[saved.run]
        named = first.run
        if len(found.results) > 1:
            ending += f" in {item.runs} of {len(found.results)} runs"
            if first.outcome != "answered":
                named += f" ({first.outcome})"
        comment = f"scrimmage record, {when}, {named}: {saved.where}, {ending}."
        if "args" in saved.fields:
            comment += " It answers only these arguments."
        parts.append(toml_write.mock(saved.fields, [comment]))
    parts += [skeleton(entry) for entry in found.manual.values()]
    return "\n".join(parts)


def skeleton(entry: ManualEntry) -> str:
    """A listed call, as commented lines, where its mock would go."""
    head = f"scrimmage record could not write this mock: {entry.call}: {entry.message}."
    hint = (
        f"A mock of {entry.instead} was written in its place."
        if entry.instead is not None
        else "Write it by hand, or leave the tool under real."
    )
    lines = [head, hint]
    if entry.shown is not None:
        lines.append(f"{entry.call} with {entry.shown}")
    lines.append("[[test.mock]]")
    lines.append(f'call    = "{entry.call}"')
    if entry.args is not None:
        lines.append(f"args    = {toml_write.inline(entry.args)}")
    if entry.method is not None:
        lines += [f'method  = "{entry.method}"', f'path    = "{entry.path}"', "status  = ..."]
    else:
        lines.append("returns = ...")
    return "\n".join(toml_write.comment(line) for line in lines) + "\n"


def spliced(
    team_text: str,
    plan: Plan,
    records: list[TestRecord],
    built: list[str],
    when: str,
    name: str,
) -> tuple[str, str | None]:
    """The team's file with the new mocks in, or why it cannot be extended."""
    lines = team_text.split("\n")
    starts = [i for i, line in enumerate(lines) if is_header(line, "[[test]]")]
    full = plan.full.tests
    if len(starts) != len(full):
        return "", "the [[test]] lines in the file cannot be told apart from its text"
    index_of = {test.name: i for i, test in enumerate(full)}
    inserts: dict[int, str] = {}
    taken: list[tuple[int, int]] = []
    for found, block in zip(records, built, strict=True):
        place = index_of[found.test.name]
        begin = starts[place]
        end = starts[place + 1] if place + 1 < len(starts) else len(lines)
        for i in plan.refreshed.get(found.test.name, []) if found.trouble is None else []:
            span = mock_span(lines, begin, end, i, len(full[place].mocks))
            if span is None:
                return "", f"[[test]] {found.test.name!r}: its [[test.mock]] lines cannot be found"
            taken.append(span)
        if block:
            inserts[last_content(lines, begin, end)] = block
    new_lines = patched(lines, inserts, taken)
    new_lines = with_header(new_lines, name, when)
    new_text = "\n".join(new_lines)
    problem = read_back(team_text, new_text, plan, records)
    return new_text, problem


def is_header(line: str, table: str) -> bool:
    """Whether a line is exactly this table's header, spaced in any way TOML allows."""
    text = line.split("#", maxsplit=1)[0].strip().replace(" ", "").replace("\t", "")
    return text == table


def mock_span(
    lines: list[str], begin: int, end: int, nth: int, total: int
) -> tuple[int, int] | None:
    """Lines [first, last) of the test's nth [[test.mock]], to the next table not its own."""
    heads = [i for i in range(begin + 1, end) if is_header(lines[i], "[[test.mock]]")]
    if len(heads) != total:
        return None
    first = heads[nth]
    while first > begin + 1 and lines[first - 1].startswith("# scrimmage record,"):
        first -= 1
    last = heads[nth] + 1
    while last < end and not starts_other_table(lines[last]):
        last += 1
    while last > heads[nth] + 1 and is_blank_or_comment(lines[last - 1]):
        last -= 1
    return first, last


def starts_other_table(line: str) -> bool:
    """A table header that is not one of this mock's own sub-tables.

    A continuation line of a long array can start with [ too, such as [1, 2], so only a whole
    line of bare key words counts. A header this misses fails the read-back: nothing is lost.
    """
    text = line.split("#", maxsplit=1)[0].strip()
    return TABLE_HEADER.fullmatch(text) is not None and not text.replace(" ", "").startswith(
        ("[test.mock.", "[[test.mock.")
    )


def is_blank_or_comment(line: str) -> bool:
    text = line.strip()
    return not text or text.startswith("#")


def last_content(lines: list[str], begin: int, end: int) -> int:
    """The last line of a test's block that is neither blank nor a comment."""
    last = end - 1
    while last > begin and is_blank_or_comment(lines[last]):
        last -= 1
    return last


def patched(lines: list[str], inserts: dict[int, str], taken: list[tuple[int, int]]) -> list[str]:
    """The lines with the mocks of --refresh taken out and each test's mocks added, as one pass."""
    dropped = {i for first, last in taken for i in range(first, last)}
    out: list[str] = []
    for i, line in enumerate(lines):
        if i not in dropped:
            out.append(line)
        if i in inserts:
            out += ["", *inserts[i].rstrip("\n").split("\n")]
    return out


def with_header(lines: list[str], name: str, when: str) -> list[str]:
    """One header line, after the file's leading comment lines, so a #:schema line stays first."""
    header = f"{HEADER_START}{name} on {when}. Review it, then rename it to {name}."
    place = 0
    while place < len(lines) and lines[place].startswith("#"):
        place += 1
    return [*lines[:place], header, *lines[place:]]


def read_back(team_text: str, new_text: str, plan: Plan, records: list[TestRecord]) -> str | None:
    """The new file, read by tomllib, must be the team's file plus exactly these mocks.

    Both are read literally: $${ is ${, and no ${NAME} is filled in, so the check never
    needs the team's environment, and a written mock reads back as the value recorded."""
    team = cast(dict[str, object], settings.literal(tomllib.loads(team_text)))
    try:
        found = settings.literal(tomllib.loads(new_text))
    except tomllib.TOMLDecodeError:
        return "the file's layout cannot take the mocks, such as mocks written as an inline array"
    index_of = {test.name: i for i, test in enumerate(plan.full.tests)}
    tests = cast(list[dict[str, object]], team.get("test") or [])
    for record in records:
        if record.trouble is not None:
            continue
        added = [w.saved.fields for w in record.written]
        table = tests[index_of[record.test.name]]
        drop = set(plan.refreshed.get(record.test.name, []))
        kept = [
            m for i, m in enumerate(cast(list[object], table.get("mock") or [])) if i not in drop
        ]
        mocks = [*kept, *added]
        if mocks:
            table["mock"] = mocks
        else:
            table.pop("mock", None)
    where = capture.differ(team, found, "file")
    if where is not None:
        return "the file's layout cannot take the mocks: reading it back differed"
    guard(records)
    return None


def mock_identity(mock: settings.Mock) -> tuple[object, ...]:
    """What a mock answers: its label, its method and path, and its arguments, told apart by
    type (1, 1.0, true and "1" are four)."""
    args = None if mock.args is None else json.dumps(mock.args, sort_keys=True)
    return (mock.call, mock.method, mock.path, args)


def guard(records: list[TestRecord]) -> None:
    """The mocks of each test, team and written, must pass the order check scrimmage check
    makes and have no two that answer the same call. A failure is a bug in Scrimmage."""
    for record in records:
        if record.trouble is not None:
            continue
        added = [settings.read_mock(w.saved.fields, "the mock") for w in record.written]
        mocks = (*record.test.mocks, *added)
        problems = settings.dead_mocks(dataclasses.replace(record.test, mocks=mocks))
        if problems:
            raise toml_write.WriterBug(
                f"scrimmage wrote a mock that can never answer: {problems[0]}"
            )
        identities = [mock_identity(mock) for mock in mocks]
        for mock in added:
            if identities.count(mock_identity(mock)) > 1:
                raise toml_write.WriterBug(
                    f"scrimmage wrote two mocks that answer the same call: {mock.call}"
                )


def write_atomically(team: Path, out: Path, text: str) -> None:
    """Replace the output file whole, with the team file's permissions."""
    handle, name = tempfile.mkstemp(dir=out.parent, prefix=".scrimmage-record-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        shutil.copymode(team, name)
        os.replace(name, out)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def print_result(result: Result, config: settings.Config) -> None:
    """The result as text, in the order a reviewer reads it."""
    many = result.runs > 1
    tests = outcomes.plural(len(config.tests), "test")
    ran = result.ran if many else len(result.records)
    runs = outcomes.plural(ran, "run")
    each = f"{result.runs} runs each, " if many else ""
    print(f"scrimmage record: {tests}, {each}{runs}; records in {result.folder}")
    for kind, text in result.above:
        print(f"{running.PREFIXES[kind]}: {text}")
    for found in result.records:
        print(test_line(found))
        if many:
            for line in run_lines(found):
                print(line)
    if result.out is not None:
        wrote = outcomes.plural(result.wrote, "mock")
        print(f"wrote {wrote} to {result.out}; {result.team} is unchanged")
    print(f"recorded {result.done} of {result.seen} calls that needed a mock")
    for found in result.records:
        if many and found.trouble is None:
            for line in argument_lines(found):
                print(line)
    for found in result.records:
        for line in by_hand_lines(found):
            print(line)
    for kind, text in result.messages:
        print(f"{running.PREFIXES[kind]}: {text}")
    if result.mocks_text:
        print(result.mocks_text)
    budget = result.budget
    limit = "no budget" if budget is None else f"a budget of ${budget:g}"
    more = f", {outcomes.plural(result.ran, 'run')}" if many else ""
    print(f"model calls cost ${result.spent:.4f} of {limit}{more}")
    if result.out is not None:
        print(f"next: diff {result.team} {result.out}, then scrimmage test {result.out}")
        for line in result.diff:
            print(line)


def test_line(found: TestRecord) -> str:
    parts = [f"wrote {len(found.written)}"] if found.written else []
    for word, count in (
        ("kept", len(found.kept)),
        ("skipped", len(found.skipped)),
        ("by hand", sum(1 for entry in found.manual.values() if entry.instead is None)),
    ):
        if count:
            parts.append(f"{word} {count}")
    if found.trouble is not None:
        parts = ["nothing written"]
    tail = ", ".join(parts) or "no calls to write"
    if found.asked > 1:
        answered = sum(r.outcome == "answered" for r in found.results)
        return f"{found.test.name}  {answered} of {found.asked} answered  {tail}"
    return f"{found.test.name}  {found.result.run}  {found.result.outcome}  {tail}"


def run_lines(found: TestRecord) -> list[str]:
    """One line a run, for a test that ran more than once."""
    out: list[str] = []
    for index, run in enumerate(found.results):
        seen, got = found.per_run.get(index, (0, 0))
        tail = "nothing written" if found.trouble is not None else f"{got} of {seen} calls written"
        out.append(f"  run {index + 1}  {run.run}  {run.outcome}  {tail}")
    return out


def argument_lines(found: TestRecord) -> list[str]:
    """For each label that was called with two or more sets of arguments, the sets."""
    out: list[str] = []
    for label, sets in found.sets.items():
        if len(sets) < 2:
            continue
        each = ", ".join(
            f"{item.shown or '(arguments not shown)'} in {len(item.runs)}" for item in sets.values()
        )
        have = sum(item.key is not None for item in sets.values())
        ending = (
            "each has its own mock" if have == len(sets) else f"{have} of {len(sets)} have a mock"
        )
        out.append(
            f"arguments: {found.test.name}: {label}: {len(sets)} sets in "
            f"{outcomes.plural(len(found.results), 'run')}: {each}; {ending}"
        )
    return out


def before_runs(config: settings.Config) -> str | None:
    """The line said before the first run of a record of several: how many, and what is real."""
    count = config.agent.runs
    if count == 1:
        return None
    real = ", ".join(config.agent.real)
    each = f"{count} runs of each of {outcomes.plural(len(config.tests), 'test')}"
    return (
        f"{each}; each calls these for real: {real}" if real else f"{each}; nothing is under real"
    )


def by_hand_lines(found: TestRecord) -> list[str]:
    name = found.test.name
    with_arguments = found.asked > 1
    out = [
        f"by hand: {name}: {entry.call}{asked_with(entry, with_arguments)}: {entry.message}"
        if entry.instead is None
        else f"instead: {name}: {entry.call}: {entry.message}; {entry.instead} stands for it"
        for entry in found.manual.values()
    ]
    for entry in found.skipped.values():
        why = (
            "a credential request is never recorded; it runs for real, as in the recording"
            if entry["code"] == CREDENTIAL
            else "made outside any real tool call; it runs for real in the test, as it did here"
        )
        request = " ".join(str(entry[k]) for k in ("method", "path") if entry.get(k))
        out.append(f"skipped: {name}: {entry['call']} {request}: {why}")
    for item in found.written:
        out += [
            f"removed: {name}: {item.saved.fields['call']}: {removal_text(removal)}"
            for removal in item.saved.removed
        ]
    return out


def asked_with(entry: ManualEntry, show: bool) -> str:
    return f" with {entry.shown}" if show and entry.shown is not None else ""


def removal_text(removal: Fields) -> str:
    key = removal.get("key")
    if removal.get("rule") == capture.KEY_NAME:
        return f"a value under {key!r} (heuristic: by its key's name)"
    return (
        f"a value under {key!r} (a known secret pattern)"
        if key
        else ("text that matches a known secret pattern")
    )


def json_fields(result: Result) -> dict[str, object]:
    """The fields scrimmage record --json adds to the envelope."""
    written = result.out is not None
    seen_sets = {
        (label, key)
        for found in result.records
        for label, sets in found.sets.items()
        for key in sets
    }
    wrote_sets = {
        (label, key)
        for found in result.records
        if written and found.trouble is None
        for label, sets in found.sets.items()
        for key, item in sets.items()
        if item.key is not None
    }
    doc: dict[str, object] = {
        "out": result.out,
        "records": str(result.folder),
        "cost_usd": round(result.spent, 8),
        "budget_usd": result.budget,
        "stopped_by": result.stopped_by,
        "numbers": {
            "calls_seen": result.seen,
            "calls_written": result.done,
            "runs": result.runs,
            "runs_recorded": sum(len(r.results) for r in result.records if r.trouble is None),
            "argument_sets_seen": len(seen_sets),
            "argument_sets_written": len(wrote_sets),
        },
        "tests": [test_json(found) for found in result.records],
    }
    if result.mocks_text is not None:
        doc["mocks_text"] = result.mocks_text
    return doc


def mock_place(found: TestRecord, key: tuple[str, ...] | None) -> int | None:
    """Where a tool-level mock sits among the output test's mocks, counting from 1."""
    for index, item in enumerate(found.written):
        if key is not None and item.saved.key == key:
            return len(found.test.mocks) + index + 1
    return None


def argument_sets_json(found: TestRecord) -> list[dict[str, object]]:
    """Per label, how its calls split by arguments. No argument value is in it."""
    return [
        {
            "call": label,
            "seen": len(sets),
            "written": sum(item.key is not None for item in sets.values()),
            "sets": [
                {"runs": len(item.runs), "mock": mock_place(found, item.key), "code": item.code}
                for item in sets.values()
            ],
        }
        for label, sets in found.sets.items()
    ]


def test_json(found: TestRecord) -> dict[str, object]:
    return {
        "test": found.test.name,
        "run": found.result.run,
        "outcome": found.result.outcome,
        "runs": [
            {
                "run": run.run,
                "outcome": run.outcome,
                "calls_seen": found.per_run.get(index, (0, 0))[0],
                "calls_written": 0 if found.trouble else found.per_run.get(index, (0, 0))[1],
                "cost_usd": round(run.cost_usd, 8),
            }
            for index, run in enumerate(found.results)
        ],
        "not_started": found.not_started,
        "written": [
            {
                "call": item.saved.fields["call"],
                "method": item.saved.method,
                "path": item.saved.path,
                "calls": item.calls,
                "runs": item.runs,
                "removed": item.saved.removed,
            }
            for item in found.written
        ],
        "kept": found.kept,
        "skipped": list(found.skipped.values()),
        "manual": [
            {
                "call": entry.call,
                "code": entry.code,
                "method": entry.method,
                "path": entry.path,
                "calls": entry.calls,
                "message": entry.message,
                "instead": entry.instead,
            }
            for entry in found.manual.values()
        ],
        "argument_sets": argument_sets_json(found),
    }
