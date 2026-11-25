"""Read scrimmage.toml. Every bad value is reported with its table and key.

Only what this version supports is accepted. Anything else is rejected, never read
and ignored; docs/configuration.md lists what later versions add.

${NAME} is read in every text value, from the environment, when the file is read; $${ is a
literal ${. A service's body and headers also read {question} and {session}, where {{ and }}
are a brace. With start, start and service read {port}. Each string is read once, so an
expanded value is never read again.
"""

import builtins
import difflib
import ipaddress
import math
import os
import re
import shlex
import shutil
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scrimmage import labels, prices, python_args
from scrimmage.errors import ConfigError, docs_url
from scrimmage.judge import Rules, impossible, normal
from scrimmage.mocks import allowed
from scrimmage.prices import PriceFile, Rates, read_prices
from scrimmage.providers.registry import built_in, registry_for
from scrimmage.redact import SCRUB_MIN, redact, remove_values

READ_METHODS = ("GET", "HEAD")  # the web requests that run for real without being listed
AGENT_KEYS = {
    "function",
    "command",
    "children",
    "runs",
    "parallel",
    "run_timeout",
    "tools",
    "real",
    "max_cost_usd",
    "model_hosts",
    "reserve_output_tokens",
    "prices",
    "service_tier",
    "answer",
    "service",
    "body",
    "headers",
    "record_grace_s",
    "record_wait_s",
    "background_real",
    "control",
    "control_tls",
    "start",
}
# An agent inside a web service: these go with service only.
SERVICE_KEYS = (
    "body",
    "headers",
    "record_grace_s",
    "record_wait_s",
    "background_real",
    "control",
    "control_tls",
    "start",
)
# With start the runner talks to its own copy of the service on this machine only.
ATTACH_ONLY = ("control", "control_tls")
LOOPBACK_HOSTS = ("127.0.0.1", "[::1]", "localhost")
CONTROL_TLS_KEYS = {"cert", "key"}
RULE_KEYS = {"expect", "forbid", "pass_rate"}
TEST_KEYS = {"name", "question", "mock", "fault", *RULE_KEYS}
# recorded marks a mock scrimmage record wrote; args, on a tool or MCP mock, the arguments
# it answers, exactly. returns_from names the function that builds a tool mock's value.
MOCK_KEYS = {"call", "returns", "returns_from", "type", "recorded", "args"}
WEB_MOCK_KEYS = {"call", "method", "path", "status", "headers", "json", "text", "recorded"}
# text and find belong to append and replace. On a model call only replace takes them.
CHANGE_KEYS = {"text", "find"}
FAULT_KEYS = {"call", "do", "when", "raises", *CHANGE_KEYS, *RULE_KEYS}
MODEL_FAULT_KEYS = {*FAULT_KEYS, "model", "retry_after", "form", "tool"}
MCP_MOCK_KEYS = {"call", "returns", "recorded", "args"}
MCP_FAULT_KEYS = {"call", "do", "when", *CHANGE_KEYS, *RULE_KEYS}
WEB_FAULT_KEYS = {
    "call", "do", "when", "method", "path", "retry_after", *CHANGE_KEYS, *RULE_KEYS,
}  # fmt: skip
# A mock's body is sent as written, so these could only make the reply lie about it.
BODY_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}

