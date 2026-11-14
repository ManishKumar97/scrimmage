"""The runner as a Job when the agent cannot reach its control server: each way it fails in a
cluster ends the test with exit 3 and a message that names the fix.

Four faults stop the test at its probe, before any run: a NetworkPolicy that drops the
agent's traffic to the runner, a Service `scrimmage` with no endpoints, which those policies
drop too, the same Service with no policy at all, which refuses, and no Service of that name.
The fifth comes mid-test: a policy applied while a run is inside its model call turns that
run, and every later one, into an infrastructure error, never into a result.

The positive control for all five is test_service_k8s.py's passing Job: the same manifests
without the fault. Like the other cluster tests, these run only with SCRIMMAGE_KIND=1:

    SCRIMMAGE_KIND=1 SCRIMMAGE_LANE=3 test-slot uv run pytest -n 0 -m kind \
        tests/test_k8s_failures.py
"""

import json
import re
import time
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from k8s import cluster as kubernetes
from k8s import images
from k8s.cluster import Cluster, Tags, port_forward
from k8s.fake_model import serve
from k8s.workloads import (
    AGENT_PODS,
    RUNS,
    pod_names,
    start_job,
    token_of,
    wait_agents,
    wait_default_account,
    wait_ended,
)

pytestmark = [pytest.mark.slow, pytest.mark.kind]

URL = "https://scrimmage:7700"
# The fixed parts of each message: the error's own text after "asking:" differs between C
# libraries and resolvers, so it is never asserted.
ASKED = re.compile(
    rf"scrimmage: the service cannot reach the control server at {re.escape(URL)} "
    r"after (\d+) s of asking: "
)
DROPS = (
    "; nothing answered, as when a NetworkPolicy or a firewall drops the traffic, or when the "
    "control Service has no ready endpoints and an egress policy allows only named pods; "
    "allow the service's pods to reach this runner on port 7700, and check that the Service's "
    "selector matches this runner's pod"
)
REFUSES = (
    "; nothing listens there, as when a Kubernetes Service has no ready endpoints; check that "
    "the Service's selector matches this runner's pod"
)
NO_NAME = (
    "; the name scrimmage does not resolve where the service runs; check the host in "
    "SCRIMMAGE_CONTROL_URL and that the Service is in the service's namespace"
)
WAITING = f"scrimmage: waiting for the service to reach the control server at {URL} "
# One connection from an agent replica to the control Service, as the SDK makes one: it prints
# "connected" or the failure's class name.
CONNECT = """
import socket
try:
    socket.create_connection(("scrimmage", 7700), timeout=2).close()
    print("connected")
except OSError as exc:
    print(type(exc).__name__)
"""
# The agent-to-runner policy with its rule taken away: with default-deny, no agent pod may then
# open a connection to the runner. A server-side apply by the manager that made it removes the
# field it leaves out.
BLOCKING = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "agent-to-runner-egress"},
    "spec": {
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "acme-agent"}},
        "policyTypes": ["Egress"],
    },
}
HELD_QUESTION = f"Where is order 1234? {serve.HOLD_MARKER}"
TEST = """
[[test]]
name     = "{name}"
question = "{question}"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"
returns = {{sku = "SKU-42", count = 12}}
"""


@dataclass(frozen=True)
class Stopped:
    """A Job that stopped at its probe: what its pod said and what the test left behind."""

    exit_code: int
    message: str  # the runner's last line
    waiting: list[str]  # the runner's lines that say it asks again
    leaked: bool  # whether the token is in the log; the token itself is never kept here
    model_requests: int
    written: list[str]  # every record file on the volume
    log: Path = field(repr=False)


@pytest.fixture(scope="module")
def tags() -> Tags:
    return Tags(
        runner=images.ensure_runner_image(),
        agent=images.ensure_agent_image(),
        fake_model=images.ensure_fake_model_image(),
    )


@pytest.fixture(scope="module")
def cluster(tags: Tags) -> Iterator[Cluster]:
    with kubernetes.session((tags.runner, tags.agent, tags.fake_model)) as made:
        yield made


