"""Read faultline.toml. Every bad value is reported with its table and key.

Only what this version supports is accepted. Anything else is rejected, never read
and ignored; docs/configuration.md lists what later versions add.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from faultline_sdk import labels
from faultline_sdk.errors import ConfigError

AGENT_KEYS = {"function", "runs", "parallel", "run_timeout", "tools", "real"}
TEST_KEYS = {"name", "question", "expect", "mock", "fault"}
MOCK_KEYS = {"call", "returns", "type"}
FAULT_KEYS = {"call", "do", "when"}

DOCS = "docs/configuration.md"
# The faults this version builds. Each one has an entry in docs/fault-catalog.toml.
FAULTS = ("timeout",)
IMPORT_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")


@dataclass(frozen=True)
class Mock:
    call: str
    returns: object
    type: str | None


@dataclass(frozen=True)
class Fault:
    call: str
    do: str
    when: str


@dataclass(frozen=True)
class Test:
    name: str
    question: str
    expect: tuple[str, ...]
    mocks: tuple[Mock, ...]
    faults: tuple[Fault, ...]


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

    def tool_names(self) -> list[str]:
        """Every tool's final name: the one the model sees."""
        renames = dict(self.renames)
        return [renames.get(path, path.rpartition(":")[2]) for path in self.tools]

    def allowed_imports(self) -> list[str]:
        """Prefixes the agent's process may import from: function's and tools' packages."""
        paths = [self.function, *self.tools]
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
        if test.name in seen:
            raise ConfigError(f"[[test]] {test.name!r}: name is used twice")
        seen.add(test.name)
        parsed.append(test)
    return Config(agent, tuple(parsed), path.parent)


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
    if "function" not in table:
        raise ConfigError(f"{where} function: required")
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
        real.append(str(labels.parse(entry, f"{where} real", pattern=True)))
    agent = Agent(
        function=import_path(text(table, "function", where), f"{where} function"),
        runs=whole(table, "runs", 3, where),
        parallel=whole(table, "parallel", 4, where),
        run_timeout=float(timeout),
        tools=tuple(tools),
        real=tuple(real),
        renames=tuple(renames),
    )
    names = agent.tool_names()
    for name in names:
        if names.count(name) > 1:
            raise ConfigError(f"{where} tools: two tools are named {name!r}")
    return agent


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
    if "expect" not in table:
        raise ConfigError(f"{where} expect: required")
    expect = as_list(table["expect"], f"{where} expect")
    if not expect or not all(isinstance(e, str) for e in expect):
        raise ConfigError(f"{where} expect: expected a list of text, got {expect!r}")
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
        expect=tuple(str(e) for e in expect),
        mocks=tuple(mocks),
        faults=tuple(faults),
    )


def read_mock(table: dict[str, object], where: str) -> Mock:
    check_keys(table, MOCK_KEYS, where)
    call = str(labels.parse(text(table, "call", where), f"{where} call"))
    if "returns" not in table:
        raise ConfigError(f"{where} returns: required")
    kind = table.get("type")
    if kind is not None:
        kind = import_path(text(table, "type", where), f"{where} type")
    return Mock(call, table["returns"], kind)


def read_fault(table: dict[str, object], where: str) -> Fault:
    check_keys(table, FAULT_KEYS, where)
    call = str(labels.parse(text(table, "call", where), f"{where} call"))
    do = text(table, "do", where)
    if do not in FAULTS:
        raise ConfigError(f"{where} do: {do!r} is not a fault; see {DOCS}")
    when = str(table.get("when", "always"))
    if when != "always":
        raise ConfigError(f"{where} when: {when!r} is not a schedule; see {DOCS}")
    return Fault(call, do, when)
