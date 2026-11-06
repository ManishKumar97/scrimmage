# An agent run as a script

Some agents are not a function.
They are a program you start with a question, such as `python bot.py "Where is order 1234?"`, that prints its answer and ends.

Derail starts that program itself, once per run, and changes nothing in your code.

## The command

Write the command under `[agent]`, in place of `function`, with `{question}` where the question goes:

```toml
[agent]
command = "python bot.py {question}"
tools   = ["acme.tools:get_order", "acme.tools:check_inventory", "acme.tools:refund_order"]
real    = ["tool:check_inventory"]

[[test]]
name     = "order-status"
question = "Where is order 1234; rm -rf ~"
expect   = ["shipped"]
```

Derail splits the command into arguments when it reads the file, the way a shell splits words, so quotes work as usual.
Then it puts the question into its argument.
No shell is ever involved, so the question above reaches `bot.py` as one argument, `sys.argv[1]`, and `rm` never runs.

`{question}` is the only placeholder, and the command must hold it. It can sit inside an argument, as in `--ask={question}`.

The program runs in the folder that holds `derail.toml`.
`python` is found on your `PATH`, so run `derail test` in the environment your agent runs in.
If that Python has no Derail installed, the program stops before your agent runs, and the run is an infrastructure error.
A wrapper such as `uv run python bot.py {question}` is fine: the first Python in the run is the program under test.

## The answer

Derail takes the answer from one of two places:

1. **The answer file.** A test sets `DERAIL_ANSWER_FILE`. If your program writes its answer to that path, the file is the answer, whole.
2. **The last printed line.** With no answer file, the answer is the last line your program printed to standard output that is not blank, if it ended with exit code 0.

Standard error is never the answer.
What the program prints goes to `derail-runs/<time>/<run>.out`, and standard error to `<run>.log`.

A child that shares your program's output can print after it, and then its line is the last one.
An answer over several lines has the same problem, as only the last line is read.
In both cases write the answer file:

```python
import os

path = os.environ.get("DERAIL_ANSWER_FILE")
if path:
    with open(path, "w") as out:
        out.write(answer)
```

A program that ends with exit code 0 and gives no answer counts as crashed.

## Child processes

Most Python programs your agent starts inherit Derail's environment.
What happens next depends on which Python they run.

| The child runs | What happens |
|---|---|
| The same Python as your program, by any name, such as `python3` | It joins the run |
| Another Python, even another venv on the same binary | It runs untouched and prints nothing, unless it imports a tool under `tools` |
| Another Python, with `children = true` and Derail installed | It joins the run |

A child that joins gets the same mocks and safety rules, and its calls go into the same record.
So a child's `refund_order` is refused, and the refusal ends the run as if your program had made the call.

A child that joins prints nothing, so it adds nothing to its parent's output, and a stdio MCP server's protocol stream stays clean.
It fetches the plan at its first gated call, and until then it costs nothing, so a short command such as `pip --version` sends no request at all.

The first time a process makes a call or a record line, the record says what it is: a line with the notice `process`, holding its interpreter, its command line and its parent's process id.
Known secret patterns are removed from the command line, and it is cut to 1000 characters.
Each call's line carries its own process id, so you can tell who made it.

If a child cannot get its plan, every gated call in it is refused.

### A tool on another Python

A Python that is not the run's cannot be gated, but `tools` still names what must not run.
So when a program on another Python imports a module that holds a listed tool, the import fails with an `ImportError` that says why.
The tool never runs, the run ends as "refused call", and the record has a line with the notice `other_python` holding that program's details.
Every other program on that Python runs as if Derail were not there.

### Faults in children

A fault fires only in the program Derail started, because each process counts its own calls, so "the third call" would mean something different in each.
In a child the same call gets its mock, or follows the safety rules.

Set `children = true` to fire faults in children too.
Then `first 1` fires once in each process, not once in the run.
The same setting lets a child on another Python join, if it has Derail installed.

### Forked children

`os.fork` and `multiprocessing` work the same as a child you start.
A forked child sends each record line as it makes it, so a line is not lost when `multiprocessing` ends its worker with no exit steps.
Its refusal reaches the runner before the error reaches your code.

The other start methods of `multiprocessing`, `spawn` and `forkserver`, start a new Python, which joins as a child.

### MCP servers over stdio

A stdio MCP server you start with `env={**os.environ}` is a child like any other.
On your Python it joins, so its own tools and web requests follow the test, and it prints nothing to the stream that carries the protocol.
On another Python it serves untouched, even on `mcp` 1.x.

The MCP library's default gives a server a small clean environment, so such a server does not join: see [Limits](limits.md).

## How a run stops

At `run_timeout`, or when a refusal ends a run, Derail sends SIGTERM to the run's process group, which holds your program and every child it started.
After five seconds it sends SIGKILL to whatever is left.

The SDK handles SIGTERM in each process that joined: it sends its last record lines, marks any call still going on as stopped, and then ends the program as Python would have without Derail. So no `finally` block or `atexit` function runs, and the exit status says SIGTERM.
A program with its own SIGTERM handler replaces the SDK's, and then it decides what happens.

A child still running when your program ends is stopped the same way, so nothing outlives its run.
A daemon that starts its own session is the exception: see [Limits](limits.md).

## Running it by hand

`derail-run` is the command Derail starts your program through, and you can put it in front of any command, as in `derail-run python bot.py "Where is order 1234?"`.
Without a test running it changes nothing. See [Commands](cli.md#derail-run).
