"""Measure what Scrimmage adds, and print it: uv run python tests/benchmark.py

Each number is a real run of the real code: gated calls go through the real gates, a real
control server and the real sender thread, against a local server in its own process.
Rounds alternate the plain and gated forms of each measure, so a change in load hits
both alike, and the spread across rounds is printed beside each number.
"""

import argparse
import contextlib
import json
import os
import pstats
import re
import secrets
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import benchmark_agent
from fakes import fast_server
from scrimmage_core.control import ControlServer
from scrimmage_core.plan import Plan
from scrimmage_core.records import RecordStore

HERE = Path(__file__).parent
FAST_SERVER = Path(fast_server.__file__)
DOCS_SERVER = HERE / "fakes" / "docs_server.py"
PYTHON = sys.executable
SCRIMMAGE = str(Path(PYTHON).with_name("scrimmage"))
# (name in the table, the case's name in benchmark_agent, whether a plain form exists)
CALLS: tuple[tuple[str, str, bool], ...] = (
    ("tool call, sync", "tool_sync", True),
    ("tool call, async", "tool_async", True),
    ("tool call, mocked (sync)", "tool_mock", False),
    ("HTTP through httpx, sync", "http_sync", True),
    ("HTTP through httpx, async", "http_async", True),
    ("HTTP through httpx, mocked (sync)", "http_mock", False),
    ("model call, sync, budget on", "model_sync", True),
    ("model call, async, budget on", "model_async", True),
    ("MCP tool call, stdio, async", "mcp_async", True),
)
NO_BUDGET_CASES = ("model_sync", "model_async")
NO_BUDGET_NAMES = {
    "model_sync": "model call, sync, budget off",
    "model_async": "model call, async, budget off",
}
GATED_CASES = ",".join(case for _, case, _ in CALLS)
PLAIN_CASES = ",".join(case for _, case, has_plain in CALLS if has_plain)
FIRST_LINE = "import time; print(time.time_ns())"


# The enforced ceilings: what a change may never cost, in the unit the name says.
# Each is several times today's cost on a quiet machine, and over the worst seen under
# load (docs/benchmarks.md), so a busy machine does not reach it. See the same page for
# the budget each number should come down to, and why.
CALL_BUDGETS_US = {
    "tool_sync": 500.0,  # 62 µs quiet
    "tool_async": 500.0,
    "http_sync": 8000.0,  # 404 µs quiet, 2.8 ms seen under load
    "http_async": 8000.0,  # 553 µs quiet, 2.3 ms seen under load
    "model_sync": 20000.0,  # 1.8 ms quiet with the budget on, 9 ms seen under load
    "model_async": 20000.0,  # 2.2 ms quiet, 6 ms seen under load
    "mcp_async": 6000.0,  # 300 µs quiet, 2 ms seen under load
}
PROCESS_BUDGETS_MS = {
    "startup, first line": 800.0,  # 106 ms quiet, 318 ms seen under load
    "startup, whole process": 1500.0,  # 226 ms quiet, 664 ms seen under load
    "scrimmage check": 800.0,  # 91 ms quiet, 314 ms seen under load
    "scrimmage --help": 800.0,  # 91 ms quiet, 375 ms seen under load
    "import scrimmage_core.cli": 800.0,  # 88 ms quiet, 301 ms seen under load
}
RUN_BUDGET_MS = {"run": 2000.0}  # 255 ms quiet, 802 ms seen under load


@dataclass(frozen=True)
class Servers:
    base: str
    mcp: str  # the MCP server's arguments, split on |, as the agent passes them on


def toml(*, budget: str, mocks: bool) -> str:
    """The scrimmage.toml of the bench agent. A mock that goes unused is a test mistake."""
    tools = ", ".join(
        f'"benchmark_agent:{name}"' for name in ("tool_sync", "tool_async", "tool_mock")
    )
    return textwrap.dedent(f"""\
        [agent]
        function = "benchmark_agent:answer"
        tools = [{tools}]
        real = ["tool:tool_sync", "tool:tool_async", "http:127.0.0.1", "mcp:docs/search"]
        model_hosts = [{{host = "127.0.0.1", provider = "openai"}}]
        runs = 1
        parallel = 1
        run_timeout = 900
        max_cost_usd = {budget}

        [[test]]
        name = "bench"
        question = "go"
        expect = ["ok"]

        """) + (
        textwrap.dedent("""\

        [[test.mock]]
        call = "tool:tool_mock"
        returns = {order_id = "A1", status = "mocked"}

        [[test.mock]]
        call = "http:mocked.invalid"
        json = {status = "mocked"}
        """)
        if mocks
        else ""
    )


