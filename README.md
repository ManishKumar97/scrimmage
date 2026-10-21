# Faultline

Faultline finds the silent failures in an AI agent, proves the fix, and stops them from coming back.

This repository is at an early stage.
It currently holds the reference agent that Faultline will test: an incident diagnosis agent for a microservice app on Kubernetes.

## The diagnosis agent

The agent reads a question such as "Customers report checkout fails. Why?".
It calls tools in a loop until it can name a cause, quoting the log lines that support it.

- It uses the raw model APIs: chat completions for OpenAI-compatible providers (Groq by default) and the native Messages API for Anthropic. No agent framework.
- The loop talks to one small `ModelClient` interface in `src/faultline/models.py`. Each provider is one adapter behind it.
- Tools are plain async Python functions registered with a decorator. Their argument schemas come from type hints.
- Tools read the cluster through the Kubernetes API: logs of every pod of a service, pod status, and the list of services.
- Every run ends with a named stop reason: `done`, `tool_errors`, `bad_call`, `truncated`, `content_filtered`, `refused`, `model_error`, `step_limit`, `token_limit` or `time_limit`.
- One deadline covers the whole run, including model calls, retries and tool calls.

## Run it

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), a kubeconfig for the test cluster, and a provider API key.

```bash
cp .env.example .env        # then set the key for your provider
uv sync
uv run python -m faultline.ida "Customers report checkout fails. Why?"
```

## Pick a provider

One setting picks the provider: `PROVIDER` in `.env` or the environment.

| `PROVIDER` | Needs | Default model |
| --- | --- | --- |
| `groq` (default) | `GROQ_API_KEY`, optional `GROQ_BASE_URL` for any OpenAI-compatible endpoint | `openai/gpt-oss-120b` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` |

Set `MODEL` to override the default model of the chosen provider.

```bash
PROVIDER=anthropic uv run python -m faultline.ida "Customers report checkout fails. Why?"
```

## Checks

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
