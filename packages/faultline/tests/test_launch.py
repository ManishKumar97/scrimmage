"""`faultline run` as a user runs it: the installed command, with a small agent as the child."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from upstream import upstream_server

from faultline.runs import read_runs_many

FAULTLINE = str(Path(sys.executable).parent / "faultline")
PYTHON = sys.executable

# Prints what the agent sees, then makes one model call to the base URL it was given.
SEES = """
import json, os, urllib.request
names = ["OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "GROQ_BASE_URL", "FAULTLINE_TRACE_DIR",
         "FAULTLINE_TRACE_ID", "FAULTLINE_ROOT_SPAN_ID", "FAULTLINE_FAULTS"]
print(json.dumps({name: os.environ.get(name) for name in names}))
request = urllib.request.Request(os.environ["GROQ_BASE_URL"] + "/chat/completions", b"{}")
print(urllib.request.urlopen(request).read().decode())
"""

# Says when its handler is in place, then waits. On SIGTERM it makes one last model call,
# so the proxy must still be up, and exits with 7.
WAITS = """
import os, signal, sys, time, urllib.request
def stop(number, frame):
    request = urllib.request.Request(os.environ["OPENAI_BASE_URL"] + "/last", b"{}")
    urllib.request.urlopen(request).read()
    os.write(1, f"got {number}\\n".encode())
    sys.exit(7)