def noop_project(folder: Path, runs: int) -> Path:
    """A project whose agent does nothing, not even an import, with no gated calls."""
    folder.mkdir()
    (folder / "noop_agent.py").write_text('def answer(question):\n    return "ok"\n')
    config = folder / "scrimmage.toml"
    config.write_text(
        f'[agent]\nfunction = "noop_agent:answer"\nruns = {runs}\nparallel = 1\n'
        'max_cost_usd = "none"\n\n[[test]]\nname = "bench"\nquestion = "go"\nexpect = ["ok"]\n'
    )
    return config


def env_with(extra: dict[str, str]) -> dict[str, str]:
    """The agent's environment. Scrimmage's own variables never leak in from a test run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    env["PYTHONPATH"] = str(HERE)
    return {**env, **extra}


def start_server(args: list[str]) -> tuple[subprocess.Popen[bytes], str]:
    """Start a server process and read the port it prints."""
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    port = proc.stdout.readline().decode().strip()
    return proc, port


def call_env(
    servers: Servers, cases: str, out: Path, *, paired: bool = False, profile: Path | None = None
) -> dict[str, str]:
    env: dict[str, str] = {
        "BENCH_BASE": servers.base,
        "BENCH_MCP": servers.mcp,
        "BENCH_CASES": cases,
        "BENCH_OUT": str(out),
    }
    if paired:
        env["BENCH_PAIRED"] = "1"
    if profile is not None:
        env["BENCH_PROFILE"] = str(profile)
    return env_with(env)


def run_plain(servers: Servers, out: Path, cases: str) -> dict[str, dict[str, list[int]]]:
    code = "import benchmark_agent; benchmark_agent.answer('go')"
    env = call_env(servers, cases, out)
    subprocess.run([PYTHON, "-c", code], env=env, cwd=out.parent, check=True)
    return json.loads(out.read_text())


def run_gated(
    servers: Servers,
    out: Path,
    cases: str,
    budget: str,
    *,
    paired: bool = False,
    profile: Path | None = None,
) -> dict[str, dict[str, list[int]]]:
    """The agent under a real scrimmage test."""
    project = out.parent / f"project-{out.stem}"
    project.mkdir()
    mocks = "tool_mock" in cases
    (project / "scrimmage.toml").write_text(toml(budget=budget, mocks=mocks))
    env = call_env(servers, cases, out, paired=paired, profile=profile)
    result = subprocess.run(
        [SCRIMMAGE, "test", str(project / "scrimmage.toml")],
        env=env,
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(out.read_text())


def pct(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]


def us(value_ns: float) -> float:
    return value_ns / 1000


@dataclass
class CallRow:
    name: str
    plain_p50: list[float]
    plain_p99: list[float]
    gated_p50: list[float]
    gated_p99: list[float]
    has_plain: bool

    def added(self, gated: list[float], plain: list[float]) -> list[float]:
        return [g - p for g, p in zip(gated, plain, strict=True)]


def spread(values: list[float]) -> str:
    return f"{min(values):.1f}..{max(values):.1f}"


def med(values: list[float]) -> float:
    return statistics.median(values)


def call_table(rows: list[CallRow]) -> str:
    head = (
        "| Call | Ungated p50 µs | Gated p50 µs | Added p50 µs (spread) "
        "| Ungated p99 µs | Gated p99 µs | Added p99 µs (spread) |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    body = ""
    for row in rows:
        if not row.has_plain:
            body += (
                f"| {row.name} | no real call | {med(row.gated_p50):.1f} "
                f"| n/a (spread {spread(row.gated_p50)}) | no real call | "
                f"{med(row.gated_p99):.1f} | n/a |\n"
            )
            continue
        p50 = row.added(row.gated_p50, row.plain_p50)
        p99 = row.added(row.gated_p99, row.plain_p99)
        body += (
            f"| {row.name} | {med(row.plain_p50):.1f} | {med(row.gated_p50):.1f} "
            f"| {med(p50):.1f} ({spread(p50)}) | {med(row.plain_p99):.1f} "
            f"| {med(row.gated_p99):.1f} | {med(p99):.1f} ({spread(p99)}) |\n"
        )
    return head + body


def p50_p99(samples: list[int]) -> tuple[float, float]:
    data = [float(s) for s in samples]
    return us(statistics.median(data)), us(pct(data, 0.99))


def measure_calls(rounds: int, tmp: Path, servers: Servers) -> list[CallRow]:
    """Each round runs the plain agent and then the gated one, with budget on and off."""
    rows = {case: CallRow(name, [], [], [], [], has_plain) for name, case, has_plain in CALLS}
    free = {case: CallRow(NO_BUDGET_NAMES[case], [], [], [], [], True) for case in NO_BUDGET_CASES}
    for n in range(rounds):
        plain = run_plain(servers, tmp / f"plain-{n}.json", PLAIN_CASES)
        gated = run_gated(servers, tmp / f"gated-{n}.json", GATED_CASES, "1000")
        for _, case, has_plain in CALLS:
            rows[case].gated_p50.append(p50_p99(gated[case]["samples"])[0])
            rows[case].gated_p99.append(p50_p99(gated[case]["samples"])[1])
            if has_plain:
                rows[case].plain_p50.append(p50_p99(plain[case]["samples"])[0])
                rows[case].plain_p99.append(p50_p99(plain[case]["samples"])[1])
        cases = ",".join(NO_BUDGET_CASES)
        none = run_gated(servers, tmp / f"nobudget-{n}.json", cases, '"none"')
        for case in NO_BUDGET_CASES:
            free[case].gated_p50.append(p50_p99(none[case]["samples"])[0])
            free[case].gated_p99.append(p50_p99(none[case]["samples"])[1])
            free[case].plain_p50.append(rows[case].plain_p50[-1])
            free[case].plain_p99.append(rows[case].plain_p99[-1])
    return [*rows.values(), *free.values()]


def paired_added_us(servers: Servers, tmp: Path, name: str) -> dict[str, tuple[float, float]]:
    """Per case, the gated and plain batch time in µs, measured in one process.

    Each is the smallest of the batch medians, so a stretch of load on the machine can
    only make a batch look slower: the smallest is the batch the machine left alone.
    """
    out = run_gated(servers, tmp / f"{name}.json", PLAIN_CASES, "1000", paired=True)
    return {case: (us(min(data["gated"])), us(min(data["plain"]))) for case, data in out.items()}


def paired_table(rows: list[dict[str, tuple[float, float]]]) -> str:
    out = "| Call | Added µs, smallest batch medians (spread) |\n|---|---|\n"
    for name, case, has_plain in CALLS:
        if has_plain:
            added = [row[case][0] - row[case][1] for row in rows]
            out += f"| {name} | {med(added):.1f} ({spread(added)}) |\n"
    return out


def timed_spawn(args: Sequence[str], env: dict[str, str], cwd: Path) -> float:
    start = time.perf_counter()
    subprocess.run(
        args, env=env, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return time.perf_counter() - start


def start_control(tmp: Path) -> tuple[ControlServer, dict[str, str]]:
    """A real control server with one open run, and the environment that joins it."""
    token, run = secrets.token_hex(32), "run-benchmark"
    # Its own folder each time, as a budget's check starts one per attempt.
    records = Path(tempfile.mkdtemp(prefix="control-records-", dir=tmp))
    server = ControlServer(token, RecordStore(records))
    server.start()
    plan = Plan(run, "benchmark", "baseline", (), (), (), (), 600.0)
    server.open_run(run, "baseline", plan.to_json())
    env = env_with(
        {
            "SCRIMMAGE_TOKEN": token,
            "SCRIMMAGE_CONTROL_URL": server.url,
            "SCRIMMAGE_RUN": run,
            "SCRIMMAGE_ALLOW_IMPORTS": "benchmark_agent.",
        }
    )
    return server, env


@dataclass
class Startup:
    name: str
    first_line_ms: list[float]  # one value per round
    exit_ms: list[float]


def first_line_ms(args: Sequence[str], env: dict[str, str], cwd: Path) -> float:
    """From just before the process starts to its first line of code."""
    before = time.time_ns()
    done = subprocess.run(
        args, env=env, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    return (int(done.stdout) - before) / 1e6


def boot_env(env: dict[str, str]) -> dict[str, str]:
    from scrimmage_core.run_command import BOOT

    return {**env, "PYTHONPATH": f"{BOOT}{os.pathsep}{HERE}"}


def child_env(env: dict[str, str]) -> dict[str, str]:
    """A child process of a run: it joins by the root's process id and Python."""
    python = f"{sys.prefix}{os.pathsep}{sys.version_info[0]}.{sys.version_info[1]}"
    return {**boot_env(env), "SCRIMMAGE_ROOT_PID": "1", "SCRIMMAGE_PYTHON": python}


