"""The written budgets in tests/benchmark.py, checked on the real code.

Time itself is the claim of the budget checks. Each attempt times the same calls in a plain
process, which has no gates, and under a real run, and each side is its smallest batch
median. An attempt that is over is taken again, up to three times, so a machine that
stays busy for one attempt passes on the next; a real regression slows every attempt.
Load can also lower a difference, when it slows the plain side, and so hide a regression.
It does not cause a flake.

The time ceilings are wide: only the tool ceiling catches one added round trip to the
runner. HTTP needs about 7.6 ms added and a model call about 18 ms to fail. So a count of
requests to the runner covers those, and a list of heavy imports covers the CLI: neither
uses a clock.
"""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import coverage
import pytest

import benchmark

pytestmark = pytest.mark.slow
ATTEMPTS = 3
# A run measured by coverage is a different program: it traces each line the gates run.
# On 3.12 to 3.14 the ceilings are about 10x looser for that, so the 3.11 job enforces them.
COVERAGE_MARGIN = 10.0


def margin() -> float:
    return COVERAGE_MARGIN if coverage.Coverage.current() is not None else 1.0


def still_over(
    measure: Callable[[], dict[str, float]], budgets: dict[str, float], unit: str
) -> None:
    """Remeasure until every budget is met, or fail with what stayed over."""
    attempts: list[dict[str, str]] = []
    for _ in range(ATTEMPTS):
        found = measure()
        over = {
            name: f"{found[name]:.1f} {unit} over a budget of {limit * margin():.0f} {unit}"
            for name, limit in budgets.items()
            if found[name] > limit * margin()
        }
        if not over:
            print(f"within budget after {len(attempts) + 1} attempt(s): {found}")
            return
        attempts.append(over)
    pytest.fail(f"over budget in all {ATTEMPTS} attempts: {attempts}")


def test_each_gated_call_stays_under_its_budget(tmp_path: Path) -> None:
    attempt = iter(range(ATTEMPTS))
    with benchmark.running_servers(tmp_path) as servers:

        def measure() -> dict[str, float]:
            name = f"attempt-{next(attempt)}"
            batched = benchmark.batched_added_us(servers, tmp_path, name)
            return {case: gated - plain for case, (gated, plain) in batched.items()}

        still_over(measure, benchmark.CALL_BUDGETS_US, "µs")


def test_startup_and_the_cli_stay_under_their_budgets(tmp_path: Path) -> None:
    def measure() -> dict[str, float]:
        return benchmark.process_added_ms(tmp_path)

    still_over(measure, benchmark.PROCESS_BUDGETS_MS, "ms")


def test_a_run_stays_under_its_budget(tmp_path: Path) -> None:
    attempt = iter(range(ATTEMPTS))

    def measure() -> dict[str, float]:
        return {"run": benchmark.runner_added_ms(tmp_path, f"attempt-{next(attempt)}")}

    still_over(measure, benchmark.RUN_BUDGET_MS, "ms")


def test_a_gated_call_waits_on_the_expected_requests_to_the_runner(tmp_path: Path) -> None:
    """A count, not a time. One budgeted model call reserves once, and nothing else asks."""
    with benchmark.running_servers(tmp_path) as servers:
        with_budget = benchmark.round_trips(
            servers, tmp_path, "budget-on", benchmark.GATED_CASES, "1000"
        )
        without = benchmark.round_trips(
            servers, tmp_path, "budget-off", ",".join(benchmark.NO_BUDGET_CASES), '"none"'
        )
    assert with_budget == benchmark.ROUND_TRIPS
    assert without == benchmark.ROUND_TRIPS_BUDGET_OFF


def test_importing_the_cli_loads_no_heavy_library() -> None:
    """A library this large would pass the CLI's time ceiling. In a new process, as this
    one has already imported them."""
    names = ", ".join(repr(name) for name in benchmark.NOT_IMPORTED_BY_CLI)
    code = (
        "import sys, scrimmage_core.cli\n"
        f"print(','.join(name for name in ({names}) if name in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == ""