DOCS = docs_url("configuration")
JUDGING = docs_url("judging")
# The faults this version builds. Each one has an entry in docs/fault-catalog.toml.
FAULTS = (
    "timeout", "status", "error", "empty", "append", "replace", "slow", "refused", "reset", "dns",
    "bad body", "context too long", "garble", "truncate", "unknown tool", "bad tool args",
    "refuse",
)  # fmt: skip
# A connection that fails: on a model call or a web request, with what a tool raises instead.
CONNECTION = {
    "refused": "ConnectionRefusedError",
    "reset": "ConnectionResetError",
    "dns": "OSError",  # socket.gaierror is one, and a plan imports only the agent's own
}
STATUS = re.compile(r"status (?P<code>\d{3})")
SLOW = re.compile(r"slow (?P<s>\d+(?:\.\d+)?)s")
# These let the call happen: the fault changes what it returns, or when.
CARRIED = ("append", "replace", "slow", "garble", "truncate", "bad tool args")
# A model's reply that came back 200 and is wrong. refuse and unknown tool throw the real
# reply away, so they are built and nothing is sent; the other three change the real one.
REPLY_FAULTS = ("garble", "truncate", "unknown tool", "bad tool args", "refuse")
BUILT_REPLIES = ("refuse", "unknown tool")
CHANGED_REPLIES = tuple(word for word in REPLY_FAULTS if word not in BUILT_REPLIES)
MAX_SLOW_S = 600.0
# The replies RFC 9110 and RFC 6585 give a Retry-After header: on another status it means
# nothing to a library.
RETRY_AFTER = ("status 429", "status 503")
MAX_CHANGE_TEXT = 64 * 1024  # bytes, for text and for find
SCHEDULE = re.compile(r"always|(?:first|requests|call) (?P<n>[1-9]\d*)")
# The name unknown tool calls, when a row names it: the MCP specification's rule for a tool
# name, which SHOULD be 1 to 128 of letters, digits, underscore, hyphen and dot.
MAX_TOOL_NAME = 128
TOOL_NAME_CHARS = re.compile(r"[A-Za-z0-9_.-]+")
MODEL_KEYS = {"host", "provider"}
DEFAULT_BUDGET_USD = 5.0
DEFAULT_RESERVE_OUTPUT_TOKENS = 4096
# Where the answer is in what the agent returns, such as messages[-1].content.
ANSWER_PATH = re.compile(r"[A-Za-z_]\w*(\[-?\d+\]|\.[A-Za-z_]\w*)*")
IMPORT_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
TOOL_NAMES = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")  # a function, or Class.method
QUESTION = "{question}"  # in a command, and in a service's body
SESSION = "{session}"  # in a service's body and headers: new for every run
PORT = "{port}"  # in start and service: the free port the runner picks for its copy
SESSION_PREFIX = "scrimmage-"
MAX_ARGUMENT = 128 * 1024  # Linux's limit on one argument, with its closing NUL
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# http://host:port/path, with a host name, an IPv4 address or a bracketed IPv6 one.
SERVICE_URL = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<host>[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>[0-9]{1,5}))?(?P<path>/[^\s#]*)?"
)
# ip:port, an IPv6 address in brackets. A name is never read: localhost can land on ::1.
CONTROL = re.compile(r"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<v4>[0-9.]+)):(?P<port>[0-9]{1,5})")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an HTTP token, RFC 9110
# Headers the runner writes itself, or that would change how the request is read.
SET_BY_SCRIMMAGE = ("host", "content-length", "content-type", "transfer-encoding", "connection")
MAX_GRACE_S = 60.0
MAX_WAIT_S = 120.0
DEFAULT_GRACE_S = 5.0
DEFAULT_WAIT_S = 10.0


class Expanded(str):
    """A text value whose ${NAME} placeholders were filled in. It is what the agent gets;
    written is the text as the file has it, which is what a row's hash reads, so a hash
    never covers a secret and a rotated token is not a changed test."""

    written: str

    def __new__(cls, value: str, written: str) -> "Expanded":
        made = super().__new__(cls, value)
        made.written = written
        return made

    def __reduce__(self) -> tuple[type["Expanded"], tuple[str, str]]:
        """Copied and pickled with its written form, as dataclasses.replace and copy do."""
        return Expanded, (str.__str__(self), self.written)


def as_written(value: object) -> object:
    """A value as the file wrote it: every expanded text back to its placeholders."""
    if isinstance(value, Expanded):
        return value.written
    if isinstance(value, dict):
        return {k: as_written(v) for k, v in cast(dict[object, object], value).items()}
    if isinstance(value, list):
        return [as_written(v) for v in cast(list[object], value)]
    return value


def kept(original: str, checked: str) -> str:
    """A checked value keeps its written form, even where the check changed its text, such as
    a host in lower case: the row hash must never read the value from the environment."""
    if not isinstance(original, Expanded) or isinstance(checked, Expanded):
        return checked
    return original if checked == original else Expanded(checked, original.written)


@dataclass(frozen=True)
class Piece:
    """One part of a text: text as it is, an expanded value, the question or the session."""

    kind: str  # "text", "value", "question" or "session"
    text: str = ""


@dataclass(frozen=True)
class Template:
    """A body or header text, read once into its pieces, filled in for each run."""

    pieces: tuple[Piece, ...]

    def render(self, question: str, session: str) -> str:
        filled = {"question": question, "session": session}
        return "".join(filled.get(piece.kind, piece.text) for piece in self.pieces)

    @property
    def asks(self) -> bool:
        return any(piece.kind == "question" for piece in self.pieces)

    @property
    def fixed(self) -> str:
        """The text that is the same in every run: all but the question and the session."""
        return "".join(p.text for p in self.pieces if p.kind in ("text", "value"))


class Reader:
    """Reads placeholders from one environment. Every value it fills in of SCRUB_MIN
    characters or more is kept, so the runner and the SDK remove it from every record.

    literal: fill in nothing, so a file reads the same in any environment; $${ is still ${.
    """

    def __init__(self, env: Mapping[str, str], *, literal: bool = False) -> None:
        self.env = env
        self.literal = literal
        self.scrub: set[str] = set()

    def scan(self, text: str, where: str, *, braces: bool = False) -> list[Piece]:
        """One pass over the text. braces: also read {question}, {session}, {{ and }}."""
        pieces: list[Piece] = []
        held: list[str] = []

        def flush() -> None:
            if held:
                pieces.append(Piece("text", "".join(held)))
                held.clear()

        i = 0
        while i < len(text):
            if text.startswith("$${", i):
                # A literal ${, with the name and the brace that close it, which a body would
                # otherwise read as a lone }.
                end = text.find("}", i + 3)
                escaped = end != -1 and VARIABLE.fullmatch(text[i + 3 : end]) is not None
                held.append(text[i + 1 : end + 1] if escaped else "${")
                i = end + 1 if escaped else i + 3
                continue
            if text.startswith("${", i):
                end = text.find("}", i + 2)
                name = text[i + 2 : end] if end != -1 else ""
                if not VARIABLE.fullmatch(name):
                    shown = text[i : end + 1] if end != -1 else text[i:]
                    raise ConfigError(
                        f"{where}: {redact(shown)!r} is not a placeholder; a placeholder is "
                        "${NAME}, and $${ is a literal ${"
                    )
                i = end + 1
                if self.literal:
                    held.append(f"${{{name}}}")
                    continue
                value = self.env.get(name)
                if value is None:
                    raise ConfigError(f"{where}: ${{{name}}} is not set in the environment")
                if len(value) >= SCRUB_MIN:
                    self.scrub.add(value)
                flush()
                pieces.append(Piece("value", value))
                continue
            if braces:
                found = braced(text, i, where)
                if found is not None:
                    piece, i = found
                    if piece.kind == "text":
                        held.append(piece.text)
                    else:
                        flush()
                        pieces.append(piece)
                    continue
            held.append(text[i])
            i += 1
        flush()
        return pieces

    def expand(self, text: str, where: str) -> str:
        """A text value with its placeholders filled in, keeping the written text beside."""
        if "${" not in text:
            return text
        filled = "".join(p.text for p in self.scan(text, where))
        return filled if self.literal else Expanded(filled, text)

    def template(self, text: str, where: str) -> Template:
        return Template(tuple(self.scan(text, where, braces=True)))

    def walk(self, value: object, where: str, skip: tuple[str, ...] = ()) -> object:
        """Every text in a value, keys left as they are. skip: keys read by their own rules."""
        if isinstance(value, str):
            return self.expand(value, where)
        if isinstance(value, dict):
            table = cast(dict[str, object], value)
            return {
                key: item if key in skip else self.walk(item, f"{where} {key}")
                for key, item in table.items()
            }
        if isinstance(value, list):
            items = cast(list[object], value)
            return [self.walk(item, f"{where} {i}") for i, item in enumerate(items, 1)]
        return value

    def file(self, data: dict[str, object]) -> dict[str, object]:
        """The whole file. A service's address, body and headers, a command and start are read
        later, by their own rules: a command line is split first, so a value cannot change the
        split, and {port} is found in the file's own text only."""
        out: dict[str, object] = {}
        for key, value in data.items():
            if key == "agent" and isinstance(value, dict):
                out[key] = self.walk(
                    cast(object, value),
                    "[agent]",
                    ("body", "headers", "command", "start", "service"),
                )
            elif key == "test" and isinstance(value, list):
                tests = cast(list[object], value)
                out[key] = [self.walk(t, test_where(t, i)) for i, t in enumerate(tests, 1)]
            else:
                out[key] = self.walk(value, key)
        return out


def test_where(test: object, index: int) -> str:
    name = cast(dict[str, object], test).get("name") if isinstance(test, dict) else None
    return f"[[test]] {name!r}" if isinstance(name, str) else f"[[test]] {index}"


def braced(text: str, i: int, where: str) -> tuple[Piece, int] | None:
    """A brace at i in a body or header text: {{, }}, {question}, {session}, or a mistake."""
    if text.startswith("{{", i):
        return Piece("text", "{"), i + 2
    if text.startswith("}}", i):
        return Piece("text", "}"), i + 2
    if text.startswith(QUESTION, i):
        return Piece("question"), i + len(QUESTION)
    if text.startswith(SESSION, i):
        return Piece("session"), i + len(SESSION)
    if text[i] not in "{}":
        return None
    end = text.find("}", i)
    shown = text[i : end + 1] if text[i] == "{" and end != -1 else text[i]
    raise ConfigError(
        f"{where}: {redact(shown)} is not a placeholder; only {QUESTION} and {SESSION} are "
        "read here, and {{ and }} are a brace"
    )


def new_session() -> str:
    """A run's {session}: new for every run, and never the run's name."""
    return SESSION_PREFIX + os.urandom(8).hex()


def literal(data: object) -> object:
    """A file's values as text, with nothing filled in: $${ reads as ${ and ${NAME} stays.
    What scrimmage record reads back, so its check never needs the team's environment."""
    return Reader({}, literal=True).walk(data, "the file")


@dataclass(frozen=True)
class Mock:
    call: str
    returns: object
    type: str | None
    # A web mock is one fixed reply, for a request narrowed by method and path if given.
    method: str | None = None
    path: str | None = None
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    json: object = None
    text: str | None = None
    recorded: bool = False  # written by scrimmage record: unused, it is a note, not a mistake
    args: dict[str, object] | None = None  # the mock answers only these exact arguments
    returns_from: str | None = None  # a tool mock: the function that builds its value


@dataclass(frozen=True)
class Fault:
    call: str
    do: str
    when: str
    raises: str | None = None  # for "error": a built-in name, or package.module:Class
    method: str | None = None  # a web fault can be narrowed to one method and path
    path: str | None = None
    # What judges this row in place of the test's. Never in a plan: the agent must not see it.
    expect: tuple[tuple[str, ...], ...] | None = None
    forbid: tuple[str, ...] | None = None
    pass_rate: float | None = None
    model: str | None = None  # a model fault can be narrowed to one model name
    text: str | None = None  # append and replace: the injected text
    find: str | None = None  # replace: the text it puts text in place of
    retry_after: int | None = None  # status 429 and 503: the reply's Retry-After, in seconds
    form: str | None = None  # refuse: "plain", the default, or "classifier"
    tool: str | None = None  # unknown tool: the name the made-up call asks for

    @property
    def target(self) -> str:
        """The call as a person reads it: two rows can differ by model alone."""
        return self.call if self.model is None else f"{self.call} {self.model}"


def carried(do: str) -> bool:
    """Whether a fault word lets the call happen: append, replace, slow, and the reply faults
    that change the real reply."""
    return do in CARRIED or do.partition(" ")[0] == "slow"


def slow_seconds(do: str) -> float | None:
    """The delay of a "slow 8s" fault word; None for any other."""
    found = SLOW.fullmatch(do)
    return None if found is None else float(found.group("s"))


@dataclass(frozen=True)
class Test:
    name: str
    question: str
    expect: tuple[tuple[str, ...], ...]
    mocks: tuple[Mock, ...]
    faults: tuple[Fault, ...]
    forbid: tuple[str, ...] = ()
    pass_rate: float = 1.0

    def rules_for(self, fault: Fault | None) -> Rules:
        """What a row is judged by: each setting a fault gives replaces the test's."""
        if fault is None:
            return Rules(self.expect, self.forbid, self.pass_rate)
        return Rules(
            self.expect if fault.expect is None else fault.expect,
            self.forbid if fault.forbid is None else fault.forbid,
            self.pass_rate if fault.pass_rate is None else fault.pass_rate,
        )


@dataclass(frozen=True)
class ModelHost:
    """A host whose requests are model calls, such as a gateway, and the format it speaks."""

    host: str
    provider: str


@dataclass(frozen=True)
class Agent:
    function: str
    runs: int
    parallel: int
    run_timeout: float
    tools: tuple[str, ...]
    real: tuple[str, ...]
    # (import path, name the model sees), for a tool whose two names differ.
    renames: tuple[tuple[str, str], ...] = ()
    model_hosts: tuple[ModelHost, ...] = ()
    max_cost_usd: float | None = DEFAULT_BUDGET_USD  # None: the budget is off, on purpose
    reserve_output_tokens: int = DEFAULT_RESERVE_OUTPUT_TOKENS
    prices: tuple[tuple[str, Rates], ...] = ()
    service_tier: str | None = None  # the tier a request that names none is held at
    answer: str | None = None  # where the answer is in what the agent returns
    command: tuple[str, ...] = ()  # a script's arguments; function is then empty
    children: bool = False  # child processes fire faults, and join on any Python
    # A command's arguments as read: each a run of pieces, (filled in from ${NAME}, text).
    # {question} is filled in the text only, so a value that holds it is sent as written.
    command_pieces: tuple[tuple[tuple[bool, str], ...], ...] = ()
    command_written: str = ""  # the command line as the file writes it, ${NAME} and all
    # An agent inside a web service: its address, body and headers, function and command empty.
    service: str = ""
    body: object = None  # JSON, with a Template in place of every text
    headers: tuple[tuple[str, Template], ...] = ()
    record_grace_s: float = DEFAULT_GRACE_S  # a run's record stays open this long after its reply
    record_wait_s: float = DEFAULT_WAIT_S  # and the runner waits this much longer for its end
    # Calls work outside any request may make for real while a run is open.
    background_real: tuple[str, ...] = ()
    # Where the runner listens for a service in a container: "ip:port", and the paths, read
    # against the file's folder, of the certificate chain and key it speaks TLS with.
    control: str = ""
    control_tls: tuple[str, str] | None = None
    # The command that starts a test copy of the service, split and read as command is; its
    # pieces fill {port} in the file's own text only.
    start: tuple[str, ...] = ()
    start_written: str = ""  # start as the file writes it, ${NAME} and {port} and all
    start_pieces: tuple[tuple[tuple[bool, str], ...], ...] = ()
    service_pieces: tuple[tuple[bool, str], ...] = ()  # with start: service, as start_pieces

    def tool_names(self) -> list[str]:
        """Every tool's final name: the one the model sees."""
        renames = dict(self.renames)
        return [labels.tool_name(path, renames) for path in self.tools]

    def allowed_imports(self) -> list[str]:
        """Prefixes the agent's process may import from: function's and tools' packages."""
        paths = [*([self.function] if self.function else []), *self.tools]
        return sorted({path.split(".")[0].split(":")[0] + "." for path in paths})


@dataclass(frozen=True)
class Config:
    agent: Agent
    tests: tuple[Test, ...]
    root: Path
    # Every value ${NAME} filled in, of SCRUB_MIN characters or more: removed from records.
    scrub: tuple[str, ...] = ()
    # scrimmage-runs/prices.toml beside the file, if `scrimmage prices update` wrote one.
    team_prices: PriceFile | None = None


def command_args(agent: Agent, question: str) -> list[str]:
    """What one run of a script starts. {question} is filled in the file's own text only."""
    if not agent.command_pieces:
        return [part.replace(QUESTION, question) for part in agent.command]
    return [
        "".join(text if value else text.replace(QUESTION, question) for value, text in pieces)
        for pieces in agent.command_pieces
    ]


def start_args(agent: Agent, port: int) -> list[str]:
    """The words that start the service's test copy, {port} filled in the file's own text."""
    return [
        "".join(text if value else text.replace(PORT, str(port)) for value, text in pieces)
        for pieces in agent.start_pieces
    ]


def service_url(agent: Agent, port: int) -> str:
    """The test copy's address, {port} filled in the file's own text."""
    return "".join(
        text if value else text.replace(PORT, str(port)) for value, text in agent.service_pieces
    )


def request_body(agent: Agent, question: str, session: str) -> object:
    """A service run's body: the file's body with this run's question and session."""

    def filled(value: object) -> object:
        if isinstance(value, Template):
            return value.render(question, session)
        if isinstance(value, dict):
            return {k: filled(v) for k, v in cast(dict[str, object], value).items()}
        if isinstance(value, list):
            return [filled(v) for v in cast(list[object], value)]
        return value

    return filled(agent.body)


def request_headers(agent: Agent, session: str) -> list[tuple[str, str]]:
    """A service run's headers. A header never holds the question."""
    return [(name, value.render("", session)) for name, value in agent.headers]


def load(path: Path, env: Mapping[str, str] | None = None) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path.name}: the file is not UTF-8 text") from None
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path.name}: cannot read it: {exc.strerror}; run 'scrimmage init' to draft one, "
            f"or see {docs_url('quickstart')}"
        ) from None
    except OSError as exc:
        raise ConfigError(f"{path.name}: cannot read it: {exc.strerror}") from None
    reject_unsendable(data, path.name)
    check_keys(data, {"agent", "test"}, path.name)
    if "agent" not in data:
        raise ConfigError(f"{path.name}: [agent] is required")
    reader = Reader(os.environ if env is None else env)
    data = reader.file(data)
    try:
        return read_file(path, data, reader)
    except ConfigError as exc:
        # A message may quote a value it rejects, which may have come from the environment.
        values = tuple(reader.scrub)
        raise ConfigError(*(remove_values(p, values)[0] for p in exc.problems)) from None


