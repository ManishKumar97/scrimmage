# An agent inside a web service

Many agents run inside a web service, one request per question.
This example puts the [function agent](function-agent.md) behind a FastAPI endpoint and tests it there, under uvicorn, with the same mock and fault.
The service is started once, and each run is one `POST` to it.

Libraries this example needs: `openai`, `fastapi` and `uvicorn`.
This page was checked with openai 3.46, fastapi 0.162 and uvicorn 0.54.

## The files

Start from the three files of the [function agent](function-agent.md).
Add the service:

```python title="shop/service.py"
"""The order bot inside a web service: POST /chat with {message, session}."""

import asyncio

from fastapi import FastAPI
from pydantic import BaseModel

from shop.agent import answer

app = FastAPI()


class Ask(BaseModel):
    message: str
    session: str = ""


@app.post("/chat")
async def chat(ask: Ask) -> dict[str, str]:
    # The bot is sync, so it runs on the loop's executor; Scrimmage's run goes with it.
    reply = await asyncio.get_running_loop().run_in_executor(None, answer, ask.message)
    return {"reply": reply}
```

`service` replaces `function`, with the body the endpoint reads and the field that holds the answer:

```toml title="scrimmage.toml"
[agent]
service = "http://127.0.0.1:8765/chat"
body    = {message = "{question}", session = "{session}"}
answer  = "reply"
tools   = ["shop.tools:get_order", "shop.tools:refund_order"]
real    = ["process:uname"]

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.fault]]
call   = "tool:get_order"
do     = "error"
raises = "ConnectionError"
expect = [["unable", "sorry", "try again"]]
forbid = ["Oct 30", "October 30"]
```

The openai library runs `uname -p` to name the machine it runs on, and a service run refuses every process start that `real` does not list, so `process:uname` is listed.

## Check the file

```bash
scrimmage check
```

```text title="Output"
scrimmage check: scrimmage.toml is valid: 1 test, 2 rows, 6 runs, 2 tools, a budget of $5
start the service with these two set, with scrimmage-run and SCRIMMAGE_SERVICE=1:
  SCRIMMAGE_TOOLS=shop.tools:get_order,shop.tools:refund_order
  SCRIMMAGE_ALLOW_IMPORTS=shop.
```

The two last lines are what the service's environment needs.

## Start the service

The service and the runner share a token, kept in a file only you can read, and the address of the runner's control server:

```bash
python -c "import secrets; print(secrets.token_hex(32))" > .scrimmage-token
chmod 600 .scrimmage-token
export SCRIMMAGE_TOKEN_FILE=$PWD/.scrimmage-token SCRIMMAGE_CONTROL_URL=http://127.0.0.1:8766
SCRIMMAGE_SERVICE=1 SCRIMMAGE_TOOLS=shop.tools:get_order,shop.tools:refund_order SCRIMMAGE_ALLOW_IMPORTS=shop. \
  scrimmage-run uvicorn shop.service:app --port 8765 --loop asyncio &
```

The service prints one line per process, `scrimmage: active in service mode`, and then serves as it always does.
A request with no Scrimmage header reaches the app untouched.

## Run it

In the same shell, with the same two variables:

```bash
scrimmage test
```

```text title="Output"
scrimmage test: 1 test, 2 rows, 6 runs; records in /home/me/shop/scrimmage-runs/20261112T144607Z-7a824c
test          row       fault                 runs  fired  done  passed  rate  range    tools  cost     result
order-status  baseline  -                     3     -      3     3/3     100%  44-100%  1.0    0.00009  pass
order-status  fault 1   tool:get_order error  3     3      3     3/3     100%  44-100%  1.0    0.00009  pass
range: a 95% Wilson interval, from these runs alone
control server: runs sent 7, claims received 7
model calls cost $0.0002 of a budget of $5
```

Each run's record stays open 5 seconds after its reply, `record_grace_s`, for work the service does after it answers, so this test takes about half a minute.
Before the first run, the runner checked that the service is gated with the same tools, and sent the first test's baseline once as a setup request.
The `control server:` line counts what the runner sent, the setup request and the 6 runs, and the claims the service made for them; the two numbers match when every request reached a gated service.
Each run then went to the service as its own request, under its own plan, while the service kept serving everything else.

## In a container

The same service runs in a container, with the runner still on the host.
The control channel then runs over TLS, and the container runs as the token file's owner.
[A service in a container](../services.md#a-service-in-a-container) has the Compose file, the commands and what crosses the network.

## In Kubernetes

In a cluster, the runner runs as a Job and the service as a Deployment of two replicas.
Each run goes to one replica or the other, and each run's summary names the pod that served it.
The token and the control key arrive as Secret volumes, and the records stay on a volume after the Job's pod is gone.
[In Kubernetes](../services.md#in-kubernetes) has every manifest, the commands, and how to read the result.

More: [An agent inside a web service](../services.md), [Configuration](../configuration.md), [Safety](../safety.md).