def measure_startup(rounds: int, per_round: int, tmp: Path) -> list[Startup]:
    server, env = start_control(tmp)
    plain = [PYTHON, "-c", FIRST_LINE]
    direct = [PYTHON, "-c", FIRST_LINE]
    via_run = [PYTHON, "-m", "scrimmage_core.run_command", PYTHON, "-c", FIRST_LINE]
    forms: list[tuple[str, list[str], dict[str, str]]] = [
        ("plain python", plain, env_with({})),
        ("hooks loaded, root process", direct, boot_env(env)),
        ("hooks loaded, child process", direct, child_env(env)),
        ("scrimmage-run, then the hooks (the real path)", via_run, env),
    ]
    results = [Startup(name, [], []) for name, _, _ in forms]
    for _ in range(rounds):
        for form, (_, args, form_env) in zip(results, forms, strict=True):
            first = [first_line_ms(args, form_env, tmp) for _ in range(per_round)]
            ends = [1000 * timed_spawn(args, form_env, tmp) for _ in range(per_round)]
            form.first_line_ms.append(med(first))
            form.exit_ms.append(med(ends))
    server.stop()
    return results


def startup_table(results: list[Startup]) -> str:
    plain = results[0]
    out = (
        "| Process start | First line of code, ms (spread) "
        "| Added to first line, ms | To process exit, ms (spread) | Added to exit, ms |\n"
        "|---|---|---|---|---|\n"
    )
    for row in results:
        added = [a - p for a, p in zip(row.first_line_ms, plain.first_line_ms, strict=True)]
        added_exit = [a - p for a, p in zip(row.exit_ms, plain.exit_ms, strict=True)]
        out += (
            f"| {row.name} | {med(row.first_line_ms):.1f} ({spread(row.first_line_ms)}) "
            f"| {med(added):.1f} | {med(row.exit_ms):.1f} ({spread(row.exit_ms)}) "
            f"| {med(added_exit):.1f} |\n"
        )
    return out