def read_file(path: Path, data: dict[str, object], reader: Reader) -> Config:
    tests = as_list(data.get("test", []), f"{path.name} [[test]]")
    if not tests:
        raise ConfigError(f"{path.name}: at least one [[test]] is required")
    agent = read_agent(as_table(data["agent"], "[agent]"), reader)
    seen: set[str] = set()
    parsed: list[Test] = []
    for i, raw in enumerate(tests, 1):
        test = read_test(as_table(raw, f"[[test]] {i}"), i)
        reject_model_mocks(test, agent)
        if test.name in seen:
            raise ConfigError(f"[[test]] {test.name!r}: name is used twice")
        seen.add(test.name)
        for j, fault in enumerate(test.faults, 1):
            if (first := test.faults.index(fault) + 1) != j:
                raise ConfigError(
                    f"[[test.fault]] {j} of {test.name!r}: the same as [[test.fault]] {first}; "
                    "a repeated row runs the same fault again, and runs in [agent] already sets "
                    "how many times every row runs"
                )
        parsed.append(test)
    config = Config(
        agent, tuple(parsed), path.parent, tuple(sorted(reader.scrub)), prices.team(path.parent)
    )
    problems = check_references(config)
    if problems:
        raise ConfigError(*problems)
    return config


def model_paths_in_real(agent: Agent) -> list[str]:
    """A real entry that names a model's path as a web request: it would run unbudgeted."""
    found: list[str] = []
    for entry in agent.real:
        if labels.kind_of(entry) != "http":
            continue
        label, _, rest = entry.partition(" ")
        method, _, path = rest.partition(" ")
        owner = built_in().probe(path, method) if path else None
        if owner is not None:
            host = label.removeprefix("http:")
            found.append(
                f"[agent] real: {entry!r} lists a model call as a web request, which is never "
                f'budgeted or faulted; name the host under model_hosts as {{host = "{host}", '
                f'provider = "{owner[0].name}"}} and take it out of real'
            )
    return found


def check_references(config: Config) -> list[str]:
    """Compare values across the file, before the first run, so a typo costs no model calls."""
    names = config.agent.tool_names()
    # With no tools list a framework's adapter finds the tools when the agent runs, so
    # their names cannot be compared here.
    listed = bool(config.agent.tools)
    problems: list[str] = []
    if config.agent.command and not can_start(config.agent.command[0], config.root):
        problems.append(f"[agent] command: {config.agent.command[0]} is not a program it can find")
    if config.agent.command and (flag := skipping_flag(config.agent.command, config.root)):
        problems.append(
            f"[agent] command: {flag} keeps Python from running Scrimmage's startup file, so the "
            "program would run with no gates; remove it"
        )
    if config.agent.start and not can_start(config.agent.start[0], config.root):
        problems.append(
            f"[agent] start: {config.agent.start[0]} is not a program it can find; activate "
            "the virtual environment that has it, or start it through Python, such as "
            "python -m uvicorn"
        )
    if config.agent.start and (flag := skipping_flag(config.agent.start, config.root)):
        problems.append(
            f"[agent] start: {flag} keeps Python from running Scrimmage's startup file, so the "
            "service would run with no gates; remove it"
        )
    problems += [p for test in config.tests if (p := bad_question(test, config.agent))]
    problems += model_paths_in_real(config.agent)
    for test in config.tests:
        for fault in (None, *test.faults):
            if why := impossible(test.rules_for(fault)):
                row = "baseline" if fault is None else f"fault on {fault.target}"
                problems.append(f"[[test]] {test.name!r} {row}: no answer can pass: {why}")
        for mock in test.mocks:
            if labels.kind_of(mock.call) == "model":
                problems.append(
                    f"[[test]] {test.name!r}: {mock.call} cannot be mocked; "
                    "a model call is always real or a fault"
                )
        problems += dead_mocks(test)
        problems += [
            p for fault in test.faults if (p := carried_problem(test, fault, config.agent))
        ]
        problems += [p for fault in test.faults if (p := long_wait(test, fault, config.agent))]
        problems += [p for fault in test.faults if (p := never_for_provider(test, fault))]
        for entry in [*test.mocks, *test.faults]:
            if labels.kind_of(entry.call) != "tool":
                continue  # a model, web or MCP label cannot be compared with anything yet
            name = entry.call.partition(":")[2]
            if listed and name not in names:
                problems.append(
                    f"[[test]] {test.name!r}: {entry.call} names no tool in [agent] tools; "
                    f"the tools are {', '.join(names) or 'none'}"
                )
        paths = [("type", m.type) for m in test.mocks]
        paths += [("returns_from", m.returns_from) for m in test.mocks]
        paths += [("type", f.raises) for f in test.faults]
        for key, path in paths:
            if path and ":" in path and not allowed(path, config.agent.allowed_imports()):
                problems.append(
                    f"[[test]] {test.name!r}: {key} {path} is outside the agent's packages "
                    f"({', '.join(config.agent.allowed_imports())}), so it is refused"
                )
    for entry in config.agent.real:
        if labels.kind_of(entry) == "model":
            problems.append(
                f"[agent] real: {entry} is not needed; a model call is always real "
                "unless a fault names it"
            )
        name = entry.partition(":")[2]
        if listed and labels.kind_of(entry) == "tool" and name != "*" and name not in names:
            problems.append(f"[agent] real: {entry} names no tool in [agent] tools")
    return problems


