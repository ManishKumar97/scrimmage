"""Fixed report inputs: configs and run results built by hand, with no process started.

Each case reaches a different part of the report, so its text can be compared byte for
byte with what the report printed before it was split into a report and its renderings.
The files under fixtures/report/ were printed by `scrimmage test`'s report at 533209e.
"""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from scrimmage_core import config, judge, plan
from scrimmage_core.budget import Held
from scrimmage_core.runner import RunResult

FOLDER = Path("/records/scrimmage-runs/20261107T101500Z-a1b2c3")
GOLDEN = Path(__file__).parent / "fixtures" / "report"
AGENT = """[agent]
function = "acme.agent:answer"
tools    = ["acme.tools:get_order", "acme.tools:get_stock"]
runs     = 3
"""
TEST = """
[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]
forbid   = ["refunded"]

[[test.mock]]
call    = "tool:get_stock"
returns = "12"
"""
FAULT = """
[[test.fault]]
call   = "tool:get_order"
do     = "timeout"
expect = [["unable", "sorry"]]
"""
GOOD = "It shipped, and arrives on Oct 30."
BAD = "It was refunded."
SORRY = "Sorry, I am unable to look it up."


@dataclass
class FakeLedger:
    held: list[Held] = field(default_factory=list[Held])

    def leaked(self) -> list[Held]:
        return self.held


@dataclass
class FakeRunner:
    """What the report reads from a runner, set by hand."""

    stopped_by: str | None = None
    label_problems: list[str] = field(default_factory=list[str])
    tools_seen: list[str] | None = None
    skipped: list[str] = field(default_factory=list[str])
    ledger: FakeLedger = field(default_factory=FakeLedger)


@dataclass
class Case:
    config: config.Config
    results: list[RunResult]
    runner: FakeRunner | None


def load(text: str) -> config.Config:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "scrimmage.toml"
        path.write_text(text)
        return config.load(path)


def run(
    row: plan.Row,
    n: int,
    outcome: str = "answered",
    answer: str | None = GOOD,
    *,
    fired: bool | None = None,
    **extra: object,
) -> RunResult:
    """One run's result, judged as the runner judges it."""
    fired = (True if row.fault is not None else None) if fired is None else fired
    chose = extra.pop("chose", None)
    result = RunResult(
        f"run-{row.name.replace(' ', '')}-{n}",
        row,
        outcome,
        answer,
        fired,
        {},
        model_calls=int(str(extra.pop("model_calls", 1))),
        cost_usd=float(str(extra.pop("cost_usd", 0.00012))),
        tool_calls=int(str(extra.pop("tool_calls", 2))),
        mocked={0},
    )
    for key, value in extra.items():
        setattr(result, key, value)
    result.judgement = judge.judge_run(
        outcome,
        answer,
        result.exercised,
        row.rules,
        bool(result.mistakes),
        None if chose is None else str(chose),
    )
    return result


def passing() -> Case:
    cfg = load(AGENT + TEST + FAULT)
    base, fault = plan.rows(cfg)
    results = [run(base, i) for i in range(3)] + [run(fault, i, answer=SORRY) for i in range(3)]
    return Case(cfg, results, FakeRunner())


def failing() -> Case:
    """Broken rules, a crash and a time-out, which name their logs."""
    cfg = load(AGENT + TEST + FAULT)
    base, fault = plan.rows(cfg)
    results = [run(base, 0), run(base, 1, answer=BAD), run(base, 2, answer="")]
    results += [
        run(fault, 0, "crashed", None),
        run(fault, 1, "timed out", None),
        run(fault, 2, answer=GOOD),
    ]
    return Case(cfg, results, FakeRunner())


def most_broken() -> Case:
    """Two rules broken, by one run and by two, the one-run rule met first."""
    cfg = load(AGENT + TEST)
    (base,) = plan.rows(cfg)
    results = [
        run(base, 0, answer="It shipped."),  # no date: one run breaks the date rule
        run(base, 1, answer="It arrives on Oct 30."),  # two runs break "shipped"
        run(base, 2, answer="It arrives on October 30."),
    ]
    return Case(cfg, results, FakeRunner())


def never_fired() -> Case:
    """A web fault under a tool mock that never fires, and a row with too few runs."""
    text = AGENT.replace("runs     = 3", "runs     = 4") + TEST.replace(
        'call    = "tool:get_stock"', 'call    = "tool:get_order"'
    )
    text += '\n[[test.fault]]\ncall = "http:orders.acme.internal"\ndo = "status 503"\n'
    text += 'forbid = ["Oct 30"]\n'
    cfg = load(text)
    base, fault = plan.rows(cfg)
    results = [run(base, 0), run(base, 1, "infrastructure error", None)]
    results += [run(fault, i, fired=False) for i in range(3)] + [run(fault, 3, "stopped", None)]
    return Case(cfg, results, FakeRunner())


def refused() -> Case:
    """A refused call under a fault, the agent's own choice, and one in a baseline."""
    cfg = load(AGENT + TEST + FAULT)
    base, fault = plan.rows(cfg)
    results = [
        run(base, 0),
        run(base, 1, "refused call", None, mistakes=["tool:refund_order is not mocked"]),
    ]
    results += [
        run(fault, 0, "refused call", None, chose="tool:refund_order"),
        run(fault, 1, "refused call", None, chose="process:python"),
        run(fault, 2, answer=SORRY),
    ]
    return Case(cfg, results, FakeRunner())