def cli_timings(rounds: int, per_round: int, tmp: Path) -> dict[str, list[float]]:
    """Wall-clock ms of each command, one value per round."""
    config = HERE / "example" / "scrimmage.toml"
    forms: list[tuple[str, list[str]]] = [
        ("plain python", [PYTHON, "-c", "pass"]),
        ("import scrimmage_core.cli", [PYTHON, "-c", "import scrimmage_core.cli"]),
        ("scrimmage --help", [SCRIMMAGE, "--help"]),
        ("scrimmage check (example)", [SCRIMMAGE, "check", str(config)]),
    ]
    env = env_with({})
    results: dict[str, list[float]] = {name: [] for name, _ in forms}
    for _ in range(rounds):
        for name, args in forms:
            results[name].append(
                med([1000 * timed_spawn(args, env, tmp) for _ in range(per_round)])
            )
    return results


def cli_table(results: dict[str, list[float]]) -> str:
    plain = results["plain python"]
    out = "| Command | Wall ms (spread) | Added to plain python, ms |\n|---|---|---|\n"
    for name, values in results.items():
        added = [v - p for v, p in zip(values, plain, strict=True)]
        out += f"| {name} | {med(values):.1f} ({spread(values)}) | {med(added):.1f} |\n"
    return out


IMPORT_LINE = re.compile(r"import time:\s+(\d+) \|\s+(\d+) \|\s*(\S+)")


def import_costs(target: str) -> list[tuple[str, int, int]]:
    """(module, self µs, cumulative µs) from -X importtime, largest cumulative first."""
    result = subprocess.run(
        [PYTHON, "-X", "importtime", "-c", f"import {target}"],
        env=env_with({}),
        check=True,
        capture_output=True,
        text=True,
    )
    found = [
        (m.group(3), int(m.group(1)), int(m.group(2)))
        for m in map(IMPORT_LINE.match, result.stderr.splitlines())
        if m is not None
    ]
    return sorted(found, key=lambda row: -row[2])


def import_table(target: str, shown: int = 10) -> str:
    rows = [row for row in import_costs(target) if row[0] != target][:shown]
    out = f"Largest imports under {target} (cumulative µs, self µs):\n\n"
    out += "| Module | Cumulative µs | Self µs |\n|---|---|---|\n"
    for module, self_us, cumulative in rows:
        out += f"| {module} | {cumulative} | {self_us} |\n"
    return out