@dataclass(frozen=True)
class Write:
    """A call that could run for real and change something: what a warning names it as, and
    the test of whether a recorded call is one of it."""

    text: str
    labels: frozenset[str] = frozenset()  # the calls it is, by label
    host: str | None = None  # a web entry: its host label, and the method and path it names
    method: str | None = None
    path: str | None = None
    any_tool: bool = False  # tool:* with no tools list: every tool label

    def names(self, label: str, method: str | None, path: str | None) -> bool:
        if self.host is not None:
            if label != self.host or self.method not in (None, method):
                return False
            if self.method is None and method in READ_METHODS:
                return False
            return self.path in (None, path)
        short = "mcp:" + label.partition("/")[2] if label.startswith("mcp:") else None
        return (
            label in self.labels
            or short in self.labels
            or (self.any_tool and label.startswith("tool:"))
        )


def real_writes(test: Test, agent: Agent) -> list[Write]:
    """What a test lets run for real that could be a write: every tool under tool:* or listed by
    name that the test does not mock, every real web entry that is not a read, every real MCP
    tool. Nothing tells an MCP read from a write, nor a tool's."""
    found: list[Write] = []
    if "tool:*" in agent.real:
        mocked = {m.call for m in test.mocks}
        names = [n for n in agent.tool_names() if f"tool:{n}" not in mocked]
        if names or not agent.tools:
            shown = f" ({', '.join(names)})" if names else ""
            found.append(
                Write(
                    f"every tool not mocked, under tool:*{shown}",
                    frozenset(f"tool:{n}" for n in names),
                    any_tool=not agent.tools,
                )
            )
    for entry in agent.real:
        kind = labels.kind_of(entry)
        if kind == "tool" and entry != "tool:*" and "tool:*" not in agent.real:
            # An obeyed injection runs a tool listed by name for real as well.
            if entry not in {m.call for m in test.mocks}:
                found.append(Write(entry, frozenset({entry})))
        elif kind == "mcp":
            found.append(Write(entry, frozenset({entry})))
        elif kind == "http":
            host, _, rest = entry.partition(" ")
            method, _, path = rest.partition(" ")
            if method in READ_METHODS:
                continue
            found.append(Write(entry, host=host, method=method or None, path=path or None))
    return found


def write_warnings(config: Config) -> list[str]:
    """One warning per append or replace row: its text may ask the agent for a write, and a
    write that real allows would then run for real in that row."""
    out: list[str] = []
    for test in config.tests:
        writes = real_writes(test, config.agent)
        if not writes:
            continue
        for i, fault in enumerate(test.faults, 1):
            if fault.do not in ("append", "replace"):
                continue
            out.append(
                f"fault {i} of {test.name!r} adds text the agent may obey; these would run for "
                f"real if it did: {'; '.join(w.text for w in writes)}. Leave a write unmocked "
                "and off real to see it refused instead"
            )
    return out


def covered(test: Test, fault: Fault, agent: Agent) -> bool:
    """Whether the baseline lets the faulted call run: a mock answers it, or real covers it.
    Anything else is refused, in the baseline and under a carried fault alike."""
    web = labels.kind_of(fault.call) == "http"
    for m in test.mocks:
        if (same_label(m.call, fault.call) or same_label(fault.call, m.call)) and (
            not web or fault.method in (None, m.method) or m.method is None
        ):
            return True
    short = "mcp:" + fault.call.partition("/")[2] if fault.call.startswith("mcp:") else None
    real = list(agent.real)
    return labels.matches_real(real, fault.call, fault.method, fault.path) or short in real


def carried_problem(test: Test, fault: Fault, agent: Agent) -> str | None:
    """A mistake in a carried fault that the whole file shows, before any model call."""
    if not carried(fault.do):
        return None
    where = f"[[test]] {test.name!r}: fault on {fault.target}"
    seconds = slow_seconds(fault.do)
    if seconds is not None and seconds >= agent.run_timeout:
        return f"{where}: {fault.do} is not under run_timeout, {agent.run_timeout:g}"
    kind = labels.kind_of(fault.call)
    if kind == "http" and fault.method in (None, *READ_METHODS):
        return None  # a web read runs on its own
    if kind not in ("tool", "mcp", "http") or covered(test, fault, agent):
        return None
    word = fault.do.partition(" ")[0]
    return (
        f"{where}: {word} lets the call run, so {fault.call} must be mocked or listed under "
        "[agent] real; as written every call it names is refused"
    )


def never_for_provider(test: Test, fault: Fault) -> str | None:
    """A fault that no reply of the row's provider can take, found before any run. The provider
    is known only when the row names it, as call = "model:<provider>". A row that says "model"
    takes whichever provider the agent calls, so the call-time refusal stays for it."""
    name = fault.call.removeprefix("model:") if fault.call.startswith("model:") else ""
    if not name or "/" in name or name not in labels.PROVIDERS:
        return None
    provider = built_in().get(name)
    why = provider.never_carried_out(fault.do)
    if why is None and fault.form is not None:
        why = provider.refusal_form_problem(fault.form)
    if why is None:
        return None
    return f"[[test]] {test.name!r}: fault on {fault.target}: {fault.call} {why}"


def long_wait(test: Test, fault: Fault, agent: Agent) -> str | None:
    """A retry_after the run cannot outlast: the code would wait past its own time limit."""
    if fault.retry_after is None or fault.retry_after < agent.run_timeout:
        return None
    return (
        f"[[test]] {test.name!r}: fault on {fault.target}: retry_after {fault.retry_after} is "
        f"not under run_timeout, {agent.run_timeout:g}"
    )


def same_label(upper: str, lower: str) -> bool:
    """Whether two mock labels name the same call: equal, or an MCP tool in its short form
    above the same tool with its server."""
    if upper == lower:
        return True
    return (
        upper.startswith("mcp:")
        and lower.startswith("mcp:")
        and (upper == "mcp:" + lower.partition("/")[2])
    )


def dead_mocks(test: Test) -> list[str]:
    """A mock with no args answers every call to its label, so an args mock below it never does."""
    problems: list[str] = []
    for i, lower in enumerate(test.mocks):
        if lower.args is None or labels.kind_of(lower.call) not in ("tool", "mcp"):
            continue
        for upper in test.mocks[:i]:
            if upper.args is None and same_label(upper.call, lower.call):
                problems.append(
                    f"[[test]] {test.name!r}: the mock on {upper.call} has no args, so it answers "
                    f"every call and the mock with args below it on {lower.call} never answers; "
                    "move the mock with no args last"
                )
                break
    return problems


def bad_question(test: Test, agent: Agent) -> str | None:
    """A question goes as one argument, which a NUL ends and Linux limits."""
    where = f"[[test]] {test.name!r} question"
    if "\0" in test.question:
        return f"{where}: holds a NUL, which cannot go in an argument"
    parts = command_args(agent, test.question) or [test.question]
    if max(len(part.encode()) + 1 for part in parts) > MAX_ARGUMENT:
        return f"{where}: is over the {MAX_ARGUMENT // 1024} KiB one argument can hold"
    return None


def skipping_flag(command: tuple[str, ...], root: Path) -> str | None:
    """A flag on the command that skips the startup file. No gate exists yet to catch it."""
    for place, word in enumerate(command):
        if python_args.PYTHON.fullmatch(os.path.basename(word)):
            return python_args.skipping(list(command[place + 1 :]))
    program = str(root / command[0]) if "/" in command[0] else shutil.which(command[0])
    target = python_args.interpreter(program, dict(os.environ)) if program else None
    return python_args.skipping(target[1]) if target else None


def can_start(program: str, root: Path) -> bool:
    """A program named with a path is found from the folder it runs in, others on PATH.
    It must be a file it can run, which a folder or a plain text file is not."""
    if "/" not in program:
        return shutil.which(program) is not None
    path = root / program
    return path.is_file() and os.access(path, os.X_OK)


def reject_unsendable(value: object, where: str) -> None:
    """Reject values a plan cannot carry, such as TOML dates."""
    if not isinstance(value, str | int | float | bool | list | dict):
        raise ConfigError(
            f"{where}: a {type(value).__name__} cannot be sent to the agent; write it as text"
        )
    if isinstance(value, dict):
        for key, item in as_table(cast(object, value), where).items():
            reject_unsendable(item, f"{where}.{key}")
    if isinstance(value, list):
        for item in as_list(cast(object, value), where):
            reject_unsendable(item, where)