def budget() -> Case:
    """The budget stopped the test, a run left a hold, and the test spent past its limit."""
    cfg = load(AGENT.replace("runs     = 3", "max_cost_usd = 0.001\nparallel = 2") + TEST + FAULT)
    base, fault = plan.rows(cfg)
    numbers: dict[str, object] = {
        "usd": 0.0004,
        "spent": 0.0009,
        "held": 0.0003,
        "limit": 0.001,
        "waited_s": 30,
        "holds": [{"label": "model:openai", "run": "run-baseline-1", "usd": 0.0003}],
    }
    results = [
        run(base, 0, cost_usd=0.0006),
        run(base, 1, "not reserved", None, budget=numbers, budget_reason="budget_held"),
    ]
    results += [run(fault, 0, answer=SORRY, cost_usd=0.0006)]
    runner = FakeRunner(
        stopped_by="budget_held",
        ledger=FakeLedger([Held("run-baseline-1", "model:openai", 0.0003, ended=True)]),
    )
    return Case(cfg, results, runner)


def budget_reached() -> Case:
    cfg = load(AGENT.replace("runs     = 3", "max_cost_usd = 0.001") + TEST)
    (base,) = plan.rows(cfg)
    numbers: dict[str, object] = {"usd": 0.0004, "spent": 0.001, "held": 0, "limit": 0.001}
    results = [run(base, 0, cost_usd=0.001)]
    results += [run(base, 1, "budget reached", None, budget=numbers, budget_reason="budget")]
    return Case(cfg, results, FakeRunner(stopped_by="budget"))


def infra() -> Case:
    """A provider that failed by itself and a library Scrimmage cannot gate."""
    cfg = load(AGENT + TEST)
    (base,) = plan.rows(cfg)
    structure: dict[str, object] = {
        "notice": "structure",
        "library": "openai",
        "message": "openai 9.0 is newer than Scrimmage can gate",
    }
    results = [
        run(base, 0),
        run(base, 1, "infrastructure error", None, real_errors=["model:openai returned 500"]),
        run(base, 2, "infrastructure error", None, notices=[structure]),
        run(base, 3, "infrastructure error", None, notices=[structure]),  # said once
    ]
    return Case(cfg, results, FakeRunner())


WEB_MISS: dict[str, object] = {
    "call": "http:x.acme",
    "action": "real",
    "reason": "request",
    "seen": None,
    "method": "GET",
    "path": "/a",
    "nearest": {
        "mock": 0,
        "args": None,
        "method": "GET",
        "path": "/b",
        "differs": ["path"],
        "differs_left_out": 0,
    },
}


def notes_and_warnings() -> Case:
    """tool:*, long names, notices, near mocks, hosted reads and a silent run."""
    name = "a-test-whose-name-is-far-too-long-for-the-table-" * 2
    text = AGENT.replace(
        'tools    = ["acme.tools:get_order", "acme.tools:get_stock"]\n', 'real = ["tool:*"]\n'
    )
    text += TEST.replace("order-status", name)
    text += '\n[[test.fault]]\ncall = "model"\ndo = "status 429"\nmodel = "gpt-4.1"\n'
    text += 'when = "first 2"\nforbid = ["Oct 30"]\n'
    cfg = load(text)
    base, fault = plan.rows(cfg)
    left: dict[str, object] = {"notice": "left_group", "message": "process 41 left the group"}
    newer: dict[str, object] = {"notice": "newer_version", "message": "httpx 2.1 is newer"}
    results = [
        run(base, 0, notices=[left, newer], misses=[WEB_MISS], hosted_reads=2),
        run(base, 1, model_calls=0),
        run(base, 2, "crashed", None),
    ]
    results += [
        run(fault, i, fired=False, models={("model:openai", "gpt-4o-mini")}) for i in range(3)
    ]
    runner = FakeRunner(tools_seen=["get_stock"])
    return Case(cfg, results, runner)


def labels_unchecked() -> Case:
    """No tools list, no run reported the tools, and a baseline that failed."""
    text = AGENT.replace(
        'tools    = ["acme.tools:get_order", "acme.tools:get_stock"]\n', 'max_cost_usd = "none"\n'
    )
    cfg = load(text + TEST + FAULT)
    base, _ = plan.rows(cfg)
    results = [run(base, i, "crashed", None, model_calls=0) for i in range(3)]
    runner = FakeRunner(
        label_problems=["[[test]] 'order-status': tool:get_ordr names no tool the agent has"],
        skipped=["order-status"],
    )
    return Case(cfg, results, runner)


def no_model_call() -> Case:
    """Every baseline run answered with no model call, so the budget proves nothing."""
    cfg = load(AGENT + TEST)
    (base,) = plan.rows(cfg)
    return Case(cfg, [run(base, i, model_calls=0) for i in range(3)], None)


def empty() -> Case:
    """No run at all, as when every row was stopped before it started."""
    return Case(load(AGENT + TEST + FAULT), [], None)


CASES: dict[str, Callable[[], Case]] = {
    "passing": passing,
    "failing": failing,
    "most_broken": most_broken,
    "never_fired": never_fired,
    "refused": refused,
    "budget": budget,
    "budget_reached": budget_reached,
    "infra": infra,
    "notes_and_warnings": notes_and_warnings,
    "labels_unchecked": labels_unchecked,
    "no_model_call": no_model_call,
    "empty": empty,
}
