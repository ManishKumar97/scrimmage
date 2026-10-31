# Faultline

Faultline finds the silent failures in an AI agent, proves the fix, and stops them from coming back.

This repository is at an early stage.
It holds two packages in one uv workspace.

- `packages/faultline` is the product, the `faultline` package.
  It reads an agent's OpenTelemetry span files into runs, steps and tool calls.
  It has no runtime dependencies and imports nothing from the lab.
- `lab` is the test bed, the `faultline-lab` package (module `faultline_lab`).
  It holds the reference agent that Faultline tests, an incident diagnosis agent for a microservice app on Kubernetes, and the runner that injects the incidents in `lab/incidents.yaml`.
  The lab imports the product, never the reverse.

## The diagnosis agent

The agent reads a question such as "Customers report checkout fails. Why?".
It calls tools in a loop until it can name a cause, quoting the log lines that support it.

- It uses the raw model APIs: chat completions for OpenAI-compatible providers (Groq by default) and the native Messages API for Anthropic. No agent framework.
- The loop talks to one small `ModelClient` interface in `lab/src/faultline_lab/models.py`. Each provider is one adapter behind it.
- Tools are plain async Python functions registered with a decorator. Their argument schemas come from type hints.
- Tools read the cluster through the Kubernetes API: logs of every pod of a service, pod status, and the list of services.
- A traces tool queries Jaeger (the test bed's trace store) through the Kubernetes API service proxy, so no port-forward is needed. It shows which service a failing request called and where the failure started.
- A metrics tool queries Prometheus the same way. It gives a service's request rate, error rate and latency now against the 30 minutes before, and the error rate over time. It runs as an MCP server beside the logs server.
- Every run ends with a named stop reason: `done`, `tool_errors`, `bad_call`, `truncated`, `content_filtered`, `refused`, `model_error`, `step_limit`, `token_limit` or `time_limit`.
- One deadline covers the whole run, including model calls, retries and tool calls.

## Traces

Every run writes OpenTelemetry spans to a local file.
Nothing is sent over the network unless a standard OTLP endpoint variable is set.

- One root span per run (`invoke_agent`), one child span per model call (`chat <model>`) and one per tool call (`execute_tool <name>`).
- Tool spans are children of the run span, not of a model call, because a reply's tool calls run at the same time. Each span has `faultline.step`, the number of the model call it belongs to.
- Attribute names follow the OpenTelemetry GenAI semantic conventions: operation, provider, model, input and output tokens, finish reasons, tool name, tool call id.
- The root span carries the stop reason, steps, tokens and budget. A tool error, a model API error and any stop reason other than `done` set the span status to ERROR with the error text.
- Spans are written as OTLP JSON, one line per export, to `runs/traces.jsonl` (the `runs/` folder is git-ignored). `TRACE_FILE` changes the path.
- Spans hold the system prompt, the messages, the tool definitions, tool arguments and results, the question and the answer, because a later replay needs them. They are written to a local file and go nowhere else by default.
- When `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the spans also go to that endpoint over OTLP/HTTP, as from any instrumented service. The file is still written.

## Run it

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), a kubeconfig for the test cluster, and a provider API key.

```bash
cp .env.example .env        # then set the key for your provider
uv sync --all-packages
uv run python -m faultline_lab.ida "Customers report checkout fails. Why?"
```

Print the runs in span files with the product's reader:

```bash
uv run python -m faultline.runs runs/traces.jsonl
```

Find the faults in the last hour of runs, from a file or from Jaeger's query API (v3).
A Jaeger source needs the traced service and a window; give the base URL with Jaeger's base path, if it has one.

```bash
uv run faultline find --since 1h runs/traces.jsonl
uv run faultline find --source jaeger:http://localhost:16686 --service faultline-ida --since 1h
```

Serve the same report as JSON over HTTP, for Grafana or a script.
`GET /api/findings?since=1h` answers what `faultline find --json --since 1h` prints; `GET /api/health` answers `{"status": "ok"}`.
It is read-only and listens on 127.0.0.1:8765 unless `--host` and `--port` say otherwise.

```bash
uv run faultline serve --source jaeger:http://localhost:16686 --service faultline-ida
curl 'http://127.0.0.1:8765/api/findings?since=1h'
```

## Show the findings in Grafana

`deploy/grafana/` holds a dashboard and a datasource for any Grafana 11 that can reach `faultline serve`.
The dashboard lists every finding in a window before now (run, fault kind, check, flagged, certainty, evidence) and counts the faults per kind.
It reads the JSON API through the [Infinity](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/) datasource plugin.

1. Install the plugin, for example with `GF_INSTALL_PLUGINS=yesoreyeram-infinity-datasource` or `grafana cli plugins install yesoreyeram-infinity-datasource`.
   Pin a release that supports your Grafana: 3.7.1 is the last one for Grafana 11.5.
2. Copy `deploy/grafana/datasource.yaml` into Grafana's datasource provisioning folder, by default `/etc/grafana/provisioning/datasources/`.
   Set `url` and `allowedHosts` to where `faultline serve` listens, as Grafana's server sees it.
   Keep the uid `faultline`: the dashboard's panels name it.
3. Add a file dashboard provider in `/etc/grafana/provisioning/dashboards/`, and put `deploy/grafana/faultline-dashboard.json` in the folder it reads:

   ```yaml
   apiVersion: 1
   providers:
     - name: faultline
       type: file
       folder: Faultline
       options:
         path: /var/lib/grafana/dashboards/faultline
   ```

4. Start `faultline serve` on an address Grafana can reach, then restart Grafana.
   The dashboard asks for `/api/findings?since=` the window picked in its Window variable (15m to 7d).

With the Grafana Helm chart the same files go in the `plugins`, `datasources`, `dashboardProviders` and `dashboardsConfigMaps` values, the dashboard in a ConfigMap.

## Pick a provider

One setting picks the provider: `PROVIDER` in `.env` or the environment.

| `PROVIDER` | Needs | Default model |
| --- | --- | --- |
| `groq` | `GROQ_API_KEY`, optional `GROQ_BASE_URL` for any OpenAI-compatible endpoint | `openai/gpt-oss-120b` |
| `anthropic` (default) | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` |

Each provider has its own model setting, `GROQ_MODEL` and `ANTHROPIC_MODEL`, so switching `PROVIDER` never sends one provider's model name to the other.

```bash
PROVIDER=anthropic uv run python -m faultline_lab.ida "Customers report checkout fails. Why?"
```

## Checks

```bash
uv run ruff check
uv run pyright
uv run pytest
```
