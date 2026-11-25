# Drafting a test file

`scrimmage init` writes a first `scrimmage.toml` for an agent you already have.
It reads your code and names what it found.
You then add one question and its expected answer, and say which tools are safe to run for real.

```bash
cd your-agent
scrimmage init
```

```
scrimmage init: read 14 Python files under . without importing them
entry point (a guess): support.agent:answer (one argument, named question, in a module that imports openai)
probe: not run; to find the tools the model is offered, choose the function to run and name it: scrimmage init --probe module:function --out another.toml
tools: 2 found by reading the code
  support.tools:get_order
  support.tools:refund_order
model hosts: gateway.acme.internal (openai)
wrote scrimmage.toml
next: write one [[test]] with a question and its expect, list the tools that are safe to run for real under real, then run scrimmage check
```

The draft is a starting point: read every line. A guess that reads like a fact would be worse than no draft, so anything init could not be sure of is a comment.

## What it reads

`scrimmage init [folder]` reads every `.py` file under the folder with Python's `ast` module.
It parses the code and imports nothing, so none of your code runs.
It skips `.venv`, `venv`, `site-packages`, `node_modules`, `scrimmage-runs` and hidden folders, and it says which files it could not read as Python.
A file over 5 MB is treated as generated: it is skipped, and reported as too large.
A `.py` symlink that points outside the folder is not read, and a note names it, as its target is not part of the code in the folder.
The read stops after 5000 `.py` files, in a fixed order, and a note says so, with `skipped N .py files` for the files it walked past and did not parse. That count is by name alone, with no stat, and stops at 100000 names, then says `at least`; counting 100000 names took about 1.2 seconds on a busy 4-core machine; reading 5000 small files took about 18 seconds on a busy 4-core machine, so give the folder that holds the agent.
Module names are the import names of an installed package: a `src` folder is not part of them, and a folder inside a package keeps its package's name.
When the top package cannot be imported from the folder you read, a note tells you to install it, such as with `pip install -e .`, because `scrimmage test` will need it.
To read one part of a large checkout, give that part as the folder, such as `scrimmage init src/mypackage/domains/airline --out scrimmage.toml`.
Names used by several parts then stop being ambiguous.

It looks for three things.

**The entry point**, which is a guess. It is written with a comment that says so, and the guess is never run.
Best first:

| Rank | Candidate |
|---|---|
| 1 | A top-level function with one required parameter named `question`, `query`, `prompt`, `message`, `text`, `input` or `user_input`, in a module that imports `openai`, `anthropic`, `langchain`, `langgraph`, `mcp` or `httpx` |
| 2 | A module with `if __name__ == "__main__":` that reads `sys.argv` or uses `argparse`, as `command = "python <path> {question}"` |
| 3 | The same function as rank 1, in a module that imports none of those |

Within a rank, a module that builds a model client comes before the rest.
A module that imports both `openai` and `anthropic` is named for both.
The best is written. The others are comments, up to five, and `--json` lists all of them.
A function a tool decorator marks, or one named in a tool spec, is a tool and never an entry point.

**The tools**, by two heuristics:

- a function or method with a decorator named `tool`, `is_tool`, `function_tool`, or ending `_tool`;
- a `"name"` in a dict literal with `"type": "function"` or `"parameters"`, matched to a function or method of that name.

Tools made at run time, as most real agents make them, have no such mark; `--probe` finds those.

**The model hosts**: `base_url="..."` written as text in an `OpenAI`, `AsyncOpenAI`, `AzureOpenAI`, `Anthropic` or `AsyncAnthropic` call.
A host read from an environment variable is not text in the code, so only the probe sees it.
The client class says the provider: an `Anthropic` client's host is drafted with `provider = "anthropic"`, and an `OpenAI` client's with `provider = "openai"`.
A provider's own host, such as `api.anthropic.com` for an `Anthropic` client, needs no line.

## An agent that uses Claude

For an agent that imports `anthropic`, init names the library in the entry point's reason, as it names `openai`.
A gateway you reach with `Anthropic(base_url="https://gateway.acme.internal")` is drafted as a `model_hosts` line with `provider = "anthropic"`:

```toml
model_hosts = [{host = "gateway.acme.internal", provider = "anthropic"}]
```

With `--probe`, a first request to `POST /v1/messages` on a host that is not Anthropic's own is stopped, and its host is drafted with `provider = "anthropic"` in the same way.
The probe sets a placeholder `ANTHROPIC_API_KEY` when none is in the environment, so the client can be built.
It sends nothing.
An agent that imports both `openai` and `anthropic` is named for both.
Each client's host gets its own provider's line, and one gateway that both clients use is listed once for each provider.
The draft plus one `[[test]]` passes `scrimmage check`.
Model prices for Anthropic models are not shipped yet, so set them under `[agent.prices]` before a run with a budget.

## What it writes

