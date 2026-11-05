# Limits of this version

- **One agent shape.** The agent must be a Python function. Scripts and web services come later.
- **Tools only.** Faultline gates the tools you list under `tools`. Model calls, web requests and MCP tools pass untouched, and are not recorded or budgeted yet.
- **One fault.** `timeout`, firing on every matching call.
- **Tools reached by import path.** A tool your code stores in a list or dictionary the moment it is defined keeps the original function, so the gate cannot reach it. A marker for those comes later.
- **No judging.** The answer is recorded, not compared with `expect`.
- **Child programs** on the same Python get the mocks and safety rules but no faults; a forked child's record lines are not sent yet.
- **Python only.** The SDK must run inside the agent's own Python program.
