# Running Scrimmage in CI

This page is for a team that wants `scrimmage test` to run on every pull request.
It says what makes that unsafe, and gives one workflow that is not.

## scrimmage.toml runs code

`scrimmage.toml` names code that Scrimmage imports and runs.
The agent's `function` or `command` is such code, and so is a service's `start` and a mock's `returns_from`.
So `scrimmage test` on a pull request runs that pull request's code, with every secret the job holds, such as the model key.

A pull request can change `scrimmage.toml`, the agent, or any file the agent imports.
Scrimmage does not sandbox the agent.
Its checks stop the calls a test is meant to stop, as [Safety](safety.md) describes.
They do not stop code that reads the job's environment and sends it out.

## Never in these places

- Never on a pull request from a fork, or from anyone who cannot already push to your repository.
- Never under `pull_request_target`.
  That trigger runs with the secrets of the base repository.
  A workflow that checks out the pull request's code there gives that code the secrets.
- Never under `workflow_run`.
  It runs with secrets after another workflow, and it is often used to check out that workflow's pull request.
- Never under `issue_comment` or `pull_request_review_comment`.
  Anyone who can comment can start them, and they run from the default branch with secrets.
- Never on a self-hosted runner that an outside pull request can reach.
  The runner keeps its files and its network between jobs.

The general rule is this.
Any trigger that checks out a pull request's head while the job holds secrets is the same mistake.

A pull request from a branch in your own repository comes from someone with write access.
That is the only kind this page runs Scrimmage on.

GitHub's own advice is in [Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use).
It says to avoid self-hosted runners for public repositories.
If you must use one, make it ephemeral and keep the repository private.

## Before this works

The sample below runs `uv sync`, so Scrimmage must be a dependency in your own `pyproject.toml` and lock.
Until 0.1.0 is on PyPI, that means installing from the repository, as [Install](install.md) describes.
The `uv sync` line is your own environment, so change it to whatever installs your agent and Scrimmage.
If `scrimmage.toml` is not at the root of the repository, pass its path: `scrimmage check path/to/scrimmage.toml` and `scrimmage test path/to/scrimmage.toml`.

## A workflow that checks the source of the pull request

```yaml
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

If your agent calls Anthropic, pass `ANTHROPIC_API_KEY` in the same `env` block, from a secret of the same name.

What each part does:

- `on: pull_request`
  - The job runs the pull request's code with the pull request's own permissions.
  - GitHub gives a fork's run no secrets.
- `if: ... head.repo.full_name == github.repository`
  - The job is skipped when the pull request's branch lives in another repository.
- `permissions: contents: read`
  - The job's token can read the code and nothing else.
- `persist-credentials: false`
  - The checkout does not leave the token in `.git` for the agent's code to read.
- `env` on the last step only
  - The key is in the one step that needs it, not in the job or the workflow.
- `--max-cost-usd 1`
  - A cap on what the whole test may spend on model calls, set in the workflow.
- `scrimmage check`
  - It reads the file and runs nothing, so it needs no key.
  - See [Commands](cli.md#scrimmage-check).

Pin each action to a full commit SHA, as above, with the version in a comment.
A tag such as `v4` can be moved by whoever owns the action.
A SHA cannot.

Scrimmage's exit code fails the job.
0 is a pass, 1 is a failed row, and 2 and 3 are mistakes or infrastructure errors.
See [Commands](cli.md) for each code.
Do not add `|| true` to the step, as it hides a failed row.

## Review what a pull request can change

A pull request can change more than `scrimmage.toml`.
Require a review on `.github/workflows/`, on the agent's code and on its dependencies, as well as on `scrimmage.toml`.
Branch protection with code owners does that.
A change to any of them can run new code with the job's key.

## Keys come from secrets

Put the model key in a repository secret, and pass it to the one step that needs it, as `env:` on that step.
Never write a key in `scrimmage.toml`, in the agent's code or in the workflow file.
The file is read by everyone who can see the repository, and a pull request can change it.

Give the key a spending limit at your provider.
It is the one control that holds even if the agent's code misuses the key.
Use a key made only for CI, so you can revoke it alone.

A `headers` value in a service's file may hold `${NAME}`, which Scrimmage fills from the environment.
Use that for a token the service needs, so the file holds the name and the secret stays in the job.

## Real and mocked tools in CI

A tool call runs for real only if `[agent] real` lists it.
Every other tool call must be mocked, or the run stops as "refused call".
This holds in CI exactly as on your machine.

So in CI:

- Mock every tool that writes, such as `refund_order`.
- List under `real` only tools that read.
- Do not write `real = ["tool:*"]` in a file CI runs, as it lets every write run for real.
- Point the tools that you list at a test copy of the service, never at production.

Model calls are real, because they are what is being tested.
They are the calls that spend the key.

## Where to go next

- [Safety](safety.md) has the rules a test applies call by call.
- [Security](security.md) says where your data goes and what a record keeps.
- [Configuration](configuration.md) lists every setting of `scrimmage.toml`.

Example: [Scrimmage in CI](examples/ci.md) runs this workflow's steps and shows a failed row failing the job.