def as_table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def as_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list")
    return list(cast(list[object], value))


def check_keys(table: dict[str, object], known: set[str], where: str) -> None:
    for key in table:
        if key not in known:
            raise ConfigError(f"{where} {key}: unknown setting; see {DOCS}")


def text(table: dict[str, object], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} {key}: expected text, got {value!r}")
    return value


def import_path(value: str, where: str) -> str:
    if not IMPORT_PATH.fullmatch(value):
        raise ConfigError(
            f"{where}: {value!r} is not an import path of the form 'package.module:function'"
        )
    return value


def tool_path(value: str, where: str) -> str:
    """A function, or a method on a class at the module's top level."""
    module, colon, name = value.partition(":")
    if colon and TOOL_NAMES.fullmatch(name) and IMPORT_PATH.fullmatch(f"{module}:x"):
        if name.count(".") > 1:
            raise ConfigError(
                f"{where}: {value!r} names a method on a nested class, which this version "
                "cannot gate; move the class to the top of its module"
            )
        return value
    raise ConfigError(
        f"{where}: {value!r} is not an import path of the form 'package.module:function' "
        "or 'package.module:Class.method'"
    )


def whole(table: dict[str, object], key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} {key}: expected a whole number of 1 or more, got {value!r}")
    return value


def read_agent(table: dict[str, object], reader: Reader | None = None) -> Agent:
    where = "[agent]"
    reader = reader or Reader(os.environ)
    check_keys(table, AGENT_KEYS, where)
    if sum(key in table for key in ("function", "command", "service")) != 1:
        raise ConfigError(f"{where}: give function, command or service, exactly one of them")
    service = "service" in table
    for key in SERVICE_KEYS:
        if key in table and not service:
            raise ConfigError(f"{where} {key}: works only with service; see {DOCS}")
    if service and "children" in table:
        raise ConfigError(
            f"{where} children: works only with function or command; a service run refuses "
            f"every process it starts unless real lists it; see {DOCS}"
        )
    for key in ATTACH_ONLY:
        if key in table and "start" in table:
            raise ConfigError(
                f"{where} {key}: works only for a service someone else started, with no start; "
                f"with start the runner and its copy of the service talk on this machine; "
                f"see {DOCS}"
            )
    timeout = table.get("run_timeout", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError(f"{where} run_timeout: expected a number of seconds, got {timeout!r}")
    tools: list[str] = []
    renames: list[tuple[str, str]] = []
    for entry in as_list(table.get("tools", []), f"{where} tools"):
        path, name = read_tool(entry, f"{where} tools")
        tools.append(path)
        if name is not None:
            renames.append((path, name))
    real: list[str] = []
    for entry in as_list(table.get("real", []), f"{where} real"):
        if not isinstance(entry, str):
            raise ConfigError(f"{where} real: expected labels, got {entry!r}")
        real.append(kept(entry, labels.check(entry, f"{where} real", pattern=True)))
    background: list[str] = []
    for entry in as_list(table.get("background_real", []), f"{where} background_real"):
        if not isinstance(entry, str):
            raise ConfigError(f"{where} background_real: expected labels, got {entry!r}")
        background.append(labels.check(entry, f"{where} background_real", pattern=True))
    answer = table.get("answer")
    if answer is not None and not (isinstance(answer, str) and ANSWER_PATH.fullmatch(answer)):
        raise ConfigError(f"{where} answer: {answer!r} is not a path of the form 'field[-1].name'")
    if answer is not None and "command" in table:
        raise ConfigError(f"{where} answer: works only with function; see {DOCS}")
    children = table.get("children", False)
    if not isinstance(children, bool):
        raise ConfigError(f"{where} children: expected true or false, got {children!r}")
    function = ""
    if "function" in table:
        function = import_path(text(table, "function", where), f"{where} function")
    command, pieces = read_command(table, where, reader) if "command" in table else ((), ())
    start, start_pieces = read_start(table, where, reader) if "start" in table else ((), ())
    address, address_pieces = (
        read_service(table, where, reader, managed="start" in table) if service else ("", ())
    )
    agent = Agent(
        function=function,
        runs=whole(table, "runs", 3, where),
        parallel=whole(table, "parallel", 1 if service else 4, where),
        run_timeout=float(timeout),
        tools=tuple(tools),
        real=tuple(real),
        renames=tuple(renames),
        model_hosts=read_model_hosts(table.get("model_hosts", []), where),
        max_cost_usd=read_budget(table.get("max_cost_usd", DEFAULT_BUDGET_USD), where),
        reserve_output_tokens=whole(
            table, "reserve_output_tokens", DEFAULT_RESERVE_OUTPUT_TOKENS, where
        ),
        prices=read_prices(table.get("prices", {}), f"{where}.prices"),
        service_tier=text(table, "service_tier", where) if "service_tier" in table else None,
        answer=answer,
        command=command,
        children=children,
        command_pieces=pieces,
        command_written=str(table["command"]) if "command" in table else "",
        service=address,
        body=read_body(table, where, reader) if service else None,
        headers=read_headers(table.get("headers", {}), f"{where} headers", reader),
        record_grace_s=seconds(table, "record_grace_s", DEFAULT_GRACE_S, MAX_GRACE_S, where),
        record_wait_s=seconds(table, "record_wait_s", DEFAULT_WAIT_S, MAX_WAIT_S, where),
        background_real=tuple(background),
        control=read_control(table, where) if "control" in table else "",
        control_tls=read_control_tls(table, where) if "control_tls" in table else None,
        start=start,
        start_written=str(table["start"]) if "start" in table else "",
        start_pieces=start_pieces,
        service_pieces=address_pieces,
    )
    check_control(agent, where)
    names = agent.tool_names()
    for name in names:
        if names.count(name) > 1:
            raise ConfigError(f"{where} tools: two tools are named {name!r}")
    return agent


def control_address_of(value: str) -> tuple[str, int] | None:
    """The host and port of a control setting, or None when it is not an IP address and a
    port from 1 to 65535, or is an address that listens on every interface."""
    found = CONTROL.fullmatch(value)
    if found is None:
        return None
    host, port = found["v6"] or found["v4"], int(found["port"])
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if (found["v6"] and address.version != 6) or not 1 <= port <= 65535:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    if address.is_unspecified or (mapped is not None and mapped.is_unspecified):
        return None
    return host, port


def read_control(table: dict[str, object], where: str) -> str:
    """The address the runner listens on, for a service in a container to reach."""
    value = text(table, "control", where)
    if control_address_of(value) is None:
        raise ConfigError(
            f"{where} control: {value!r} is not an IP address and a port, such as "
            "'172.17.0.1:8766' or '[fd00::1]:8766', that is one address of this machine and "
            "not every address; a host name is refused because localhost can mean ::1"
        )
    return value


def read_control_tls(table: dict[str, object], where: str) -> tuple[str, str]:
    """The certificate chain and the key, as paths against the file's folder."""
    at = f"{where} control_tls"
    tls = as_table(table["control_tls"], at)
    check_keys(tls, CONTROL_TLS_KEYS, at)
    for key in sorted(CONTROL_TLS_KEYS):
        if key not in tls:
            raise ConfigError(
                f"{at}: {key} is required; write control_tls = {{cert = ..., key = ...}}"
            )
    return text(tls, "cert", at), text(tls, "key", at)


def check_control(agent: Agent, where: str) -> None:
    """Plain HTTP carries the plans and the record, so it stays on this machine."""
    if agent.control_tls is not None and not agent.control:
        raise ConfigError(
            f"{where} control_tls: needs control, the address to listen on; a runner on "
            "loopback needs no certificate"
        )
    address = control_address_of(agent.control) if agent.control else None
    if address is not None and agent.control_tls is None:
        host = ipaddress.ip_address(address[0])
        if not host.is_loopback:
            raise ConfigError(
                f"{where} control: {agent.control!r} is not a loopback address, so records "
                "that can hold customer data would cross the network in clear; add control_tls "
                "with a certificate from scrimmage cert"
            )


def split_words(
    table: dict[str, object], key: str, where: str, reader: Reader
) -> tuple[tuple[str, ...], tuple[tuple[tuple[bool, str], ...], ...]]:
    """A command line's arguments, and their pieces. It is split here, then each argument's
    ${NAME} is filled in, so neither a question nor a value can change the split."""
    try:
        parts = shlex.split(text(table, key, where))
    except ValueError as exc:
        raise ConfigError(f"{where} {key}: {exc}") from None
    pieces = tuple(
        tuple((p.kind == "value", p.text) for p in reader.scan(part, f"{where} {key}"))
        for part in parts
    )
    return tuple("".join(text for _, text in arg) for arg in pieces), pieces


def holds(pieces: tuple[tuple[tuple[bool, str], ...], ...], placeholder: str) -> bool:
    """The placeholder is in the file's own text, not only in a value from the environment."""
    return any(placeholder in text for arg in pieces for value, text in arg if not value)


def read_command(
    table: dict[str, object], where: str, reader: Reader
) -> tuple[tuple[str, ...], tuple[tuple[tuple[bool, str], ...], ...]]:
    words, pieces = split_words(table, "command", where, reader)
    if not holds(pieces, QUESTION):
        raise ConfigError(f"{where} command: must hold {QUESTION}, or the agent gets no question")
    return words, pieces


def read_start(
    table: dict[str, object], where: str, reader: Reader
) -> tuple[tuple[str, ...], tuple[tuple[tuple[bool, str], ...], ...]]:
    """The command that starts the service's test copy. {port} is where the copy listens."""
    words, pieces = split_words(table, "start", where, reader)
    if not holds(pieces, PORT):
        raise ConfigError(
            f"{where} start: must hold {PORT}, the free port the runner picks, such as "
            f"--port {PORT}"
        )
    return words, pieces


def read_service(
    table: dict[str, object], where: str, reader: Reader, *, managed: bool = False
) -> tuple[str, tuple[tuple[bool, str], ...]]:
    """The service's address: http://host:port/path, on any machine the runner reaches, and,
    with managed, its pieces. managed: the runner starts the service, so the port is {port}
    and the host is loopback."""
    written = text(table, "service", where)
    if managed:
        return read_managed_service(written, where, reader)
    value = reader.expand(written, f"{where} service")
    parts = SERVICE_URL.fullmatch(value)
    if parts is not None and parts["scheme"].lower() == "https":
        raise ConfigError(
            f"{where} service: https is not supported in this version, which reaches a service "
            "on this machine over http; use http://host:port/path"
        )
    port = parts["port"] if parts is not None else None
    if parts is None or parts["scheme"] != "http" or (port is not None and int(port) > 65535):
        raise ConfigError(f"{where} service: {value!r} is not of the form http://host:port/path")
    return value, ()


def read_managed_service(
    written: str, where: str, reader: Reader
) -> tuple[str, tuple[tuple[bool, str], ...]]:
    """With start: http://<loopback host>:{port}/path, {port} once, as the port, in the file's
    own text. A ${NAME} value is filled in after, and a {port} in it is sent as written."""
    pieces = tuple((p.kind == "value", p.text) for p in reader.scan(written, f"{where} service"))
    probe = ""  # the address with the file's {port} as 1, to match it as a port
    literal: list[bool] = []  # for each character of probe: from the file's own text
    at = -1
    found = 0
    for value, piece in pieces:
        if not value:
            found += piece.count(PORT)
            if PORT in piece and at < 0:
                at = len(probe) + piece.index(PORT)
            piece = piece.replace(PORT, "1")
        probe += piece
        literal += [not value] * len(piece)
    parts = SERVICE_URL.fullmatch(probe)
    if (
        found != 1
        or parts is None
        or parts["scheme"] != "http"
        or parts["port"] != "1"
        or parts.start("port") != at
    ):
        raise ConfigError(
            f"{where} service: with start it must be of the form http://127.0.0.1:{PORT}/path, "
            f"with {PORT} once, as the port, got {written!r}"
        )
    if not all(literal[parts.start("host") : parts.end("host")]):
        raise ConfigError(
            f"{where} service: with start the host is written in the file, as 127.0.0.1, "
            f"[::1] or localhost, not read from a ${{NAME}} value, got {written!r}"
        )
    if parts["host"].lower() not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"{where} service: with start the runner starts the service on this machine, so "
            f"the host must be 127.0.0.1, [::1] or localhost, got {parts['host']!r}"
        )
    filled = "".join(text for _, text in pieces)
    if any(value for value, _ in pieces):
        return Expanded(filled, written), pieces
    return filled, pieces