for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(number, stop)
os.write(1, b"ready\\n")  # not print: a signal during its flush would re-enter it
time.sleep(30)
"""


def faultline(*args: str, env: dict[str, str] | None = None, **kwargs):
    return subprocess.run(
        [FAULTLINE, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
        **kwargs,
    )


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_BASE_URL")}
    return {**env, **extra}


def test_the_agent_gets_the_proxy_url_and_its_calls_reach_the_upstream(tmp_path):
    with upstream_server() as upstream:
        result = faultline(
            "run",
            "--base-url-env",
            "GROQ_BASE_URL",
            "--trace",
            str(tmp_path),
            "--fault",
            "tool:get_logs:empty",
            "--",
            PYTHON,
            "-c",
            SEES,
            env=clean_env(GROQ_BASE_URL=upstream.url + "/openai/v1"),
        )

    assert result.returncode == 0, result.stderr
    seen, reply = result.stdout.splitlines()
    sees = json.loads(seen)
    assert sees["GROQ_BASE_URL"].startswith("http://127.0.0.1:")
    assert sees["GROQ_BASE_URL"].endswith("/groq")
    # No upstream is known for these, so they are left as they were: unset.
    assert sees["OPENAI_BASE_URL"] is None
    assert sees["ANTHROPIC_BASE_URL"] is None
    assert "faultline: OPENAI_BASE_URL not set, its calls do not go through" in (
        result.stderr
    )
    assert sees["FAULTLINE_TRACE_DIR"] == str(tmp_path.resolve())
    assert len(sees["FAULTLINE_TRACE_ID"]) == 32
    assert len(sees["FAULTLINE_ROOT_SPAN_ID"]) == 16
    assert json.loads(sees["FAULTLINE_FAULTS"]) == ["tool:get_logs:empty"]
    assert reply == '{"ok": true}'
    assert upstream.seen[0][1] == "/openai/v1/chat/completions"


def test_upstream_flag_wins_over_the_environment():
    with upstream_server() as upstream:
        result = faultline(
            "run",
            "--upstream",
            upstream.url,
            "--base-url-env",
            "GROQ_BASE_URL",
            "--",
            PYTHON,
            "-c",
            SEES,
            env=clean_env(GROQ_BASE_URL="http://127.0.0.1:9"),
        )

    assert result.returncode == 0, result.stderr
    assert upstream.seen[0][1] == "/chat/completions"


def test_the_agents_exit_code_is_faultlines():
    result = faultline(
        "run",
        "--upstream",
        "http://127.0.0.1:9",
        "--",
        PYTHON,
        "-c",
        "raise SystemExit(3)",
    )

    assert result.returncode == 3


def test_no_upstream_is_an_error_that_says_what_to_pass():
    result = faultline("run", "--", PYTHON, "-c", "print('ran')", env=clean_env())

    assert result.returncode == 2
    assert "ran" not in result.stdout
    assert "pass --upstream URL" in result.stderr
    assert "OPENAI_BASE_URL, ANTHROPIC_BASE_URL" in result.stderr


def test_a_command_that_does_not_exist_exits_127():
    result = faultline("run", "--upstream", "http://x", "--", "no-such-agent-cmd")

    assert result.returncode == 127
    assert "cannot start no-such-agent-cmd" in result.stderr


def test_no_command_is_an_error():
    result = faultline("run", "--upstream", "http://x")

    assert result.returncode == 2
    assert "after --" in result.stderr


def signalled(number: signal.Signals) -> tuple[int, str]:
    with (
        upstream_server() as upstream,
        subprocess.Popen(
            [FAULTLINE, "run", "--upstream", upstream.url, "--", PYTHON, "-c", WAITS],
            stdin=subprocess.DEVNULL,  # no terminal, as under a CI runner or a script
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as process,
    ):
        assert process.stdout
        assert process.stdout.readline() == "ready\n"
        process.send_signal(number)
        rest = process.stdout.read()
        code = process.wait(30)
        assert (
            upstream.seen[0][1] == "/last"
        )  # the proxy outlived the agent's last call
    return code, rest


def test_sigterm_reaches_the_agent_and_faultline_exits_with_its_code():
    code, said = signalled(signal.SIGTERM)

    assert said == f"got {signal.SIGTERM.value}\n"
    assert code == 7


def test_sigint_sent_to_faultline_alone_reaches_the_agent():
    code, said = signalled(signal.SIGINT)

    assert said == f"got {signal.SIGINT.value}\n"
    assert code == 7


def test_an_agent_killed_by_a_signal_gives_128_plus_the_signal():
    result = faultline(
        "run",
        "--upstream",
        "http://x",
        "--",
        PYTHON,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
    )

    assert result.returncode == 128 + signal.SIGKILL.value


def test_upstream_flags_name_their_variable(tmp_path):
    with upstream_server() as groq, upstream_server() as anthropic:
        result = faultline(
            "run",
            "--upstream",
            f"GROQ_BASE_URL={groq.url}/openai/v1",
            "--upstream",
            f"ANTHROPIC_BASE_URL={anthropic.url}",
            "--",
            PYTHON,
            "-c",
            SEES,
            env=clean_env(),
        )

    assert result.returncode == 0, result.stderr
    sees = json.loads(result.stdout.splitlines()[0])
    assert sees["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert sees["OPENAI_BASE_URL"] is None
    assert [s[1] for s in groq.seen] == ["/openai/v1/chat/completions"]
    assert anthropic.seen == []


def test_sighup_and_sigquit_reach_the_agent_too():
    for number in (signal.SIGHUP, signal.SIGQUIT):
        code, said = signalled(number)

        assert said == f"got {number.value}\n"
        assert code == 7


def gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux only")
def test_the_agent_dies_with_faultline_even_on_sigkill():
    agent = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
    with subprocess.Popen(
        [FAULTLINE, "run", "--upstream", "http://127.0.0.1:9", "--"]
        + [PYTHON, "-c", agent],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as process:
        assert process.stdout
        pid = int(process.stdout.readline())
        process.kill()  # SIGKILL: faultline gets no chance to pass anything on
        process.wait(10)

    died = gone(pid, 5)
    if not died:
        os.kill(pid, signal.SIGKILL)  # leave no orphan behind a failed test
    assert died


# One model call, then a wait. It exits on SIGHUP as a shell's child does on hangup.
CALLS_THEN_WAITS = """
import os, signal, sys, time, urllib.request
signal.signal(signal.SIGHUP, lambda number, frame: sys.exit(5))
request = urllib.request.Request(os.environ["OPENAI_BASE_URL"] + "/chat/completions", b"{}")
urllib.request.urlopen(request).read()
os.write(1, b"ready\\n")
time.sleep(30)
"""


def test_a_hung_up_run_still_leaves_a_whole_trace(tmp_path):
    trace = tmp_path / "trace"
    with (
        upstream_server() as upstream,
        subprocess.Popen(
            [FAULTLINE, "run", "--upstream", upstream.url, "--trace", str(trace)]
            + ["--", PYTHON, "-c", CALLS_THEN_WAITS],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as process,
    ):
        assert process.stdout
        assert process.stdout.readline() == "ready\n"
        process.send_signal(signal.SIGHUP)
        code = process.wait(30)

    assert code == 5
    [run] = read_runs_many(sorted(trace.glob("*.jsonl"))).runs
    assert run.complete  # the root was written
    assert len(run.steps) == 1  # and the step still open when the signal came
    assert run.error_type == "exit 5"
