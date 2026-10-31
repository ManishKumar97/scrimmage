"""`faultline test` as a team runs it: a real SDK agent, several tasks, clean and faulted.

The model is the lab's scripted fake_llm, so no run calls a real model API.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from ida_harness import fake_llm

FAULTLINE = str(Path(sys.executable).parent / "faultline")
AGENT = [sys.executable, str(Path(__file__).parent / "task_agent.py")]
ANSWER = "The cart service is down.\nROOT CAUSE SERVICE: cart"
TASKS = """
[[task]]
id = "cart-fails"
input = "Customers report the cart fails. Why?"
expected = "cart"

[[task]]
id = "checkout-slow"
input = "Checkout is slow. Why?"
"""


def faultline_test(tmp_path: Path, upstream: str, *flags: str):
    tasks = tmp_path / "tasks.toml"
    tasks.write_text(TASKS)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
    env["OPENAI_API_KEY"] = "fake"
    return subprocess.run(
        [FAULTLINE, "test", "--tasks", str(tasks), "--out", str(tmp_path / "out")]
        + ["--upstream", upstream, *flags, "--", *AGENT],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_each_task_runs_clean_and_under_the_fault_and_every_run_is_saved(tmp_path):
    with fake_llm(tmp_path, [{"text": ANSWER}]) as upstream:
        result = faultline_test(
            tmp_path,
            upstream,
            "--fault",
            "model:server_error",
            "--runs",
            "2",
            "--clean-runs",
            "2",
        )

    assert result.returncode == 0, result.stderr
    out = tmp_path / "out"
    saved = sorted(str(p.relative_to(out)) for p in out.glob("*/*/*"))
    assert saved == [
        "cart-fails/clean/1",
        "cart-fails/clean/2",
        "cart-fails/model_server_error/1",
        "cart-fails/model_server_error/2",
        "checkout-slow/clean/1",
        "checkout-slow/clean/2",
        "checkout-slow/model_server_error/1",
        "checkout-slow/model_server_error/2",
    ]
    for run in out.glob("*/*/*"):
        assert sorted(p.name for p in run.iterdir()) == [
            "result.json",
            "stderr.txt",
            "stdout.txt",
            "trace",
        ]
        assert (run / "trace" / "model.jsonl").is_file()
    clean = json.loads((out / "cart-fails/clean/1/result.json").read_text())
    assert (clean["exit_code"], clean["answer"], clean["verdict"]) == (
        0,
        ANSWER,
        "passed",
    )
    assert clean["fault"] is None and clean["seconds"] > 0
    # The task's input reached the agent as its last argument.
    assert (out / "cart-fails/clean/1/stdout.txt").read_text() == ANSWER + "\n"
    faulted = json.loads(
        (out / "checkout-slow/model_server_error/2/result.json").read_text()
    )
    assert (faulted["exit_code"], faulted["answer"], faulted["verdict"]) == (
        1,
        None,
        "failed_loudly",
    )
    assert faulted["fault"] == "model:server_error"
    stderr = (out / "checkout-slow/model_server_error/2/stderr.txt").read_text()
    assert "agent failed: InternalServerError" in stderr
    assert "[8/8] checkout-slow model:server_error #2: failed_loudly" in result.stderr