def read_body(table: dict[str, object], where: str, reader: Reader) -> object:
    """A service run's body: a table, an array or a text, read once into its pieces."""
    if "body" not in table:
        raise ConfigError(f"{where} body: required with service, and must hold {QUESTION}")
    asks: list[bool] = []

    def read(value: object, at: str) -> object:
        if isinstance(value, str):
            template = reader.template(value, at)
            asks.append(template.asks)
            return template
        if isinstance(value, dict):
            return {k: read(v, f"{at} {k}") for k, v in cast(dict[str, object], value).items()}
        if isinstance(value, list):
            return [read(v, f"{at} {i}") for i, v in enumerate(cast(list[object], value), 1)]
        return value

    raw: object = table["body"]
    if not isinstance(raw, str | dict | list):
        raise ConfigError(f"{where} body: expected a table, an array or text, got {raw!r}")
    body = read(cast(object, raw), f"{where} body")
    if not any(asks):
        raise ConfigError(
            f"{where} body: must hold {QUESTION} in a text value, or the agent gets no question"
        )
    return body


def read_headers(value: object, where: str, reader: Reader) -> tuple[tuple[str, Template], ...]:
    """A service run's headers: names as HTTP writes them, values that never hold the
    question, and printable ASCII once their values are filled in."""
    headers: list[tuple[str, Template]] = []
    for name, raw in as_table(value, where).items():
        at = f"{where} {name}"
        if not HEADER_NAME.fullmatch(name):
            raise ConfigError(f"{at}: {name!r} is not a valid header name")
        if name.lower().startswith("x-scrimmage-") or name.lower() in SET_BY_SCRIMMAGE:
            raise ConfigError(f"{at}: {name} is set by Scrimmage, so it cannot be given here")
        if not isinstance(raw, str):
            raise ConfigError(f"{at}: expected text, got {redact(repr(raw))}")
        template = reader.template(raw, at)
        if template.asks:
            raise ConfigError(f"{at}: {QUESTION} cannot go in a header; put it in body")
        fixed = template.fixed
        if not fixed.isascii() or not fixed.isprintable():
            raise ConfigError(f"{at}: must be printable ASCII once its values are filled in")
        headers.append((name, template))
    return tuple(headers)


def seconds(table: dict[str, object], key: str, default: float, top: float, where: str) -> float:
    """record_grace_s from 0, and record_wait_s above 0, each up to its top."""
    value = table.get(key, default)
    zero = key == "record_grace_s"  # a grace of 0 closes the record at the reply
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
        or (value == 0 and not zero)
        or value > top
    ):
        low = "0" if zero else "above 0"
        raise ConfigError(f"{where} {key}: expected seconds, {low} to {top:g}, got {value!r}")
    return float(value)


def read_model_hosts(value: object, where: str) -> tuple[ModelHost, ...]:
    hosts: list[ModelHost] = []
    for i, raw in enumerate(as_list(value, f"{where} model_hosts"), 1):
        item = f"{where} model_hosts {i}"
        entry = as_table(raw, item)
        check_keys(entry, MODEL_KEYS, item)
        provider = text(entry, "provider", item)
        labels.check_model(f"model:{provider}", f"{item} provider")
        host = text(entry, "host", item).lower()
        if ":" in host or "/" in host:
            raise ConfigError(f"{item} host: {host!r} should be a host name, with no port or path")
        hosts.append(ModelHost(host, provider))
    return tuple(hosts)


def read_budget(value: object, where: str) -> float | None:
    if value == "none":
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(
            f'{where} max_cost_usd: expected dollars above 0, or "none" to turn the budget '
            f"off, got {value!r}"
        )
    return float(value)


def read_tool(entry: object, where: str) -> tuple[str, str | None]:
    """A tools entry: an import path, or {path, name} when the model sees another name."""
    if isinstance(entry, str):
        return tool_path(entry, where), None
    table = as_table(entry, where)
    check_keys(table, {"path", "name"}, where)
    name = text(table, "name", where)
    if not labels.TOOL_NAME.fullmatch(name):
        raise ConfigError(f"{where} name: {name!r} is not a tool name")
    return tool_path(text(table, "path", where), where), name


def read_test(table: dict[str, object], index: int) -> Test:
    where = f"[[test]] {index}"
    check_keys(table, TEST_KEYS, where)
    name = text(table, "name", where)
    where = f"[[test]] {name!r}"
    expect = read_expect(table, where)
    if expect is None:
        raise ConfigError(f"{where} expect: required")
    mocks = [
        read_mock(as_table(m, f"{where} mock"), f"[[test.mock]] {i} of {name!r}")
        for i, m in enumerate(as_list(table.get("mock", []), f"{where} mock"), 1)
    ]
    faults = [
        read_fault(as_table(f, f"{where} fault"), f"[[test.fault]] {i} of {name!r}")
        for i, f in enumerate(as_list(table.get("fault", []), f"{where} fault"), 1)
    ]
    return Test(
        name=name,
        question=text(table, "question", where),
        expect=expect,
        mocks=tuple(mocks),
        faults=tuple(faults),
        forbid=read_forbid(table, where) or (),
        pass_rate=read_pass_rate(table, where) or 1.0,
    )