def hold_state(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/hold", timeout=10) as reply:
        return json.loads(reply.read())


def release(port: int) -> int:
    """Let the held model call go. Returns how many calls were still held."""
    asked = urllib.request.Request(f"http://127.0.0.1:{port}/hold/release", method="POST")
    with urllib.request.urlopen(asked, timeout=10) as reply:
        return int(json.loads(reply.read())["released"])


def record_files(cluster: Cluster, namespace: str) -> list[str]:
    """Every run's record and every result.json on the volume, through the reader pod."""
    found = cluster.reader_exec(
        namespace, "sh", "-c", f"find {RUNS} -name '*.jsonl' -o -name result.json 2>/dev/null"
    )
    return sorted(found.stdout.split())


def connect_from_agents(cluster: Cluster, namespace: str) -> set[str]:
    """What one connection from each agent replica found: {"connected"} when all got through."""
    agents = pod_names(cluster, namespace, AGENT_PODS)

    def connect(agent: str) -> str:
        done = cluster.kubectl("exec", agent, "-n", namespace, "--", "python3", "-c", CONNECT)
        return str(done.stdout.strip())

    # One kubectl exec takes about 2.5 s, so the replicas go at the same time.
    with ThreadPoolExecutor(max_workers=len(agents)) as pool:
        return set(pool.map(connect, agents))


def block_agent_to_runner(cluster: Cluster, namespace: str) -> None:
    cluster.kubectl(
        "apply", "--server-side", "-n", namespace, "-f", "-", input=json.dumps(BLOCKING)
    )
    policy = cluster.json("get", "networkpolicy", "agent-to-runner-egress", "-n", namespace)
    assert "egress" not in policy["spec"], policy["spec"]


def stopped_at_probe(
    cluster: Cluster, tags: Tags, tmp_path: Path, fault: Callable[[str], None]
) -> Stopped:
    """The committed manifests, the fault, then the Job, in a namespace of its own."""
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        cluster.deploy(namespace, tags)
        wait_agents(cluster, namespace)
        fault(namespace)
        token = token_of(cluster, namespace)
        with port_forward(cluster, namespace, "deployment/fake-model", serve.HOLD_PORT) as port:
            pod = start_job(cluster, namespace, tags)
            code = wait_ended(cluster, namespace, pod, timeout=300)
            model_requests = hold_state(port)["requests"]
        said = cluster.kubectl("logs", pod, "-n", namespace, check=False).stdout
        log = tmp_path / f"{pod}.log"
        log.write_text(said)
        leaked = token in said  # outside any assert, so a failure never prints it
        lines = said.strip().splitlines()
        cluster.start_reader(namespace, tags)
        return Stopped(
            exit_code=code,
            message=lines[-1] if lines else "",
            waiting=[line for line in lines if line.startswith(WAITING)],
            leaked=leaked,
            model_requests=model_requests,
            written=record_files(cluster, namespace),
            log=log,
        )


def check_stopped(found: Stopped, hint: str, not_yet: str) -> None:
    """Exit 3 after the whole wait, the message's prefix and hint, and nothing sent."""
    assert not found.leaked, f"the token is in the runner's log, {found.log}"
    assert found.exit_code == 3, f"exit {found.exit_code}; the runner's log is {found.log}"
    asked = ASKED.match(found.message)
    assert asked is not None, found.message
    # The probe asked again for the whole wait, READY_S, 30 s, before it gave up.
    assert int(asked.group(1)) >= 25, found.message
    assert found.message.endswith(hint), found.message
    assert found.waiting == [f"{WAITING}({not_yet})"], found.waiting
    # runs sent 0: no setup request and no run reached the agent's model, and none was recorded.
    # The record files catch a setup request sent too early: it gets a 503 before any model call.
    assert found.model_requests == 0
    assert found.written == [], found.written


def test_a_policy_that_blocks_the_agent_from_the_runner_gives_an_infrastructure_error(
    cluster: Cluster, tags: Tags, tmp_path: Path
) -> None:
    found = stopped_at_probe(
        cluster, tags, tmp_path, lambda namespace: block_agent_to_runner(cluster, namespace)
    )
    check_stopped(found, DROPS, "nothing answers yet")


def test_a_control_service_with_no_endpoints_gives_an_infrastructure_error(
    cluster: Cluster, tags: Tags, tmp_path: Path
) -> None:
    def no_endpoints(namespace: str) -> None:
        cluster.kubectl(
            "patch", "service", "scrimmage", "-n", namespace, "--type=merge",
            "-p", '{"spec": {"selector": {"app.kubernetes.io/name": "no-such-pod"}}}',
        )  # fmt: skip

    found = stopped_at_probe(cluster, tags, tmp_path, no_endpoints)
    # With no endpoint, kube-proxy never translates the Service's address to a pod's, so the
    # agent's egress policies, which allow only named pods, drop the packet before kube-proxy
    # can refuse it: a timeout, not a refusal. Without them the same Service refuses: see the
    # next test.
    check_stopped(found, DROPS, "nothing answers yet")


def test_a_control_service_with_no_endpoints_and_no_egress_policy_is_refused(
    cluster: Cluster, tags: Tags, tmp_path: Path
) -> None:
    def no_endpoints_no_policy(namespace: str) -> None:
        cluster.kubectl("delete", "networkpolicy", "--all", "-n", namespace, "--wait=true")
        left = cluster.json("get", "networkpolicy", "-n", namespace)["items"]
        assert left == [], left
        cluster.kubectl(
            "patch", "service", "scrimmage", "-n", namespace, "--type=merge",
            "-p", '{"spec": {"selector": {"app.kubernetes.io/name": "no-such-pod"}}}',
        )  # fmt: skip

    found = stopped_at_probe(cluster, tags, tmp_path, no_endpoints_no_policy)
    # Nothing drops the packet, so kube-proxy refuses the Service's address itself.
    check_stopped(found, REFUSES, "nothing listens yet")


def test_a_control_name_that_does_not_exist_gives_an_infrastructure_error(
    cluster: Cluster, tags: Tags, tmp_path: Path
) -> None:
    def no_service(namespace: str) -> None:
        cluster.kubectl("delete", "service", "scrimmage", "-n", namespace, "--wait=true")

    found = stopped_at_probe(cluster, tags, tmp_path, no_service)
    check_stopped(found, NO_NAME, "the name does not resolve yet")
    # The resolver's own words, such as "[Errno -2] Name or service not known", are not part
    # of the contract; only that the hint names the host does.
    assert "does not resolve where the service runs" in found.message


RUN_TIMEOUT_S = 60


def held_file(tmp_path: Path) -> Path:
    """The committed [agent] table with `parallel = 1`, 2 runs per row and a 60 s run timeout,
    and two tests: the second one's question holds the fake model's hold marker. So the runs go
    in a known order: run 1 asks the first question, run 2, the held one, the second."""
    committed = kubernetes.CONFIG_FILE.read_text()
    agent = committed[: committed.index("[[test]]")]
    for old, new in (
        ("runs = 30\n", "runs = 2\n"),
        ("run_timeout = 30\n", f"run_timeout = {RUN_TIMEOUT_S}\nparallel = 1\n"),
    ):
        assert old in agent, old
        agent = agent.replace(old, new)
    tests = (
        TEST.format(name="order-status", question="Where is order 1234?"),
        TEST.format(name="order-status-held", question=HELD_QUESTION),
    )
    path = tmp_path / "scrimmage.toml"
    path.write_text(agent + "".join(tests))
    return path


def wait_for(what: str, check: Callable[[], bool], timeout: float) -> None:
    """Ask every 0.25 s: the hold leaves the test 40 s, so the harness's 2 s step is too slow."""
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"{what} after {timeout:.0f} s"
        time.sleep(0.25)


def run_summaries(cluster: Cluster, namespace: str) -> dict[str, dict[str, Any]]:
    """Each run's summary line, as the runner writes it to the volume when the run ends."""
    lines = cluster.reader_exec(
        namespace, "sh", "-c", f"cat {RUNS}/*/run-*.jsonl 2>/dev/null", check=False
    ).stdout
    return {
        line["run"]: line["summary"]
        for line in map(json.loads, lines.splitlines())
        if "summary" in line
    }


def test_a_policy_applied_mid_test_turns_every_later_run_into_an_infrastructure_error(
    cluster: Cluster, tags: Tags, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kubernetes, "CONFIG_FILE", held_file(tmp_path))
    assert serve.HOLD_S < RUN_TIMEOUT_S  # the hold must end before the run's own timeout
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        cluster.deploy(namespace, tags)
        wait_agents(cluster, namespace)
        cluster.start_reader(namespace, tags)  # reads the volume while the Job writes it
        with port_forward(cluster, namespace, "deployment/fake-model", serve.HOLD_PORT) as port:
            pod = start_job(cluster, namespace, tags)
            # Run 2 is inside its model call, and run 1 has ended with its last batch.
            wait_for("run 2's model call held", lambda: hold_state(port)["waiting"] == 1, 180)
            held_at = time.monotonic()
            wait_for("run 1's summary", lambda: len(run_summaries(cluster, namespace)) == 1, 10)
            assert connect_from_agents(cluster, namespace) == {"connected"}  # positive control
            block_agent_to_runner(cluster, namespace)
            wait_for(
                "the policy to drop the agent's connection",
                lambda: connect_from_agents(cluster, namespace) == {"TimeoutError"},
                15,
            )
            # Released by the test, not by the hold's own limit: run 2 was still waiting.
            assert release(port) == 1, f"the hold ended by itself after {serve.HOLD_S} s"
            assert time.monotonic() - held_at < serve.HOLD_S
        code = wait_ended(cluster, namespace, pod, timeout=300)
        said = cluster.kubectl("logs", pod, "-n", namespace, check=False).stdout
        log = tmp_path / f"{pod}.log"
        log.write_text(said)
        leaked = token_of(cluster, namespace) in said  # outside the assert: never printed
        assert not leaked, f"the token is in the runner's log, {log}"
        folders = cluster.reader_exec(namespace, "ls", RUNS).stdout.split()
        assert len(folders) == 1, folders
        result = json.loads(
            cluster.reader_exec(namespace, "cat", f"{RUNS}/{folders[0]}/result.json").stdout
        )
        summaries = run_summaries(cluster, namespace)
    details = {r["test"]: r["run_details"] for r in result["rows"]}
    assert sorted(details) == ["order-status", "order-status-held"], result["rows"]
    (first, third), (second, fourth) = details["order-status"], details["order-status-held"]
    # Run 1 was claimed and ended before the policy: the positive control inside this test.
    assert first["outcome"] == "answered", (first, log)
    # Run 2 was claimed before the policy, so its reply came, but its last batch could not.
    held = summaries[second["run"]]
    assert held["incomplete"] is True, (held, log)
    assert held["outcome"] == "infrastructure error", held
    # Its next model call could not reserve budget, so the reservation in flight stays held.
    assert f"{second['run']} ended with model:openai still holding" in said, log
    # Every later run's claim failed, and the gate said so with its own 503.
    for later in (third, fourth):
        assert later["outcome"] == "infrastructure error", (later, log)
        said_503 = f"{later['run']}: the service's gate answered 503 plan-unavailable"
        assert said_503 in json.dumps(result), result  # one of the result's messages
        assert summaries[later["run"]]["model_calls"] == 0, summaries[later["run"]]
    # No run claimed or started after the policy took effect has a result.
    outcomes = [d["outcome"] for d in (second, third, fourth)]
    assert outcomes == ["infrastructure error"] * 3, outcomes
    assert code == 3, f"exit {code}; the runner's log is {log}"
    assert (result["exit"], result["runs_sent"], result["claims_received"]) == (3, 5, 3), result
    assert said.rstrip().endswith(
        "see each run whose outcome is infrastructure error, and its .log file in the records "
        "folder"
    ), log
