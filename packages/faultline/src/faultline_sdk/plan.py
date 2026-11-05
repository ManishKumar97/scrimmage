"""The plan: what the SDK does in one run. Data only, never code."""

from dataclasses import dataclass
from typing import cast

from faultline_sdk.config import Config, Fault, Mock, Test

PLAN_VERSION = 1


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

    def to_json(self) -> dict[str, object]:
        mocks: list[dict[str, object]] = []
        for m in self.mocks:
            mock: dict[str, object] = {"call": m.call, "returns": m.returns}
            if m.type is not None:
                mock["type"] = m.type
            mocks.append(mock)
        doc: dict[str, object] = {
            "v": PLAN_VERSION,
            "run": self.run,
            "test": self.test,
            "row": self.row,
            "mocks": mocks,
            "faults": [{"call": f.call, "do": f.do, "when": f.when} for f in self.faults],
            "tools": list(self.tools),
            "real": list(self.real),
            "time_limit_s": self.time_limit_s,
        }
        if self.renames:
            doc["renames"] = dict(self.renames)
        return doc

    @staticmethod
    def from_json(data: object) -> "Plan":
        """Read a plan sent by the control server. A bad plan raises ValueError."""
        doc = obj(data)
        if doc.get("v") != PLAN_VERSION:
            raise ValueError(f"plan version {doc.get('v')!r} is not {PLAN_VERSION}")
        mocks = tuple(
            Mock(string(m, "call"), m["returns"], optional(m, "type"))
            for m in map(obj, items(doc, "mocks"))
        )
        faults = tuple(
            Fault(string(f, "call"), string(f, "do"), string(f, "when"))
            for f in map(obj, items(doc, "faults"))
        )
        limit = doc.get("time_limit_s")
        if isinstance(limit, bool) or not isinstance(limit, int | float):
            raise ValueError("plan time_limit_s is not a number")
        return Plan(
            run=string(doc, "run"),
            test=string(doc, "test"),
            row=string(doc, "row"),
            mocks=mocks,
            faults=faults,
            tools=tuple(str(t) for t in items(doc, "tools")),
            real=tuple(str(r) for r in items(doc, "real")),
            time_limit_s=float(limit),
            renames=tuple((k, str(v)) for k, v in obj(doc.get("renames", {})).items()),
        )


@dataclass(frozen=True)
class Row:
    """One row of a test: the baseline, or the mocks plus exactly one fault."""

    test: Test
    name: str
    fault: Fault | None


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


def optional(doc: dict[str, object], key: str) -> str | None:
    return None if doc.get(key) is None else string(doc, key)
