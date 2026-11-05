# How it works

Faultline has two parts.

- The **runner** is the `faultline` command. It reads `faultline.toml`, makes a plan for each run, starts your agent, and collects the answer and the record. It never runs inside your agent.
- The **SDK** is Faultline's code inside your agent's own Python program. It catches each call and does what the plan says. It is off unless a test switches it on.

## One run, step by step

1. `faultline test` starts a small **control server** on your machine (127.0.0.1 only) and makes a random secret for this test.
2. For each run, it stores the run's **plan**: the mocks, the fault, the tools and what is safe to run for real.
3. It starts a new Python program through `faultline-run`, in its own process group, with the run's name, the control server's address and the secret in environment variables.
4. `faultline-run` puts a folder holding one file, `sitecustomize.py`, first on Python's path. Python runs that file before anything else.
5. The file sees the secret and switches the SDK on. The SDK fetches the plan, checks its signature, and gets ready to swap your tools. It prints one line saying Faultline is active.
6. Then it runs any other `sitecustomize.py` your environment has, such as Ubuntu's crash reporter, so your program starts as it always does.
7. Faultline's starter calls your function with the question.
8. When your code imports a module named under `tools`, the SDK swaps each named function for a **gate**, a function of the same kind, sync or async.
9. Each tool call meets its gate. The gate decides fault, mock, real or refused, does it, and hands a record line to a background thread that sends lines in small batches.
10. When the program ends, the runner reads the answer, closes the run, and writes the summary line.

Nothing on disk is edited. The swaps exist only in the memory of that one program.

## The plan and the secret

The plan is data only: it cannot carry code.
Every request from the SDK and every reply from the control server is signed with the secret (an HMAC), and the secret itself never travels.
So a fake server listening at the same address cannot hand your agent a plan, and a copied request cannot be replayed.

## Failing closed

If the SDK cannot switch on fully, for any reason, the program stops before your agent's code runs.
Your agent never runs in a test without its gates.

The one exception is a child program on a different Python, such as a cloud command-line tool your agent starts.
It inherits the environment but not Faultline, so it is left to run as normal.

## Child processes

A child Python program your agent starts on the same Python joins the run: the mocks and the safety rules apply to it, and its calls go into the same record.
Faults fire only in the program the runner started, because each process counts its own calls.
