"""The plan: what the SDK does in one run. Data only, never code."""

import math
from dataclasses import dataclass
from typing import cast

from scrimmage_core import labels
from scrimmage_core.config import (
    DEFAULT_RESERVE_OUTPUT_TOKENS,
    MAX_GRACE_S,
    SCHEDULE,
    Config,
    Fault,
    Mock,
    ModelHost,
    Test,
    retry_after_problem,
    word_problem,
)
from scrimmage_core.judge import Rules
from scrimmage_core.provider_openai import Rates

PLAN_VERSION = 1
# The keys a fault has in a plan. A runner of a later version may send one this SDK does not
# know, and running the fault without it would run a fault nobody asked for.
PLAN_FAULT_KEYS = frozenset(
    {"call", "do", "when", "raises", "method", "path", "model", "text", "find", "retry_after"}
)


@dataclass(frozen=True)
class Plan:
    run: str
    test: str
    row: str
    mocks: tuple[Mock, ...]
    faults: tuple[Fault, ...]
    tools: tuple[str, ...]
    real: tuple[str, ...]
    time_limit_s: float
    # import path -> the name the model sees, for tools whose two names differ
    renames: tuple[tuple[str, str], ...] = ()
    model_hosts: tuple[ModelHost, ...] = ()
    budget_usd: float | None = None  # None: no budget, so no reservations
    reserve_output_tokens: int = DEFAULT_RESERVE_OUTPUT_TOKENS
    prices: tuple[tuple[str, Rates], ...] = ()
    children: bool = False  # child processes fire faults too
    unavailable: str | None = None  # why a child has no plan; such a plan is never sent
    record: bool = False  # scrimmage record: real calls' lines carry what a mock needs
    probe: bool = False  # scrimmage init --probe: the first model request ends the run
    # How long the run had been going when the runner sent this copy, on the runner's clock.
    # The runner adds it to each copy it serves, so the SDK's time limit counts from the
    # runner's start, however long the program took to load.
    elapsed_s: float = 0.0
    # The values ${NAME} filled in, which the SDK removes from every line and side-folder item
    # before either is hashed. Sent with the plan only; never written to a file.
    scrub: tuple[str, ...] = ()
    service: bool = False  # a run of an agent inside a web service, one request long
    record_grace_s: float = 0.0  # in a service: the record stays open this long after the reply
    # In a service: calls work outside any request may make for real while this run is open.
    background_real: tuple[str, ...] = ()

    def tool_names(self) -> list[str]:
        """Every tool's final name: the one the model sees."""
        renames = dict(self.renames)
        return [labels.tool_name(path, renames) for path in self.tools]

    def to_json(self) -> dict[str, object]:
        doc: dict[str, object] = {
            "v": PLAN_VERSION,
            "run": self.run,
            "test": self.test,
            "row": self.row,
            "mocks": [mock_json(m) for m in self.mocks],
            "faults": [fault_json(f) for f in self.faults],
            "tools": list(self.tools),
            "real": list(self.real),
            "children": self.children,
            "time_limit_s": self.time_limit_s,
            "model_hosts": [{"host": h.host, "provider": h.provider} for h in self.model_hosts],
            "budget": {
                "limit_usd": self.budget_usd,
                "reserve_output_tokens": self.reserve_output_tokens,
                "prices": {m: price_json(p) for m, p in self.prices},
            },
        }
        if self.renames:
            doc["renames"] = dict(self.renames)
        if self.record:
            doc["record"] = True
        if self.probe:
            doc["probe"] = True
        if self.elapsed_s:
            doc["elapsed_s"] = self.elapsed_s
        if self.scrub:
            doc["scrub"] = list(self.scrub)
        if self.service:
            doc["service"] = True
            doc["record_grace_s"] = self.record_grace_s
            doc["background_real"] = list(self.background_real)
        return doc

    @staticmethod
    def from_json(data: object) -> "Plan":
        """Read a plan sent by the control server. A bad plan raises ValueError."""
        doc = obj(data)
        if doc.get("v") != PLAN_VERSION:
            raise ValueError(f"plan version {doc.get('v')!r} is not {PLAN_VERSION}")
        mocks = tuple(mock_from(m) for m in map(obj, items(doc, "mocks")))
        faults = tuple(fault_from(f) for f in map(obj, items(doc, "faults")))
        limit = number(doc, "time_limit_s")
        budget = obj(doc.get("budget", {}))
        prices = tuple(
            (model, price_from(obj(p))) for model, p in obj(budget.get("prices", {})).items()
        )
        hosts = tuple(
            ModelHost(string(h, "host"), string(h, "provider"))
            for h in map(obj, items(doc, "model_hosts"))
        )
        reserve = int(number(budget, "reserve_output_tokens"))
        limit_usd = None if budget.get("limit_usd") is None else number(budget, "limit_usd")
        elapsed = number(doc, "elapsed_s") if "elapsed_s" in doc else 0.0
        if not elapsed >= 0:  # NaN too: a negative age would stretch the time limit
            raise ValueError("plan elapsed_s is negative")
        scrub = tuple(str(value) for value in items(doc, "scrub")) if "scrub" in doc else ()
        grace = number(doc, "record_grace_s") if "record_grace_s" in doc else 0.0
        if not 0 <= grace <= MAX_GRACE_S:
            raise ValueError("plan record_grace_s is out of range")
        return Plan(
            run=string(doc, "run"),
            test=string(doc, "test"),
            row=string(doc, "row"),
            mocks=mocks,
            faults=faults,
            tools=tuple(str(t) for t in items(doc, "tools")),
            real=tuple(str(r) for r in items(doc, "real")),
            time_limit_s=limit,
            renames=tuple((k, str(v)) for k, v in obj(doc.get("renames", {})).items()),
            model_hosts=hosts,
            budget_usd=limit_usd,
            reserve_output_tokens=reserve,
            prices=prices,
            children=doc.get("children") is True,
            record=doc.get("record") is True,
            probe=doc.get("probe") is True,
            elapsed_s=elapsed,
            scrub=scrub,
            service=doc.get("service") is True,
            record_grace_s=grace,
            background_real=tuple(str(r) for r in items(doc, "background_real"))
            if "background_real" in doc
            else (),
        )


