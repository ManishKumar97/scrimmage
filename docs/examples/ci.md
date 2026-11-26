# Scrimmage in CI

The [function agent](function-agent.md) as a repository that GitHub Actions tests on every pull request.
The job runs `scrimmage check`, then `scrimmage test --max-cost-usd 1`, and a failed row fails the job.
Read [Running Scrimmage in CI](../ci.md) first: `scrimmage.toml` names code, so the job runs a pull request's code with the job's key.

Libraries this example needs: `openai`.
This page was checked with openai 3.46.
Its workflow was run step by step on a scripted stand-in for the model, not on GitHub and not on a real model.
The `uv sync` step is the one it does not run: it installs your own environment, which a test cannot stand in for.

## The files

The repository holds the three files of the function agent, `shop/tools.py`, `shop/agent.py` and `scrimmage.toml`, unchanged.
It adds a `pyproject.toml`, which `uv sync` reads, and the workflow:

```text
.
├── .github/workflows/scrimmage.yml
├── pyproject.toml
├── scrimmage.toml
└── shop
    ├── agent.py
    └── tools.py
```

```toml title="pyproject.toml"
[project]
name = "shop"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["openai", "scrimmage"]
```

Until 0.1.0 is on PyPI, point `scrimmage` at the repository, as [Install](../install.md) describes.

```yaml title=".github/workflows/scrimmage.yml"
name: scrimmage
on:
  pull_request:

permissions:
  contents: read

jobs:
  test:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4.4.0
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6.8.0
      - run: uv sync
      - run: uv run scrimmage check
      - run: uv run scrimmage test --max-cost-usd 1
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

The job runs only for a branch in your own repository, reads the code and nothing else, and gives the key to the one step that spends it.
[Running Scrimmage in CI](../ci.md#a-workflow-that-checks-the-source-of-the-pull-request) says what each part is for.

## What the job prints

The second step, with no key:

```bash
uv run scrimmage check
```

```text title="Output"
scrimmage check: scrimmage.toml is valid: 1 test, 2 rows, 6 runs, 2 tools, a budget of $5, OpenAI prices as of 2026-11-22, Anthropic prices as of 2026-11-25
```

It exits 0.
`scrimmage check` reads the file and runs nothing, so it needs no key.

The third step, with the key from the secret:

```bash
uv run scrimmage test --max-cost-usd 1
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/runner/work/shop/shop/scrimmage-runs/20261126T080330Z-99f393
test          row       fault                 runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                     3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
order-status  fault 1   tool:get_order error  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
model calls cost $0.0002 of a budget of $1
```

It exits 0, and the job passes.
The last line shows the cap from the workflow, a budget of $1 and not the default $5.

## A failed row fails the job

Say a pull request makes the bot stop saying the order shipped.
The test in `scrimmage.toml` expects a word the bot no longer uses, here "delivered":

```diff title="scrimmage.toml"
-expect   = ["shipped", ["Oct 30", "October 30"]]
+expect   = ["delivered"]
```

```bash
uv run scrimmage test --max-cost-usd 1
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/runner/work/shop/shop/scrimmage-runs/20261126T080433Z-b57755
test          row       fault                 runs  fired  done  passed  rate  range  tools  cost     result
order-status  baseline  -                     3     -      3     0/3     0%    0-56%  1.0    0.00009  fail
order-status  fault 1   tool:get_order error  3     3      3     3/3     -     -      1.0    0.00009  baseline failed
range: a 95% Wilson interval, from these runs alone
fail: order-status baseline: 0/3 passed, pass_rate is 1
  3 runs: expect "delivered" not found: run-bb50ac816a5f, run-308fece32ff7, run-203e71ebb6c3
model calls cost $0.0002 of a budget of $1
```

The baseline fails, so the fault row is not judged: it says "baseline failed".
The step exits 1, GitHub marks the job failed, and the pull request cannot merge if you require the check.
The step must not end in `|| true`, which would turn that exit back into 0.

More: [Running Scrimmage in CI](../ci.md), [Commands](../cli.md) for each exit code.
