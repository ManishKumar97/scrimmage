"""Every example page, run on a real model, once per release.

`tests/test_docs_examples.py` runs each page against a scripted fake model, so a page's
answer rules never meet the words a real model uses. This check runs each page's documented
commands against the real provider and asserts the outcome the page shows: the exit code of
each command, and the pass or fail of each row. Costs and wording differ from the page.

It is left out unless SCRIMMAGE_REAL=1, and it needs the provider key in the environment.
It prints the spend of each page, about $0.02 for all of them. See tests/README.md.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ports import free_port
from test_docs_examples import (
    BUDGET,
    DOCS,
    EXAMPLES,
    FENCE,
    ORDERS_PORT,
    OUTPUT,
    RECORDED,
    RECORDING,
    SERVICE,
    SERVICE_RUN,
    Block,
    Example,
    OrdersService,
    apply,
    blocks,
    retarget,
)

PAGES = {**EXAMPLES, SERVICE: SERVICE_RUN}
RESULT = re.compile(r"^(?!full name).*\s(pass|fail)\s*$", re.M)
COST = re.compile(r"model calls cost \$([0-9.]+)")
CAP = 0.05  # dollars for one page; a page that spends more has a loop or a wrong model
CAP_FLAG = ["--max-cost-usd", str(CAP)]
spent: dict[str, float] = {}


def results(text: str) -> list[str]:
    return RESULT.findall(text)


def answers(output: str) -> str:
    """What each run answered, so a rule that fails can be judged against the real words."""
    found = re.search(r"records in (\S+)", output)
    if not found or not (Path(found.group(1)) / "result.json").exists():
        return ""
    rows = json.loads((Path(found.group(1)) / "result.json").read_text())["rows"]
    return "\n".join(
        f"  {row['row']} {detail['judged']}: {detail['answer_preview']}"
        for row in rows
        for detail in row["run_details"]
    )


def child_env() -> dict[str, str]:
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    return {**os.environ, "PATH": path}


def run_page(name: str, example: Example, root: Path) -> float:
    if example.start:
        shutil.copytree(example.start, root, ignore=shutil.ignore_patterns("__pycache__"))
        toml = root / "scrimmage.toml"
        # The fake model's host is for the repository's tests; a real key needs no such line.
        toml.write_text(re.sub(r"^model_hosts[^\n]*\n", "", toml.read_text(), flags=re.M))
    root.mkdir(exist_ok=True)
    for block in blocks(example.base) if example.base else []:
        if block.title != OUTPUT:
            apply(root, block)
    command: list[str] = []
    codes: list[int] = []
    total = 0.0
    for lang, title, body in FENCE.findall((DOCS / name).read_text()):
        if lang == "bash":
            shown = [line for line in body.splitlines() if line.startswith("scrimmage ")]
            command = shown[-1].split()[1:] if shown else command
            continue
        if not title:
            continue
        block = Block(lang, title, body)
        if block.title != OUTPUT:
            apply(root, block)
            continue
        run = subprocess.run(
            # The runner itself refuses spend over the cap, so a loop cannot run up a bill.
            [
                sys.executable,
                "-m",
                "scrimmage",
                *command,
                *(CAP_FLAG if command[:1] == ["test"] else []),
            ],
            cwd=root,
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=600,
        )
        shown = f"scrimmage {' '.join(command)} -> exit {run.returncode}"
        print(f"\n--- {name}: {shown}\n{run.stdout}{answers(run.stdout)}")
        assert run.returncode == example.codes[len(codes)], run.stdout + run.stderr
        assert results(run.stdout) == results(body), f"{name}: rows differ from the page"
        codes.append(run.returncode)
        total += sum(float(c) for c in COST.findall(run.stdout))
    assert len(codes) == len(example.codes)
    return total


@pytest.fixture(scope="module", autouse=True)
def total_spend() -> Iterator[None]:
    yield
    print(f"\nREAL SPEND TOTAL: ${sum(spent.values()):.4f} over {len(spent)} pages")


@pytest.mark.real
@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(PAGES))
def test_the_page_passes_on_a_real_model(name: str, tmp_path: Path) -> None:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("run the real check with -n 0: parallel pages would share one spend cap")
    key = f"{PAGES[name].provider.upper()}_API_KEY"
    if not os.environ.get(key):
        pytest.fail(f"{key} is not set; the real check needs the provider key")
    cost = run_page(name, PAGES[name], tmp_path / "project")
    spent[name] = cost
    print(f"\nREAL SPEND {name}: ${cost:.4f}")
    assert cost < CAP, f"{name} spent ${cost:.4f}, over the ${CAP} cap for a page"


@pytest.mark.real
@pytest.mark.slow
def test_the_recording_page_passes_on_a_real_model(tmp_path: Path) -> None:
    """Record with a real model asking a stand-in orders service, then replay without it."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("run the real check with -n 0: parallel pages would share one spend cap")
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.fail("OPENAI_API_KEY is not set; the real check needs the provider key")
    root = tmp_path / "project"
    root.mkdir()
    for block in blocks("examples/function-agent.md"):
        if block.title != OUTPUT:
            apply(root, block)
    port = free_port()
    service = OrdersService(port)
    command: list[str] = []
    total = 0.0
    stdout = ""
    try:
        for lang, title, body in FENCE.findall((DOCS / RECORDING).read_text()):
            if lang == "bash":
                command = body.strip().split()[1:]
                continue
            if not title or title == RECORDED:
                continue
            if title != OUTPUT:
                apply(root, Block(lang, title, body))
                continue
            if command[0] == "test":  # the replay: the service is gone
                service.stop()
            retarget(root, ORDERS_PORT, port)
            run = subprocess.run(
                [sys.executable, "-m", "scrimmage", *command, *CAP_FLAG],
                cwd=root,
                env=child_env(),
                capture_output=True,
                text=True,
                timeout=600,
            )
            stdout = run.stdout
            print(f"\n--- {RECORDING}: scrimmage {' '.join(command)} -> exit {run.returncode}")
            print(run.stdout + answers(run.stdout))
            assert run.returncode == 0, run.stdout + run.stderr
            total += sum(float(c) for c in COST.findall(run.stdout))
        assert results(stdout) == ["pass"]  # the replay's one row
        assert set(service.asked) == {"/orders/1234"}  # the recording asked, the replay did not
    finally:
        service.stop()
    spent[RECORDING] = total
    print(f"\nREAL SPEND {RECORDING}: ${total:.4f}")
    assert total < CAP, f"{RECORDING} spent ${total:.4f}, over the ${CAP} cap for a page"


