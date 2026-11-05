"""Read faultline.toml. Every bad value is reported with its table and key.

A setting, fault, schedule or placeholder the design has but this version does not
build is rejected with "not supported yet", never read and ignored.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from faultline_sdk import labels
from faultline_sdk.errors import ConfigError, NotSupportedYet

AGENT_KEYS = {"function", "runs", "parallel", "run_timeout", "tools", "real"}
AGENT_LATER = {
    "command",
    "service",
    "max_cost_usd",
    "model_hosts",
    "answer",
    "body",
    "headers",
    "control",
    "control_tls",
    "reserve_output_tokens",
    "prices",
    "records",
    "record_grace_s",
    "record_wait_s",
}
TEST_KEYS = {"name", "question", "expect", "mock", "fault"}
MOCK_KEYS = {"call", "returns", "type"}
MOCK_LATER = {"returns_from", "method", "path", "status", "json", "text", "headers"}
FAULT_KEYS = {"call", "do", "when"}
FAULT_LATER = {"raises", "method", "path", "append", "replace", "retry_after"}

FAULTS_LATER = re.compile(
    r"status \d{3}|error|slow \d+(\.\d+)?s|empty|refused|reset|dns|bad body|auth error"
    r"|context too long|append|replace|garble|truncate|unknown tool|bad tool args|refuse"
)
WHEN_LATER = re.compile(r"once|call \d+|first \d+|requests \d+")
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


@dataclass(frozen=True)
class Config:
    agent: Agent
    tests: tuple[Test, ...]
    root: Path

    def tool_names(self) -> list[str]:
        return [path.rpartition(":")[2] for path in self.agent.tools]

    def allowed_imports(self) -> list[str]:
        """Prefixes the agent's process may import from: function's and tools' packages."""
        paths = [self.agent.function, *self.agent.tools]
        return sorted({path.split(".")[0].split(":")[0] + "." for path in paths})


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    reject_placeholders(data, path.name)
    check_keys(data, {"agent", "test"}, set(), path.name)
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
        raise NotSupportedYet(f"{where}: ${{...}} placeholders are not supported yet")
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


def check_keys(table: dict[str, object], known: set[str], later: set[str], where: str) -> None:
    for key in table:
        if key in later:
            raise NotSupportedYet(f"{where} {key}: not supported yet")
        if key not in known:
            raise ConfigError(f"{where} {key}: unknown setting")


def text(table: dict[str, object], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} {key}: expected text, got {value!r}")
    return value


def import_path(value: str, where: str) -> str:
    if not IMPORT_PATH.fullmatch(value):
        raise ConfigError(f"{where}: {value!r} is not an import path such as 'acme.agent:answer'")
    return value


def whole(table: dict[str, object], key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} {key}: expected a whole number of 1 or more, got {value!r}")
    return value


def read_agent(table: dict[str, object]) -> Agent:
    where = "[agent]"
    check_keys(table, AGENT_KEYS, AGENT_LATER, where)
    if "function" not in table:
        raise ConfigError(f"{where} function: required")
    timeout = table.get("run_timeout", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError(f"{where} run_timeout: expected a number of seconds, got {timeout!r}")
    tools = [
        import_path(str(t) if isinstance(t, str) else repr(t), f"{where} tools")
        for t in as_list(table.get("tools", []), f"{where} tools")
    ]
    names = [t.rpartition(":")[2] for t in tools]
    for name in names:
        if names.count(name) > 1:
            raise ConfigError(f"{where} tools: two tools are named {name!r}")
    real: list[str] = []
    for entry in as_list(table.get("real", []), f"{where} real"):
        if not isinstance(entry, str):
            raise ConfigError(f"{where} real: expected labels, got {entry!r}")
        real.append(str(labels.parse(entry, f"{where} real", pattern=True)))
    return Agent(
        function=import_path(text(table, "function", where), f"{where} function"),
        runs=whole(table, "runs", 3, where),
        parallel=whole(table, "parallel", 4, where),
        run_timeout=float(timeout),
        tools=tuple(tools),
        real=tuple(real),
    )


def read_test(table: dict[str, object], index: int) -> Test:
    where = f"[[test]] {index}"
    check_keys(table, TEST_KEYS, set(), where)
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
    check_keys(table, MOCK_KEYS, MOCK_LATER, where)
    call = str(labels.parse(text(table, "call", where), f"{where} call"))
    if "returns" not in table:
        raise ConfigError(f"{where} returns: required")
    kind = table.get("type")
    if kind is not None:
        kind = import_path(text(table, "type", where), f"{where} type")
    return Mock(call, table["returns"], kind)


def read_fault(table: dict[str, object], where: str) -> Fault:
    check_keys(table, FAULT_KEYS, FAULT_LATER, where)
    call = str(labels.parse(text(table, "call", where), f"{where} call"))
    do = text(table, "do", where)
    if do not in FAULTS:
        if FAULTS_LATER.fullmatch(do):
            raise NotSupportedYet(f"{where} do: {do!r} is not supported yet")
        raise ConfigError(f"{where} do: {do!r} is not a fault")
    when = str(table.get("when", "always"))
    if when != "always":
        if WHEN_LATER.fullmatch(when):
            raise NotSupportedYet(f"{where} when: {when!r} is not supported yet")
        raise ConfigError(f"{where} when: {when!r} is not a schedule")
    return Fault(call, do, when)