def runner_seconds(tmp: Path, runs: int, label: str) -> float:
    """The wall time of a scrimmage test of the do-nothing agent, runs one at a time."""
    config = noop_project(tmp / f"runner-{runs}-{label}", runs)
    start = time.perf_counter()
    done = subprocess.run(
        [SCRIMMAGE, "test", str(config)], env=env_with({}), cwd=config.parent, capture_output=True
    )
    elapsed = time.perf_counter() - start
    assert done.returncode == 0, (done.stdout + done.stderr).decode()
    return elapsed


def runner_overhead(rounds: int, tmp: Path, high: int = 11) -> dict[str, list[float]]:
    """Per-run wall time, from the slope between 1 run and many, and the plain agent's time."""
    plain_args = [PYTHON, "-c", "import noop_agent; noop_agent.answer('go')"]
    out: dict[str, list[float]] = {"per_run_ms": [], "fixed_ms": [], "plain_agent_ms": []}
    for n in range(rounds):
        one = runner_seconds(tmp, 1, f"a{n}")
        many = runner_seconds(tmp, high, f"b{n}")
        per_run = (many - one) / (high - 1)
        out["per_run_ms"].append(1000 * per_run)
        out["fixed_ms"].append(1000 * (one - per_run))
        plain_dir = tmp / f"runner-{1}-a{n}"
        out["plain_agent_ms"].append(1000 * timed_spawn(plain_args, env_with({}), plain_dir))
    return out


def runner_table(values: dict[str, list[float]]) -> str:
    per = values["per_run_ms"]
    plain = values["plain_agent_ms"]
    added = [p - a for p, a in zip(per, plain, strict=True)]
    return (
        "| Runner, with a do-nothing agent, 1 run at a time | ms (spread) |\n|---|---|\n"
        f"| Whole cost of one run | {med(per):.1f} ({spread(per)}) |\n"
        f"| The same agent under plain python | {med(plain):.1f} ({spread(plain)}) |\n"
        f"| Added per run by the runner and hooks | {med(added):.1f} ({spread(added)}) |\n"
        f"| Fixed cost of one `scrimmage test` | {med(values['fixed_ms']):.1f} "
        f"({spread(values['fixed_ms'])}) |\n"
    )


def machine_line() -> str:
    uname = os.uname()
    return (
        f"{uname.machine}, {os.cpu_count()} CPUs, Linux {uname.release}, "
        f"Python {sys.version.split()[0]}"
    )


@contextlib.contextmanager
def running_servers(tmp: Path) -> Iterator[Servers]:
    """The fake HTTP server, and how the agent starts the MCP server, for as long as needed."""
    fast, port = start_server([PYTHON, str(FAST_SERVER)])
    log = tmp / "mcp-received.log"
    mcp_args = "|".join([str(DOCS_SERVER), "stdio", "docs", str(log), "ok"])
    try:
        yield Servers(f"http://127.0.0.1:{port}", mcp_args)
    finally:
        fast.kill()
        fast.wait()
        assert fast.stdout is not None
        fast.stdout.close()


PROFILED = (("tool_sync", '"none"'), ("http_sync", '"none"'), ("model_sync", "1000"))


def profile_table(servers: Servers, tmp: Path, case: str, budget: str, shown: int = 14) -> str:
    """Where the agent's thread spends a gated call, by function, largest self time first.

    cProfile slows everything, so the shares are the point and not the times. It sees only
    the agent's thread: the sender thread's work is in the p99 rows, not here.
    """
    dump = tmp / f"profile-{case}.pstats"
    run_gated(servers, tmp / f"profile-{case}.json", case, budget, profile=dump)
    stats = cast(dict[Any, Any], pstats.Stats(str(dump)).stats)  # type: ignore[attr-defined]
    calls = benchmark_agent.COUNTS[case] + benchmark_agent.WARMUP
    rows = sorted(stats.items(), key=lambda item: -item[1][2])[:shown]
    out = f"Profile of {case}, self µs per call:\n\n| Function | Where | µs |\n|---|---|---|\n"
    for (file, line, function), (_, _, self_seconds, _, _) in rows:
        where = file.partition("site-packages/")[2] or file.partition("packages/")[2] or file
        out += f"| {function} | {where}:{line} | {1e6 * self_seconds / calls:.1f} |\n"
    return out