def rule_text(value: object, where: str) -> str:
    """One text a rule looks for. Blank text would be found in every answer."""
    if not isinstance(value, str) or not normal(value):
        raise ConfigError(f"{where}: expected text, got {redact(repr(value))}")
    return value


def read_expect(
    table: dict[str, object], where: str, forbid_alone: bool = False
) -> tuple[tuple[str, ...], ...] | None:
    """Each item is one text, or a list of texts of which any one will do.

    On a fault row, an empty list is how a row is judged by its forbid alone.
    """
    if "expect" not in table:
        return None
    items = as_list(table["expect"], f"{where} expect")
    if not items:
        if forbid_alone:
            return ()
        raise ConfigError(f"{where} expect: expected a list of text, got []")
    expect: list[tuple[str, ...]] = []
    for item in items:
        words = as_list(cast(object, item), where) if isinstance(item, list) else [item]
        if not words:
            raise ConfigError(f"{where} expect: a list of alternatives is empty")
        expect.append(tuple(rule_text(word, f"{where} expect") for word in words))
    return tuple(expect)


def read_forbid(table: dict[str, object], where: str) -> tuple[str, ...] | None:
    """Text no answer may hold. An empty list is how a fault drops the test's list."""
    if "forbid" not in table:
        return None
    words = as_list(table["forbid"], f"{where} forbid")
    return tuple(rule_text(word, f"{where} forbid") for word in words)


def read_pass_rate(table: dict[str, object], where: str) -> float | None:
    if "pass_rate" not in table:
        return None
    rate = table["pass_rate"]
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not 0 < rate <= 1:
        raise ConfigError(
            f"{where} pass_rate: expected a share above 0, up to 1, got {redact(repr(rate))}"
        )
    return float(rate)


def reject_model_mocks(test: Test, agent: Agent) -> None:
    """A web mock on a model path would never answer: that request is a model call."""
    registry = registry_for(tuple((h.host, h.provider) for h in agent.model_hosts))
    for mock in test.mocks:
        host = mock.call.removeprefix("http:")
        method = mock.method or "POST"
        if mock.path and registry.resolve(host, mock.path, method) is not None:
            raise ConfigError(
                f"[[test]] {test.name!r}: {mock.call} {mock.path} is a model call, which "
                f"cannot be mocked: it is real or a fault; see {DOCS}"
            )


def narrowing(table: dict[str, object], where: str) -> tuple[str | None, str | None]:
    """The method and path a web mock or fault is narrowed to, if any."""
    method = None
    if "method" in table:
        written = text(table, "method", where)
        method = kept(written, labels.method_of(written, where))
    path = None
    if "path" in table:
        written = text(table, "path", where)
        path = kept(written, labels.path_of(written, where))
    return method, path


def read_web_mock(table: dict[str, object], call: str, where: str) -> Mock:
    check_keys(table, WEB_MOCK_KEYS, where)
    if "json" in table and "text" in table:
        raise ConfigError(f"{where}: give json or text, not both")
    status = table.get("status", 200)
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ConfigError(f"{where} status: expected an HTTP status, got {status!r}")
    headers: list[tuple[str, str]] = []
    for name, value in as_table(table.get("headers", {}), f"{where} headers").items():
        if not isinstance(value, str):
            raise ConfigError(f"{where} headers {name}: expected text, got {value!r}")
        if name.lower() in BODY_HEADERS:
            raise ConfigError(f"{where} headers {name}: is written from the body")
        if not f"{name}{value}".isascii() or not f"{name}{value}".isprintable():
            raise ConfigError(f"{where} headers {name}: must be printable ASCII")
        headers.append((name, value))
    method, path = narrowing(table, where)
    body = text(table, "text", where) if "text" in table else None
    recorded = read_recorded(table, where)
    return Mock(
        call, None, None, method, path, status, tuple(headers), table.get("json"), body, recorded
    )


def read_recorded(table: dict[str, object], where: str) -> bool:
    recorded = table.get("recorded", False)
    if not isinstance(recorded, bool):
        raise ConfigError(f"{where} recorded: expected true or false, got {redact(repr(recorded))}")
    return recorded


def read_args(table: dict[str, object], where: str) -> dict[str, object] | None:
    """The arguments a tool or MCP mock answers, exactly. With none, it answers every call."""
    if "args" not in table:
        return None
    args: object = table["args"]
    if not isinstance(args, dict):
        raise ConfigError(
            f"{where} args: expected a table of the call's arguments, got {redact(repr(args))}"
        )
    return as_table(cast(object, args), f"{where} args")


def read_mock(table: dict[str, object], where: str) -> Mock:
    written = text(table, "call", where)
    call = kept(written, labels.check(written, f"{where} call"))
    kind_of = labels.kind_of(call)
    if "returns_from" in table and kind_of in ("http", "mcp"):
        fix = "json or text" if kind_of == "http" else "returns"
        raise ConfigError(
            f"{where} returns_from: works on a tool mock only; this reply is JSON or text, "
            f"so write it with {fix}"
        )
    if kind_of == "http":
        return read_web_mock(table, call, where)
    check_keys(table, MCP_MOCK_KEYS if kind_of == "mcp" else MOCK_KEYS, where)
    recorded = read_recorded(table, where)
    args = read_args(table, where)
    if "returns_from" in table:
        for key in ("returns", "type"):
            if key in table:
                raise ConfigError(
                    f"{where} {key}: returns_from builds the value itself; remove {key}"
                )
        path = import_path(text(table, "returns_from", where), f"{where} returns_from")
        return Mock(call, None, None, recorded=recorded, args=args, returns_from=path)
    if "returns" not in table:
        raise ConfigError(f"{where} returns: required")
    kind = table.get("type")
    if kind is not None:
        kind = import_path(text(table, "type", where), f"{where} type")
    return Mock(call, table["returns"], kind, recorded=recorded, args=args)


def read_fault(table: dict[str, object], where: str) -> Fault:
    call = labels.check(text(table, "call", where), f"{where} call")
    kind = labels.kind_of(call)
    if table.get("do") in REPLY_FAULTS:
        reply_fault_on(call, str(table["do"]), "model" in table, where)
    # First, so text on a model fault is told why, not that the key is unknown.
    if kind == "model" and isinstance(table.get("do"), str) and carried(str(table["do"])):
        carried_on_a_model(call, str(table["do"]), "model" in table, where)
    known = {"http": WEB_FAULT_KEYS, "mcp": MCP_FAULT_KEYS, "model": MODEL_FAULT_KEYS}
    keys = known.get(kind, FAULT_KEYS)
    check_keys(table, keys, where)
    do = text(table, "do", where)
    if STATUS.fullmatch(do) and kind in ("tool", "mcp"):
        raise ConfigError(f"{where} do: {do!r} works on a model call or a web request; see {DOCS}")
    problem = word_problem(do)
    if problem is not None:
        raise ConfigError(f"{where} do: {problem}")
    if do == "error" and kind not in ("tool", "mcp"):
        raise ConfigError(f"{where} do: 'error' works only on a tool; see {DOCS}")
    if do == "empty" and kind not in ("tool", "mcp", "http"):
        raise ConfigError(
            f"{where} do: 'empty' works only on a tool, an MCP tool or a web request; for a "
            f"model reply that fails to parse, use bad body; see {DOCS}"
        )
    if do == "bad body" and kind in ("tool", "mcp"):
        raise ConfigError(
            f"{where} do: 'bad body' works on a model call or a web request, whose reply is a "
            f"body to parse; on a tool, fault the web request inside it; see {DOCS}"
        )
    if do == "context too long" and (kind != "model" or "/" in call):
        raise ConfigError(
            f"{where} do: 'context too long' works only on a chat model call, such as "
            f'call = "model", the one OpenAI sends it for; see {docs_url("model-faults")}'
        )
    if do in CONNECTION and kind in ("tool", "mcp"):
        raise ConfigError(
            f"{where} do: {do!r} works on a model call or a web request, where the connection "
            f'is made; on a tool, write do = "error" with raises = "{CONNECTION[do]}"; '
            f"see {DOCS}"
        )
    raises = read_raises(table, do, where)
    injected, find = read_change(table, do, where)
    when = str(table.get("when", "always"))
    if not SCHEDULE.fullmatch(when):
        # once and a bare number could each mean first 1 or call N, so neither is read.
        hint = (
            "; write 'first 1' for the first call alone or 'call N' for the Nth call alone"
            if when == "once" or when.isdigit()
            else ""
        )
        raise ConfigError(f"{where} when: {when!r} is not a schedule{hint}; see {DOCS}")
    if when.startswith("requests") and kind != "model":
        raise ConfigError(
            f"{where} when: {when!r} counts model requests and their retries; use 'first N' here"
        )
    method, path = narrowing(table, where) if kind == "http" else (None, None)
    forbid = read_forbid(table, where)
    if table.get("expect") == [] and not forbid:
        raise ConfigError(
            f"{where} expect: [] judges this row by its forbid alone, so give the row a forbid "
            f"with at least one text; otherwise write what the answer must hold; see {JUDGING}"
        )
    expect = read_expect(table, where, forbid_alone=True)
    # The agent still gets the data under append, and under a replace of part of it.
    gets_data = do == "append" or (do == "replace" and find is not None)
    if when == "always" and expect is None and not forbid and not gets_data:
        raise ConfigError(
            f"{where}: this fault fires on every call, so the agent never gets the data, yet the "
            "test's expect could still pass from general knowledge. Give the fault its own "
            f"expect or forbid, or use when = 'first N' for a recovery; see {JUDGING}"
        )
    rate = read_pass_rate(table, where)
    model = labels.model_name(table["model"], where) if "model" in table else None
    retry = table.get("retry_after")
    if "retry_after" in table and (problem := retry_after_problem(do, retry)):
        raise ConfigError(f"{where} retry_after: {problem}; see {DOCS}")
    wait = cast(int | None, retry)
    form = table.get("form")
    if "form" in table and (problem := form_problem(do, form)):
        raise ConfigError(f"{where} form: {problem}; see {DOCS}")
    tool = table.get("tool")
    if "tool" in table and (problem := tool_problem(do, tool)):
        raise ConfigError(f"{where} tool: {problem}; see {DOCS}")
    return Fault(
        call, do, when, raises, method, path, expect, forbid, rate, model, injected, find, wait,
        cast(str | None, form), cast(str | None, tool),
    )  # fmt: skip


