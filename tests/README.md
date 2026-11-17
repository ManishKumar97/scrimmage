# Tests

The suite has two tiers.

| Tier | What is in it | Command |
|---|---|---|
| fast | tests in one process: the ledger, the gates, the config, the report | `uv run pytest -m "not slow"` |
| slow | tests that start a process or a real `scrimmage test` run | `uv run pytest -m slow` |

A test that starts a process must be marked `@pytest.mark.slow`, or `pytestmark = pytest.mark.slow` for a whole file.
A fixture in `conftest.py` checks this: a test not marked slow that starts a process fails, naming the rule.

## The NeMo Guardrails tests

`tests/test_nemo_rails.py` runs the real NeMo Guardrails library, which is its own dependency group, `nemo`.
A plain `uv sync` leaves it out, as it brings 36 packages and about 195 MB, among them onnxruntime, numpy and tokenizers, so those tests skip.
To run them, install the group, then run the file:

```sh
uv sync --group nemo && uv run pytest tests/test_nemo_rails.py
```

A later plain `uv sync` removes the group again.
The group does not install on Python 3.14 yet.

## The Docker tests

`tests/test_service_compose.py` runs the example service in a container under Docker Compose, with the runner on the host.
They are marked `docker` and `slow`.
They need a working Docker daemon, and a daemon that fails makes them fail, not skip.
Run them in one process and check that all six passed and none skipped:

```sh
J=$(mktemp -d)/docker.xml
uv run pytest -n 0 -m docker tests/test_service_compose.py --junitxml="$J" && uv run python tests/compose/check_junit.py "$J" 6
```

The first run downloads `python:3.12-slim` and the wheels the service needs, then builds two images, `scrimmage-compose-service` and `scrimmage-compose-egress`, each tagged by a hash of what it holds.
Later runs reuse them.
Everything stays on this machine.
Each test brings up a project of its own and takes it down, volumes included, in a `finally`.
To remove what the tests leave, run `docker rmi $(docker images -q 'scrimmage-compose-*')`.

## The Kubernetes tests

`tests/test_k8s_cluster.py` makes a kind cluster from `tests/k8s/kind.yaml`: one node, Calico as the network plugin, cert-manager for certificates.
kind is a test cluster, the one the Kubernetes project tests itself on.
The tests are marked `kind` and `slow`, and they are left out of every run unless `SCRIMMAGE_KIND=1` is set.
With it set, a missing `kind`, `kubectl`, `helm` or `docker` makes them fail, not skip.
The merge gate runs them for a branch only when `uv run python tools/kind_needed.py origin/main` exits 0, which it does for a change under `tests/k8s/`, to a kind test file, under `images/runner/` or to `docs/kubernetes.md`.
They also run once on main at the end of each slice of work, whatever the script says.
A session takes about four minutes to make its cluster.
Run one file at a time, in one process:

```sh
J=$(mktemp -d)/k8s.xml
SCRIMMAGE_KIND=1 SCRIMMAGE_LANE=2 test-slot uv run pytest -n 0 -m kind tests/test_k8s_cluster.py --junitxml="$J" && uv run python tests/compose/check_junit.py "$J" 6
```

The cluster is named `scrimmage-<lane>`, from `SCRIMMAGE_LANE` (default `1`), and a session deletes only the cluster of its own name.
The clusters use a Docker network of their own, `scrimmage-kind`, never the network named `kind`.
The harness gives kubectl, Helm and kind a kubeconfig and Helm folders of their own, so `~/.kube/config` and your Helm files are never read or written, and another cluster on the machine is never touched.
A second session waits while another lane's cluster runs and less than 6 GB of memory is free.
`SCRIMMAGE_KIND_KEEP=1` keeps the cluster for debugging and prints the command that deletes it.
`tests/k8s/versions.toml` pins the kind, node image, Calico and cert-manager versions, and `tests/k8s/probe.py` checks the assumptions the tests make about them on a real cluster.

`tests/test_k8s_manifests.py` applies the manifests in `tests/k8s/manifests/` to a namespace named `s8-<random>`, made from `namespace.yaml` with the Pod Security `restricted` standard enforced, warned and audited.
The agent Deployment has two replicas.
The runner Job, the reader pod and the records volume claim are there too, and the fake model is a small image built from `tests/k8s/fake_model/`.
Images are built with no network, loaded into the cluster with `kind load`, and never pulled.
The harness copies the control certificate's Secrets into the namespace, makes the token Secret from standard input so the token is on no command line, and fails a wait at once on a `FailedCreate` event, naming its message.
Every container has resource requests and a memory limit.
`tests/test_k8s_harness.py` checks that, and the manifests' other fixed shape, without a cluster.
Run it the same way, and check the count with `tests/compose/check_junit.py`.

## While you work

Run the fast tier, then the slow tests of the area you changed:

```sh
uv run pytest -m "not slow" && uv run pytest -m slow tests/test_budget_wait.py
```

Before you push, run everything, as CI does:

```sh
uv run pytest
```

## Coverage

CI measures branch coverage on Python 3.12 and later, in every process a test starts, and 3.11 runs every test with no tracer.
On 3.11, `sys.settrace` raises `RuntimeError` in a thread that starts while another thread is in a Python audit hook ([gh-98257](https://github.com/python/cpython/issues/98257), fixed in 3.12).
Coverage calls it as each thread starts, and the suite and the SDK each have such a hook, so a thread now and then died before its first line.
A coverage run on 3.11 stops at once and says so.
Coverage also loads `tools/coverage-threads`, a plugin of this repository's own.
Without it, coverage could delete a module another thread was importing, once `sys.path` had changed, and that import failed with `KeyError`; its docstring says how.
To measure it here, use a second environment, so the 3.11 one stays as it is:

```sh
export UV_PYTHON=3.12 UV_PROJECT_ENVIRONMENT=.venv-3.12
uv run coverage run -m pytest && uv run coverage combine && uv run coverage report
```

## Timing

A test never races the clock.
It waits for the event it means: a marker file the agent writes, a line in the record, the control server seeing an ask, or a fake clock.
The `time_limit` fixture makes a run's time limit arrive at such an event, as a short `run_timeout` races the program's start-up.
Where time itself is the claim, the margin is large enough to tell the two designs apart under any load, and a comment says so.
A wait for an event has a ceiling of 30 s or more, which only ends a test whose event never comes.

## Benchmarks

`uv run python tests/benchmark.py` measures what Scrimmage costs and prints every table on `docs/benchmarks.md`.
Run it on an otherwise idle machine.
`--rounds N` and `--section NAME` narrow it; `--section profile` shows where a gated call spends its time.
`tests/test_benchmark_budgets.py`, in the slow tier, fails when a cost goes over its ceiling.

### What the request count does not see

`test_a_gated_call_waits_on_the_expected_requests_to_the_runner` counts requests to the runner in a window of 20 calls, after 10 warm-up calls.
A round trip made only in the first 10 calls, or less often than once in 20 calls, is outside the window and passes.
A cost paid once per process, such as a request at the first call of a kind, is not caught by it.
The time ceilings catch such a cost only when it is large.

The count skips requests made on the sender thread, which carry record lines in the background.
So a gate that blocks until the sender has delivered a line is not counted, and nothing here would fail if one were added.
The test that sets the sender's window to an hour is the check for a wait of that kind at exit; see `docs/benchmarks.md`.
