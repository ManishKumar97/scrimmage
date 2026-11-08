"""The written budgets in tests/benchmark.py, checked on the real code.

Time itself is the claim, so each comparison is built not to depend on the machine's load:
gated and plain alternate batch by batch in one process, and each side is its smallest
batch median, which a stretch of load cannot lower. The ceilings are several times what
the code costs now, so a slow machine does not reach them, and each attempt is
remeasured up to three times, so a machine that stays busy for the whole of one attempt
still passes on the next. A real regression, which makes every attempt slow, fails.
"""

from collections.abc import Callable
from pathlib import Path

import coverage
import pytest

import benchmark

pytestmark = pytest.mark.slow
ATTEMPTS = 3
# A run measured by coverage is a different program: it traces each line the gates run.
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
            paired = benchmark.paired_added_us(servers, tmp_path, name)
            return {case: gated - plain for case, (gated, plain) in paired.items()}

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
