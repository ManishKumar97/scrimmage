"""Read derail.toml. Every bad value is reported with its table and key.

Only what this version supports is accepted. Anything else is rejected, never read
and ignored; docs/configuration.md lists what later versions add.
"""

import builtins
import math
import os
import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from derail_sdk import labels, provider_openai, python_args
from derail_sdk.errors import ConfigError
from derail_sdk.judge import Rules, impossible, normal
from derail_sdk.mocks import allowed
from derail_sdk.redact import redact

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
    "answer",
}
RULE_KEYS = {"expect", "forbid", "pass_rate"}
TEST_KEYS = {"name", "question", "mock", "fault", *RULE_KEYS}
MOCK_KEYS = {"call", "returns", "type"}
WEB_MOCK_KEYS = {"call", "method", "path", "status", "headers", "json", "text"}
FAULT_KEYS = {"call", "do", "when", "raises", *RULE_KEYS}
MODEL_FAULT_KEYS = {*FAULT_KEYS, "model"}
MCP_MOCK_KEYS = {"call", "returns"}
MCP_FAULT_KEYS = {"call", "do", "when", *RULE_KEYS}
WEB_FAULT_KEYS = {"call", "do", "when", "method", "path", *RULE_KEYS}
# A mock's body is sent as written, so these could only make the reply lie about it.
BODY_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}