def retry_after_problem(do: str, value: object) -> str | None:
    """Why a retry_after is not one this version sends; None when it is.

    The file's reader and the SDK's plan reader both ask this, so the two cannot drift."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return f"expected whole seconds from 0, such as 30, got {redact(repr(value))}"
    if do not in RETRY_AFTER:
        return (
            f'works only with do = "status 429" or "status 503", the replies a server sends '
            f"it on, not with {do!r}"
        )
    return None


def refusal_forms() -> tuple[str, ...]:
    """The forms of refuse any provider builds, each once, from the providers themselves."""
    found: dict[str, None] = {}
    for provider in built_in().providers:
        found.update(dict.fromkeys(provider.refusal_forms))
    return tuple(found)


def form_problem(do: str, value: object) -> str | None:
    """Why a form is not one this version builds; None when it is.

    The file's reader and the SDK's plan reader both ask this, so the two cannot drift."""
    forms = refusal_forms()
    if not isinstance(value, str) or value not in forms:
        named = " or ".join(f'"{form}"' for form in forms)
        return f"expected {named}, got {redact(repr(value))}"
    if do != "refuse":
        return f'works only with do = "refuse", not with {do!r}'
    return None


def tool_problem(do: str, value: object) -> str | None:
    """Why a tool is not a name unknown tool can call; None when it is.

    The file's reader and the SDK's plan reader both ask this, so the two cannot drift."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_TOOL_NAME
        or TOOL_NAME_CHARS.fullmatch(value) is None
    ):
        return (
            f"expected a tool name of 1 to {MAX_TOOL_NAME} letters, digits, underscores, "
            f"dashes or dots, as MCP allows, got {redact(repr(value))}"
        )
    if do != "unknown tool":
        return f'works only with do = "unknown tool", not with {do!r}'
    return None


def carried_on_a_model(call: str, do: str, named: bool, where: str) -> None:
    """replace changes a chat reply's text, and slow delays the call. Both need a chat call,
    and replace needs one model named, or it would change every model's answer."""
    word = do.partition(" ")[0]
    if word == "append":
        raise ConfigError(
            f"{where} do: append does not work on a model call; use replace on its reply, "
            f"or append to a tool result the model reads; see {docs_url('model-faults')}"
        )
    if "/" in call:  # model:openai/moderations and the other calls that only read or compute
        raise ConfigError(
            f"{where} do: {word} works on a chat call, not on {call}, whose reply is not text; "
            f"use timeout or status on it; see {docs_url('model-faults')}"
        )
    if word == "replace" and not named:
        raise ConfigError(
            f"{where} model: replace on a model call needs the model's name, or it would "
            "rewrite every model's answer, the main model's too; add model = \"...\""
        )


def reply_fault_on(call: str, do: str, named: bool, where: str) -> None:
    """A reply fault changes or makes a chat model's answer, so it needs a chat model call and
    one model named, as replace does."""
    if labels.kind_of(call) != "model" or "/" in call:
        raise ConfigError(
            f'{where} do: {do!r} works only on a chat model call, such as call = "model", '
            f"whose reply is the model's answer; see {docs_url('model-faults')}"
        )
    if not named:
        raise ConfigError(
            f"{where} model: {do} on a model call needs the model's name, or it would change "
            "every model's answer, the main model's too; add model = \"...\""
        )


def carried_model_warnings(config: Config) -> list[str]:
    """A replace or slow on a model call still makes the real call, billed. With the budget
    off nothing stops those calls but the run time."""
    if config.agent.max_cost_usd is not None:
        return []
    return [
        f"fault {i} of {test.name!r} makes the real model call, billed, and max_cost_usd is "
        '"none", so nothing stops the cost; give a self-hosted model a price of 0 under '
        "[agent.prices] and keep the budget on"
        for test in config.tests
        for i, fault in enumerate(test.faults, 1)
        # The reply faults that change the real reply stop before sending until they are
        # built, so they make no call to bill yet.
        if labels.kind_of(fault.call) == "model"
        and carried(fault.do)
        and fault.do not in REPLY_FAULTS
    ]


# What do may be, as a person writes it: status takes a code and slow takes seconds.
FAULT_WORDS = (
    *(word for word in FAULTS if word not in ("status", "slow")),
    "status <400 to 599>",
    "slow <seconds>s",
)


def suggest_word(text: str, words: Sequence[str]) -> str:
    """A did-you-mean for a word that is not one of words, or the whole list when none is close."""
    close = difflib.get_close_matches(text, words, n=1)
    if close:
        return f" (did you mean {close[0]!r}?)"
    return f" (the faults are {', '.join(words)})"


def word_problem(do: str) -> str | None:
    """Why do is not a fault word this version builds, whatever the call; None when it is.

    The file's reader and the SDK's plan reader both ask this, so the two cannot drift.
    """
    status = STATUS.fullmatch(do)
    if status:
        if not 400 <= int(status.group("code")) <= 599:
            return f"{do!r} is not an error status (400 to 599)"
        return None
    if do.startswith("slow"):
        seconds = slow_seconds(do)  # slow takes a fixed number of seconds, written with an s
        if seconds is None:
            return f"{do!r} is not a delay; write seconds with an s, such as 'slow 8s'"
        if not 0 < seconds <= MAX_SLOW_S:
            return f"{do!r} must be above 0 and at most {MAX_SLOW_S:g} seconds"
        return None
    if do not in FAULTS or do == "status":
        return f"{do!r} is not a fault{suggest_word(do, FAULT_WORDS)}; see {DOCS}"
    return None


def change_text(table: dict[str, object], key: str, where: str) -> str:
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{where} {key}: expected text, got {redact(repr(value))}")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_CHANGE_TEXT:
        raise ConfigError(f"{where} {key}: is over {MAX_CHANGE_TEXT // 1024} KiB")
    return value


def read_change(table: dict[str, object], do: str, where: str) -> tuple[str | None, str | None]:
    """The text and find of an append or a replace. Any other fault takes neither."""
    changes = do in ("append", "replace")
    for key in sorted(CHANGE_KEYS & table.keys()):
        if not changes:
            raise ConfigError(f"{where} {key}: works only with do = 'append' or 'replace'")
    if "find" in table and do != "replace":
        raise ConfigError(f"{where} find: works only with do = 'replace'")
    if not changes:
        return None, None
    if "text" not in table:
        raise ConfigError(f"{where} text: required with do = {do!r}")
    injected = change_text(table, "text", where)
    find = None
    if "find" in table:
        find = change_text(table, "find", where)
        if not find.strip():
            raise ConfigError(f"{where} find: expected text that is not blank")
    if do == "append" and not injected.strip():
        raise ConfigError(f"{where} text: blank text adds nothing; give the text to append")
    if do == "replace" and injected == "" and find is None:
        raise ConfigError(
            f"{where} text: an empty text replaces the whole result with nothing; give a find "
            "to delete part of it"
        )
    return injected, find


def takes_a_message(kind: object) -> bool:
    """An exception class that can be raised with a message alone."""
    if not (isinstance(kind, type) and issubclass(kind, Exception)):
        return False
    try:
        kind("message")
    except Exception:  # such as UnicodeDecodeError, which needs five arguments
        return False
    return True


def read_raises(table: dict[str, object], do: str, where: str) -> str | None:
    if "raises" not in table:
        return None
    if do != "error":
        raise ConfigError(f"{where} raises: works only with do = 'error'; see {DOCS}")
    raises = text(table, "raises", where)
    built_in = getattr(builtins, raises, None)
    if not takes_a_message(built_in) and not IMPORT_PATH.fullmatch(raises):
        raise ConfigError(
            f"{where} raises: {raises!r} is not a built-in exception or an import path "
            "of the form 'package.module:Class'"
        )
    return raises
