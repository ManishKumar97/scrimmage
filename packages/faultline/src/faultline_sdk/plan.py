"""The plan: what the SDK does in one run. Data only, never code."""

from dataclasses import dataclass
from typing import cast

from faultline_sdk import labels
from faultline_sdk.config import (
    DEFAULT_RESERVE_OUTPUT_TOKENS,
    Config,
    Fault,
    Mock,
    ModelHost,
    Price,
    Test,
)

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
    model_hosts: tuple[ModelHost, ...] = ()
    budget_usd: float | None = None  # None: no budget, so no reservations
    reserve_output_tokens: int = DEFAULT_RESERVE_OUTPUT_TOKENS
    prices: tuple[tuple[str, Price], ...] = ()

    def tool_names(self) -> list[str]:
        """Every tool's final name: the one the model sees."""
        renames = dict(self.renames)
        return [labels.tool_name(path, renames) for path in self.tools]

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
            "model_hosts": [{"host": h.host, "provider": h.provider} for h in self.model_hosts],
            "budget": {
                "limit_usd": self.budget_usd,
                "reserve_output_tokens": self.reserve_output_tokens,
                "prices": {m: {"input": p.input, "output": p.output} for m, p in self.prices},
            },
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
        limit = number(doc, "time_limit_s")
        budget = obj(doc.get("budget", {}))
        prices = tuple(
            (model, Price(number(obj(p), "input"), number(obj(p), "output")))
            for model, p in obj(budget.get("prices", {})).items()
        )
        hosts = tuple(
            ModelHost(string(h, "host"), string(h, "provider"))
            for h in map(obj, items(doc, "model_hosts"))
        )
        reserve = int(number(budget, "reserve_output_tokens"))
        limit_usd = None if budget.get("limit_usd") is None else number(budget, "limit_usd")
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
        model_hosts=config.agent.model_hosts,
        budget_usd=config.agent.max_cost_usd,
        reserve_output_tokens=config.agent.reserve_output_tokens,
        prices=config.agent.prices,
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