def price_json(price: Rates) -> dict[str, float]:
    doc = {"input": price.input, "output": price.output}
    if price.cached_input is not None:
        doc["cached_input"] = price.cached_input
    return doc


def price_from(doc: dict[str, object]) -> Rates:
    cached = None if doc.get("cached_input") is None else number(doc, "cached_input")
    return Rates(number(doc, "input"), number(doc, "output"), cached)


def tools_text(tools: tuple[str, ...], renames: tuple[tuple[str, str], ...]) -> str:
    """The tools list for SCRIMMAGE_TOOLS: path, or path=name when the model sees another name."""
    names = dict(renames)
    return ",".join(f"{p}={names[p]}" if p in names else p for p in tools)


def tools_from(text: str) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """SCRIMMAGE_TOOLS back as a plan's tools and renames."""
    entries = [entry.partition("=") for entry in text.split(",") if entry]
    return (
        tuple(path for path, _, _ in entries),
        tuple((path, name) for path, _, name in entries if name),
    )


def no_plan(run: str, why: str) -> Plan:
    """What a child holds when it cannot get its plan. RunState refuses each call on it."""
    return Plan(run, "unknown", "unknown", (), (), (), (), math.inf, unavailable=why)


@dataclass(frozen=True)
class Row:
    """One row of a test: the baseline, or the mocks plus exactly one fault."""

    test: Test
    name: str
    fault: Fault | None

    @property
    def rules(self) -> Rules:
        return self.test.rules_for(self.fault)


def mock_json(mock: Mock) -> dict[str, object]:
    doc = plain_mock_json(mock)
    if mock.recorded:
        doc["recorded"] = True
    if mock.args is not None:
        doc["args"] = mock.args
    return doc


def plain_mock_json(mock: Mock) -> dict[str, object]:
    if mock.returns_from is not None:
        return {"call": mock.call, "returns_from": mock.returns_from}
    if labels.kind_of(mock.call) != "http":
        doc: dict[str, object] = {"call": mock.call, "returns": mock.returns}
        if mock.type is not None:
            doc["type"] = mock.type
        return doc
    doc = {"call": mock.call, "status": mock.status}
    for key, value in (
        ("method", mock.method),
        ("path", mock.path),
        ("json", mock.json),
        ("text", mock.text),
    ):
        if value is not None:
            doc[key] = value
    if mock.headers:
        doc["headers"] = dict(mock.headers)
    return doc


