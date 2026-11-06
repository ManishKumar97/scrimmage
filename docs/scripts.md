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

A wrapper such as `uv run python bot.py {question}` or `sh run.sh {question}` is fine.
The program under test is **the first Python in the run that has Derail installed**. It claims the run.
A Python before it with no Derail, such as a tool in your shell script or a package build `uv run` starts, runs as usual.
If no Python ever claims the run, nothing was tested, and the run is an infrastructure error.

Python after the claim is a child. So with `python pre.py; python bot.py` in a wrapper, `pre.py` is the program under test and `bot.py` is a child: no fault fires in it.
Write that as `python bot.py` with `pre.py` started by `bot.py`, or run the step before `derail test`, or set `children = true`.

Listed tools must live in a module your program imports. A tool defined in `bot.py` itself, or in the module of `python -m`, runs as `__main__`, is never imported and cannot be gated, so the run stops and says to move it.
A tool's file is gated under any name it is imported by, so `tools` and `deep.tools` are the same tool.

## The answer

Derail takes the answer from one of two places:

1. **The answer file.** A test sets `DERAIL_ANSWER_FILE`. If your program writes its answer to that path, the file is the answer, whole.
2. **The last printed line.** With no answer file, the answer is the last line your program printed to standard output that is not blank, if it ended with exit code 0.

Standard error is never the answer.
What the program prints goes to `derail-runs/<time>/<run>.out`, and standard error to `<run>.log`.

The last printed line is the last line in the file, whoever wrote it, because your program and its children share one output.
So a child that prints after your program gives the run the child's line as its answer. Derail cannot tell: nothing is recorded to say so.
To avoid it, print the answer last, stop the child from printing, or write the answer file.
An answer over several lines needs the answer file too, as only the last line is read.
A line ends only at a newline.

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
It fetches the plan at its first gated call, so a short command sends no request at all. It does load the SDK: a Python that joins and does nothing takes about 100 ms, against about 10 ms without Derail.

The first time a process writes a record line, the record says what it is: a line with the notice `process`, holding its interpreter, its command line and its parent's process id.
Known secret patterns are removed from the command line, and it is cut to 1000 characters.
Each call's line carries its own process id, so you can tell who made it.

If a child cannot get its plan, its gated call is refused and the next one tries again, and the run is an infrastructure error, as that says nothing about your agent.

### A Python child Derail cannot reach

A child with its own environment, such as `env={"PATH": ...}`, never runs Derail's startup file, and neither does one started with `python -I`, `-E` or `-S`.
Its writes would run with no gate and no line.
So every process that joined watches what it starts, through Python's audit hook.
A start of this run's Python that would run blind is refused where it is made: `subprocess.run` raises `DerailRefused`, the run ends as "refused call", and the record has a line labelled `process:python`.

The message gives the fix. Pass `env={**os.environ, ...}` so the child joins, or list `"process:python"` under `real` if a blind child is safe. A listed one leaves a `real` line.

A child that is not Python passes with any environment, and so does a Python elsewhere that has its own environment.
A stdio MCP server started by the MCP library passes too, on the library's small default environment, because the MCP gate decides what it is asked.
Only starts made from Python are seen. See [Limits](limits.md).

### A tool on another Python

A Python that is not the run's cannot be gated, but `tools` still names what must not run.
So when a program on another Python imports a module that holds a listed tool, the import fails with an `ImportError` that says why.
The tool never runs, the run ends as "refused call", and the record has a line with the notice `other_python` holding that program's details.
A tool in the file that program runs, or imported by a shorter name, is stopped the same way.
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

The MCP library's default gives a server a small clean environment, so such a server does not join. It starts all the same, and the MCP gate decides every call to it.

## How a run stops

At `run_timeout`, or when a refusal ends a run, Derail sends SIGTERM to the run's process group, which holds your program and every child it started.
After five seconds it sends SIGKILL to whatever is left.

The SDK handles SIGTERM in each process that joined: it sends its last record lines, marks any call still going on as stopped, and then ends the program as Python would have without Derail. So no `finally` block or `atexit` function runs, and the exit status says SIGTERM.
A program with its own SIGTERM handler replaces the SDK's, and then it decides what happens.

A child still running when your program ends is stopped the same way.
A daemon that starts its own session leaves the group. After the group stops, Derail finds it by the exact `DERAIL_RUN` in its environment, kills it, and records a `left_group` line, and the run table prints a note. See [Limits](limits.md).

A call still going on when your program exits normally, on a daemon thread, gets its line too.

## Running it by hand

`derail-run` is the command Derail starts your program through, and you can put it in front of any command, as in `derail-run python bot.py "Where is order 1234?"`.
Without a test running it changes nothing. See [Commands](cli.md#derail-run).