BUDGET_LINE = re.compile(r"model calls cost \$([0-9.]+) of a budget of \$([0-9.]+)")


@pytest.mark.real
@pytest.mark.slow
def test_the_budget_page_stops_at_its_budget_on_a_real_model(tmp_path: Path) -> None:
    """A real model may answer after the first failure and never loop, so this asserts only that
    the test stops at its budget, or passes it by what its last calls cost above their holds and
    says so, and that a model with no price is refused before any call."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("run the real check with -n 0: parallel pages would share one spend cap")
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.fail("OPENAI_API_KEY is not set; the real check needs the provider key")
    root = tmp_path / "project"
    root.mkdir()
    for block in blocks("examples/function-agent.md"):
        if block.title != OUTPUT:
            apply(root, block)
    command: list[str] = []
    runs: list[subprocess.CompletedProcess[str]] = []
    for lang, title, body in FENCE.findall((DOCS / BUDGET).read_text()):
        if lang == "bash":
            shown = [line for line in body.splitlines() if line.startswith("scrimmage ")]
            command = shown[-1].split()[1:]
            continue
        if not title:
            continue
        if title != OUTPUT:
            apply(root, Block(lang, title, body))
            continue
        run = subprocess.run(
            [sys.executable, "-m", "scrimmage", *command, *CAP_FLAG],
            cwd=root,
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=600,
        )
        print(f"\n--- {BUDGET}: scrimmage {' '.join(command)} -> exit {run.returncode}")
        print(run.stdout + answers(run.stdout))
        runs.append(run)
    looping, unpriced = runs
    spent_usd, budget = (float(x) for x in BUDGET_LINE.findall(looping.stdout)[0])
    # The page's budget is the $0.05 page cap, and --max-cost-usd 0.05 leaves it as it is.
    assert budget == CAP
    assert spent_usd <= budget or "warning: the test spent more than its budget" in looping.stdout
    assert looping.returncode in (0, 1, 2), looping.stdout + looping.stderr
    assert unpriced.returncode == 2
    assert "has no price for the model 'acme-llm'" in unpriced.stdout
    assert BUDGET_LINE.findall(unpriced.stdout)[0][0] == "0.0000"  # no call was sent
    total = spent_usd
    spent[BUDGET] = total
    print(f"\nREAL SPEND {BUDGET}: ${total:.4f}")
    assert total < CAP + 0.02, f"{BUDGET} spent ${total:.4f}, far over the ${CAP} cap for a page"