def mock_from(doc: dict[str, object]) -> Mock:
    call = string(doc, "call")
    recorded = doc.get("recorded") is True
    if labels.kind_of(call) != "http":
        args = None if doc.get("args") is None else obj(doc["args"])
        if (made := optional(doc, "returns_from")) is not None:
            return Mock(call, None, None, recorded=recorded, args=args, returns_from=made)
        return Mock(call, doc["returns"], optional(doc, "type"), recorded=recorded, args=args)
    return Mock(
        call,
        None,
        None,
        optional(doc, "method"),
        optional(doc, "path"),
        int(number(doc, "status")),
        tuple((k, str(v)) for k, v in obj(doc.get("headers", {})).items()),
        doc.get("json"),
        optional(doc, "text"),
        recorded,
    )


def fault_json(fault: Fault) -> dict[str, object]:
    doc: dict[str, object] = {"call": fault.call, "do": fault.do, "when": fault.when}
    for key, value in (
        ("raises", fault.raises),
        ("method", fault.method),
        ("path", fault.path),
        ("model", fault.model),
        ("text", fault.text),
        ("find", fault.find),
        ("retry_after", fault.retry_after),
    ):
        if value is not None:
            doc[key] = value
    return doc


def fault_from(doc: dict[str, object]) -> Fault:
    """A plan's fault, refused when this SDK cannot run it as the runner meant it."""
    unknown = sorted(set(doc) - PLAN_FAULT_KEYS)
    if unknown:
        raise ValueError(f"plan fault key {unknown[0]!r} is not one this scrimmage knows")
    do, when = string(doc, "do"), string(doc, "when")
    if word_problem(do) is not None:
        raise ValueError(f"plan fault {do!r} is not a fault this scrimmage runs")
    if not SCHEDULE.fullmatch(when):
        raise ValueError(f"plan schedule {when!r} is not one this scrimmage runs")
    retry = doc.get("retry_after")  # null is none, as for the optional keys
    if retry is not None and (problem := retry_after_problem(do, retry)):
        raise ValueError(f"plan fault retry_after: {problem}")
    return Fault(
        string(doc, "call"),
        do,
        when,
        optional(doc, "raises"),
        optional(doc, "method"),
        optional(doc, "path"),
        model=optional(doc, "model"),
        text=optional(doc, "text"),
        find=optional(doc, "find"),
        retry_after=cast(int | None, retry),
    )


def rows(config: Config) -> list[Row]:
    out: list[Row] = []
    for test in config.tests:
        out.append(Row(test, "baseline", None))
        out += [Row(test, f"fault {i}", f) for i, f in enumerate(test.faults, 1)]
    return out


def plan_for(config: Config, row: Row, run: str) -> Plan:
    return Plan(
        run=run,
        test=row.test.name,
        row=row.name,
        mocks=row.test.mocks,
        faults=() if row.fault is None else (row.fault,),
        tools=config.agent.tools,
        real=config.agent.real,
        time_limit_s=config.agent.run_timeout,
        renames=config.agent.renames,
        model_hosts=config.agent.model_hosts,
        budget_usd=config.agent.max_cost_usd,
        reserve_output_tokens=config.agent.reserve_output_tokens,
        prices=config.agent.prices,
        children=config.agent.children,
        scrub=config.scrub,
        service=bool(config.agent.service),
        record_grace_s=config.agent.record_grace_s if config.agent.service else 0.0,
        background_real=config.agent.background_real,
    )


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def items(doc: dict[str, object], key: str) -> list[object]:
    value = doc.get(key)
    if not isinstance(value, list):
        raise ValueError(f"plan {key} is not a list")
    return list(cast(list[object], value))


def string(doc: dict[str, object], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str):
        raise ValueError(f"plan {key} is not text")
    return value


def number(doc: dict[str, object], key: str) -> float:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"plan {key} is not a number")
    return float(value)


def optional(doc: dict[str, object], key: str) -> str | None:
    return None if doc.get(key) is None else string(doc, key)