def process_added_ms(tmp: Path, count: int = 5) -> dict[str, float]:
    """Each process budget's added ms over plain python, the smallest of alternating runs."""
    config = HERE / "example" / "scrimmage.toml"
    server, env = start_control(tmp)
    via_run = [PYTHON, "-m", "scrimmage_core.run_command", PYTHON, "-c", FIRST_LINE]
    plain_env = env_with({})
    bare_first: list[float] = []
    bare_exit: list[float] = []
    forms: dict[str, list[float]] = {name: [] for name in PROCESS_BUDGETS_MS}
    try:
        for _ in range(count):
            bare_first.append(first_line_ms([PYTHON, "-c", FIRST_LINE], plain_env, tmp))
            forms["startup, first line"].append(first_line_ms(via_run, env, tmp))
            bare_exit.append(1000 * timed_spawn([PYTHON, "-c", "pass"], plain_env, tmp))
            forms["startup, whole process"].append(1000 * timed_spawn(via_run, env, tmp))
            forms["scrimmage check"].append(
                1000 * timed_spawn([SCRIMMAGE, "check", str(config)], plain_env, tmp)
            )
            forms["scrimmage --help"].append(
                1000 * timed_spawn([SCRIMMAGE, "--help"], plain_env, tmp)
            )
            forms["import scrimmage_core.cli"].append(
                1000 * timed_spawn([PYTHON, "-c", "import scrimmage_core.cli"], plain_env, tmp)
            )
    finally:
        server.stop()
    added = {name: min(times) - min(bare_exit) for name, times in forms.items()}
    added["startup, first line"] = min(forms["startup, first line"]) - min(bare_first)
    return added


def runner_added_ms(tmp: Path, label: str, high: int = 6) -> float:
    """The ms one run adds over the same agent under plain python, from the slope."""
    one = min(runner_seconds(tmp, 1, f"{label}-a{i}") for i in range(2))
    many = min(runner_seconds(tmp, high, f"{label}-b{i}") for i in range(2))
    folder = tmp / f"runner-1-{label}-a0"
    args = [PYTHON, "-c", "import noop_agent; noop_agent.answer('go')"]
    plain = min(1000 * timed_spawn(args, env_with({}), folder) for _ in range(3))
    return 1000 * (many - one) / (high - 1) - plain


SECTIONS = ("calls", "paired", "startup", "cli", "imports", "runner", "profile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5, help="repeats of each measure")
    parser.add_argument("--section", action="append", choices=SECTIONS, help="only these")
    parser.add_argument("--json", type=Path, help="also write the raw numbers to this file")
    args = parser.parse_args(argv)
    sections = args.section or SECTIONS
    print(f"Machine: {machine_line()}; rounds: {args.rounds}\n")
    raw: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="scrimmage-bench-") as name:
        tmp = Path(name)
        if "calls" in sections:
            with running_servers(tmp) as servers:
                rows = measure_calls(args.rounds, tmp, servers)
            print("Time added per gated call:\n")
            print(call_table(rows))
            raw["calls"] = [r.__dict__ for r in rows]
        if "paired" in sections:
            with running_servers(tmp) as servers:
                rows_paired = [
                    paired_added_us(servers, tmp, f"paired-{n}") for n in range(args.rounds)
                ]
            print("Added per call, run on and off in one process (the budget test's measure):\n")
            print(paired_table(rows_paired))
            raw["paired"] = rows_paired
        if "startup" in sections:
            results = measure_startup(args.rounds, 5, tmp)
            print("Agent startup:\n")
            print(startup_table(results))
            raw["startup"] = [r.__dict__ for r in results]
        if "cli" in sections:
            cli = cli_timings(args.rounds, 5, tmp)
            print("CLI:\n")
            print(cli_table(cli))
            raw["cli"] = cli
        if "imports" in sections:
            print(import_table("scrimmage_core.cli"))
            print(import_table("scrimmage_core.bootstrap"))
        if "profile" in sections:
            with running_servers(tmp) as servers:
                for case, budget in PROFILED:
                    print(profile_table(servers, tmp, case, budget))
        if "runner" in sections:
            runner = runner_overhead(args.rounds, tmp)
            print("Runner overhead per run:\n")
            print(runner_table(runner))
            raw["runner"] = runner
    if args.json is not None:
        args.json.write_text(json.dumps(raw, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