DOCS = "docs/configuration.md"
JUDGING = "docs/judging.md"
# The faults this version builds. Each one has an entry in docs/fault-catalog.toml.
FAULTS = ("timeout", "status", "error", "empty")
STATUS = re.compile(r"status (?P<code>\d{3})")
SCHEDULE = re.compile(r"always|(?:first|requests) (?P<n>[1-9]\d*)")
MODEL_KEYS = {"host", "provider"}
DEFAULT_BUDGET_USD = 5.0
DEFAULT_RESERVE_OUTPUT_TOKENS = 4096
# Where the answer is in what the agent returns, such as messages[-1].content.
ANSWER_PATH = re.compile(r"[A-Za-z_]\w*(\[-?\d+\]|\.[A-Za-z_]\w*)*")
IMPORT_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
QUESTION = "{question}"  # in a command; the only placeholder filled in
MAX_ARGUMENT = 128 * 1024  # Linux's limit on one argument, with its closing NUL


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

    @property
    def target(self) -> str:
        """The call as a person reads it: two rows can differ by model alone."""
        return self.call if self.model is None else f"{self.call} {self.model}"


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
class Price:
    """Dollars per million tokens."""

    input: float
    output: float


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
    prices: tuple[tuple[str, Price], ...] = ()
    answer: str | None = None  # where the answer is in what the agent returns
    command: tuple[str, ...] = ()  # a script's arguments; function is then empty
    children: bool = False  # child processes fire faults, and join on any Python

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


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path.name}: the file is not UTF-8 text") from None
    except OSError as exc:
        raise ConfigError(f"{path.name}: cannot read it: {exc.strerror}") from None
    reject_placeholders(data, path.name)
    check_keys(data, {"agent", "test"}, path.name)
    if "agent" not in data:
        raise ConfigError(f"{path.name}: [agent] is required")
    tests = as_list(data.get("test", []), f"{path.name} [[test]]")
    if not tests:
        raise ConfigError(f"{path.name}: at least one [[test]] is required")
    agent = read_agent(as_table(data["agent"], "[agent]"))
    seen: set[str] = set()
    parsed: list[Test] = []
    for i, raw in enumerate(tests, 1):
        test = read_test(as_table(raw, f"[[test]] {i}"), i)
        reject_model_mocks(test, agent)
        if test.name in seen:
            raise ConfigError(f"[[test]] {test.name!r}: name is used twice")
        seen.add(test.name)
        parsed.append(test)
    config = Config(agent, tuple(parsed), path.parent)
    problems = check_references(config)
    if problems:
        raise ConfigError(*problems)
    return config


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
            f"[agent] command: {flag} keeps Python from running Derail's startup file, so the "
            "program would run with no gates; remove it"
        )
    problems += [p for test in config.tests if (p := bad_question(test, config.agent))]
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
        for entry in [*test.mocks, *test.faults]:
            if labels.kind_of(entry.call) != "tool":
                continue  # a model, web or MCP label cannot be compared with anything yet
            name = entry.call.partition(":")[2]
            if listed and name not in names:
                problems.append(
                    f"[[test]] {test.name!r}: {entry.call} names no tool in [agent] tools; "
                    f"the tools are {', '.join(names) or 'none'}"
                )
        paths = [m.type for m in test.mocks] + [f.raises for f in test.faults]
        for path in paths:
            if path and ":" in path and not allowed(path, config.agent.allowed_imports()):
                problems.append(
                    f"[[test]] {test.name!r}: type {path} is outside the agent's packages "
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


def bad_question(test: Test, agent: Agent) -> str | None:
    """A question goes as one argument, which a NUL ends and Linux limits."""
    where = f"[[test]] {test.name!r} question"
    if "\0" in test.question:
        return f"{where}: holds a NUL, which cannot go in an argument"
    parts = [part.replace(QUESTION, test.question) for part in agent.command] or [test.question]
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


def reject_placeholders(value: object, where: str) -> None:
    """Reject ${...} placeholders, and values a plan cannot carry, such as TOML dates."""
    if not isinstance(value, str | int | float | bool | list | dict):
        raise ConfigError(
            f"{where}: a {type(value).__name__} cannot be sent to the agent; write it as text"
        )
    if isinstance(value, str) and "${" in value:
        raise ConfigError(
            f"{where}: ${{...}} placeholders are not read, so the text would reach the agent as is"
        )
    if isinstance(value, dict):
        for key, item in as_table(cast(object, value), where).items():
            reject_placeholders(item, f"{where}.{key}")
    if isinstance(value, list):
        for item in as_list(cast(object, value), where):
            reject_placeholders(item, where)


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


def whole(table: dict[str, object], key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} {key}: expected a whole number of 1 or more, got {value!r}")
    return value


def read_agent(table: dict[str, object]) -> Agent:
    where = "[agent]"
    check_keys(table, AGENT_KEYS, where)
    if ("function" in table) == ("command" in table):
        raise ConfigError(f"{where}: give function or command, exactly one of them")
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
        real.append(labels.check(entry, f"{where} real", pattern=True))
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
    agent = Agent(
        function=function,
        runs=whole(table, "runs", 3, where),
        parallel=whole(table, "parallel", 4, where),
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
        answer=answer,
        command=read_command(table, where) if "command" in table else (),
        children=children,
    )
    names = agent.tool_names()
    for name in names:
        if names.count(name) > 1:
            raise ConfigError(f"{where} tools: two tools are named {name!r}")
    return agent


def read_command(table: dict[str, object], where: str) -> tuple[str, ...]:
    """A command's arguments. It is split here, so a question can never change the split."""
    try:
        parts = tuple(shlex.split(text(table, "command", where)))
    except ValueError as exc:
        raise ConfigError(f"{where} command: {exc}") from None
    if not any(QUESTION in part for part in parts):
        raise ConfigError(f"{where} command: must hold {QUESTION}, or the agent gets no question")
    return parts


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


def read_prices(value: object, where: str) -> tuple[tuple[str, Price], ...]:
    prices: list[tuple[str, Price]] = []
    for model, raw in as_table(value, where).items():
        item = f"{where} {model}"
        entry = as_table(raw, item)
        check_keys(entry, {"input", "output"}, item)
        parts: list[float] = []
        for key in ("input", "output"):
            number = entry.get(key)
            if (
                isinstance(number, bool)
                or not isinstance(number, int | float)
                or not math.isfinite(number)
                or number < 0
            ):
                raise ConfigError(f"{item} {key}: expected dollars per million tokens")
            parts.append(float(number))
        prices.append((model, Price(parts[0], parts[1])))
    return tuple(prices)


def read_tool(entry: object, where: str) -> tuple[str, str | None]:
    """A tools entry: an import path, or {path, name} when the model sees another name."""
    if isinstance(entry, str):
        return import_path(entry, where), None
    table = as_table(entry, where)
    check_keys(table, {"path", "name"}, where)
    name = text(table, "name", where)
    if not labels.TOOL_NAME.fullmatch(name):
        raise ConfigError(f"{where} name: {name!r} is not a tool name")
    return import_path(text(table, "path", where), where), name


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


def read_expect(table: dict[str, object], where: str) -> tuple[tuple[str, ...], ...] | None:
    """Each item is one text, or a list of texts of which any one will do."""
    if "expect" not in table:
        return None
    items = as_list(table["expect"], f"{where} expect")
    if not items:
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
    hosts = {*provider_openai.HOSTS, *(h.host for h in agent.model_hosts)}
    for mock in test.mocks:
        host = mock.call.removeprefix("http:")
        method = mock.method or "POST"
        if host in hosts and mock.path and provider_openai.op_for(mock.path, method):
            raise ConfigError(
                f"[[test]] {test.name!r}: {mock.call} {mock.path} is a model call, which "
                f"cannot be mocked: it is real or a fault; see {DOCS}"
            )


def narrowing(table: dict[str, object], where: str) -> tuple[str | None, str | None]:
    """The method and path a web mock or fault is narrowed to, if any."""
    method = labels.method_of(text(table, "method", where), where) if "method" in table else None
    path = labels.path_of(text(table, "path", where), where) if "path" in table else None
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
    return Mock(call, None, None, method, path, status, tuple(headers), table.get("json"), body)


def read_mock(table: dict[str, object], where: str) -> Mock:
    call = labels.check(text(table, "call", where), f"{where} call")
    if labels.kind_of(call) == "http":
        return read_web_mock(table, call, where)
    check_keys(table, MCP_MOCK_KEYS if labels.kind_of(call) == "mcp" else MOCK_KEYS, where)
    if "returns" not in table:
        raise ConfigError(f"{where} returns: required")
    kind = table.get("type")
    if kind is not None:
        kind = import_path(text(table, "type", where), f"{where} type")
    return Mock(call, table["returns"], kind)


def read_fault(table: dict[str, object], where: str) -> Fault:
    call = labels.check(text(table, "call", where), f"{where} call")
    kind = labels.kind_of(call)
    known = {"http": WEB_FAULT_KEYS, "mcp": MCP_FAULT_KEYS, "model": MODEL_FAULT_KEYS}
    keys = known.get(kind, FAULT_KEYS)
    check_keys(table, keys, where)
    do = text(table, "do", where)
    status = STATUS.fullmatch(do)
    if status:
        if kind in ("tool", "mcp"):
            raise ConfigError(
                f"{where} do: {do!r} works on a model call or a web request; see {DOCS}"
            )
        if not 400 <= int(status.group("code")) <= 599:
            raise ConfigError(f"{where} do: {do!r} is not an error status (400 to 599)")
    elif do not in FAULTS or do == "status":
        raise ConfigError(f"{where} do: {do!r} is not a fault; see {DOCS}")
    if do == "error" and kind not in ("tool", "mcp"):
        raise ConfigError(f"{where} do: 'error' works only on a tool; see {DOCS}")
    if do == "empty" and kind != "mcp":
        raise ConfigError(f"{where} do: 'empty' works only on an MCP tool; see {DOCS}")
    raises = read_raises(table, do, where)
    when = str(table.get("when", "always"))
    if not SCHEDULE.fullmatch(when):
        raise ConfigError(f"{where} when: {when!r} is not a schedule; see {DOCS}")
    if when.startswith("requests") and kind != "model":
        raise ConfigError(
            f"{where} when: {when!r} counts model requests and their retries; use 'first N' here"
        )
    method, path = narrowing(table, where) if kind == "http" else (None, None)
    expect, forbid = read_expect(table, where), read_forbid(table, where)
    if when == "always" and expect is None and not forbid:
        raise ConfigError(
            f"{where}: this fault fires on every call, so the agent never gets the data, yet the "
            "test's expect could still pass from general knowledge. Give the fault its own "
            f"expect or forbid, or use when = 'first N' for a recovery; see {JUDGING}"
        )
    rate = read_pass_rate(table, where)
    model = labels.model_name(table["model"], where) if "model" in table else None
    return Fault(call, do, when, raises, method, path, expect, forbid, rate, model)


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