| A tool | Written |
|---|---|
| One top-level function | `module:function` |
| One method on a top-level class, plain, `staticmethod` or `classmethod` | `module:Class.method` |
| A framework's tool | Listed under `adapter_tools`, with no `tools` entry: LangChain tools reach a gate by themselves |
| A method on a nested class | Not written; a comment says so. Move the class to the top of its module |
| A name with two or more matches | Not written; `unresolved`, with every candidate. Pick one and write it as `{path, name}` |
| A name with no match | Not written; `unresolved`: it may be made at run time |
| A model-facing name that differs from the function's | `{path = "...", name = "..."}` |

It never writes:

- a tool under `real`. `real` stays empty, because only you know which tools are safe. Names that start `get_`, `list_`, `search_`, `find_`, `lookup_`, `read_`, `check_` or `fetch_` are listed in a comment as ones that only look like reads, labelled as a heuristic. `get_and_reset_balance` starts `get_`, and it is a write;
- a `[[test]]`, a question, an `expect` or a mock. An expected answer must come from you;
- over a file. If the target exists, nothing runs, nothing is written, and the command exits 2. Use `--out` to write somewhere else.

So the draft does not load until you add a test. `scrimmage check` says `at least one [[test]] is required`, then, once you add one, whether the rest is valid.

## --probe

```bash
scrimmage init --probe support.agent:answer
```

The static read cannot name a tool made by a dict comprehension or a framework.
`--probe` takes the function to run, as `module:function`, and runs only that function. You choose it: the command prints it first, and never runs its own guess, and it does not print or return a ready command for the guess either.
A bare `--probe`, a script path, or anything that is not `module:function` exits 2 and runs nothing.
When the module's top-level name cannot be found in the folder or the environment, `--probe` exits 2 with a mistake that names the module and says to install it or fix the name. Nothing is run and no draft is written.
Only the top-level name is looked for before the probe runs, without importing it.
A missing submodule under a real package, such as `evil.missing:answer`, is found by the probe: when its crash log holds a `ModuleNotFoundError` that names the probed module or a package above it, `--probe` exits 2 with the same mistake and writes no draft.
A module that your function imports and cannot find is your agent's crash, and stays `crashed, see <log>`.
Scrimmage starts the function the way `scrimmage test` does, with the question `scrimmage init probe`.
The probe stops at the first model request. That request is refused before it is sent, and the tools it offers are read from it.
The probe costs $0 in model calls Scrimmage sees, as it sends none. A paid call through a library it does not gate is not seen.
With no key in the environment, the run gets a placeholder `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` so the client can be built. A model request through a gated client never carries it out, but the agent's own requests can: a POST with `requests`, `urllib` or a `curl` subprocess did send it in testing.
The probe is opt-in, and `--json` never turns it on.

In probe mode a POST to a path Scrimmage knows, such as `/v1/chat/completions`, counts as a model request on any host, so a gateway you have not named yet is found and refused, never sent.
Outside the probe, the normal rule stands: only hosts in the table or under `model_hosts` are model calls.

After any refusal, at the model gate or at a web, tool or MCP gate, the probe ends the process. Code that catches the refusal runs no further.

### What the probe can do

!!! warning "The probe runs your code"
    It runs the function you name up to its first model request, as a program, with no tool list and no budget.
    A plain tool function called before the first model request runs for real, because the probe does not know your tools yet.

A web write through httpx or httpx2, a LangChain tool, and an MCP tool call are refused, as in `scrimmage test`.
A write through a library Scrimmage does not gate, such as `requests`, runs, and so does a subprocess.
Most agents call no tool before the model replies, but that is your agent's loop, not a promise from Scrimmage.
The probe prints this every time it runs.
A script entry point is never probed: its command can do anything before Python starts.

### What ended the probe

`probe.outcome` says it in words:

| The probe | `probe.outcome` |
|---|---|
| Reached a model request | `stopped at POST /v1/chat/completions on <host>, refused before it was sent` |
| Reached a first model request that offers no tools, such as an embeddings call | `stopped at POST /v1/embeddings on <host>, which offers no tools, refused before it was sent; no tools are known` |
| Was refused at another call first | `stopped at <label> <method> <path>, refused before it ran; no tools are known` |
| Returned with no POST to a model path through httpx or httpx2 | `saw no model request through a gated client` |
| Crashed, or passed its 120 seconds | `crashed, see <log>` or `timed out, see <log>` |

When no tools are known, the candidates from reading the code stand.
The number to read is `numbers`: `tools_mapped` over `tools_offered`.

| Exit code | Meaning |
|---|---|
| 0 | A draft was written with an entry point |
| 2 | No entry point was found, so the draft has the line to fill in; or the target file exists, so nothing was written |
| 3 | Scrimmage's own failure, or the probe's infrastructure error; no draft was written, and a probe leaves its records under `scrimmage-runs/` |
