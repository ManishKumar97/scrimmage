# Example: the function agent behind FastAPI

Many agents run inside a web service, one request per question.
This example puts the [function agent](function-agent.md) behind a FastAPI endpoint and tests it there, under uvicorn, with the same mock and fault.
Scrimmage starts a test copy of the service, sends each run to it as one `POST`, and stops it when the test ends.

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

`start` and `service` replace `function`: the command that starts the service, and its address.
Scrimmage picks a free port and fills it in where the file writes `{port}`.
`body` is what the endpoint reads, and `answer` is the field that holds the answer:

```toml title="scrimmage.toml"
[agent]
start   = "uvicorn shop.service:app --port {port} --loop asyncio"
service = "http://127.0.0.1:{port}/chat"
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
expect = [["unable", "not able", "wasn't able", "was not able", "can't", "cannot", "couldn't", "could not", "don't have access", "do not have access", "i don't have", "i don't know", "timed out", "ran into", "sorry", "unfortunately", "try again", "again later", "i encountered", "i'm having trouble", "i'm having difficult", "issue retrieving", "issue with retrieving", "timeout issue", "i'm experiencing", "i am experiencing", "i'm currently experiencing"]]
forbid = ["ship", "dispatched", "delivered", "for delivery", "on its way", "on the way", "in transit", "out for delivery", "sent out", "left our", "arrive", "arriving", "cancelled", "canceled", "oct", "november", "nov ", "nov.", "yesterday", "tomorrow", "30th", "10/30", "30/10", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "april", "june", "july", "august", "september", "december"]
```

`--loop asyncio` is needed on Python 3.11 when uvloop is installed; from Python 3.12 it can be left out.
The openai library runs `uname -p` to name the machine it runs on, and a service run refuses every process start that `real` does not list, so `process:uname` is listed.

## Check the file

```bash
scrimmage check
```

```text title="Output"
scrimmage check: scrimmage.toml is valid: 1 test, 2 rows, 6 runs, 2 tools, a budget of $5, OpenAI prices as of 2026-11-22, Anthropic prices as of 2026-11-25
the service starts with: uvicorn shop.service:app --port {port} --loop asyncio
```

## Run it

One command starts the service, runs the test and stops the service:

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

You made no token file, set no variable and started nothing in the background.
The service's own output is in `service.log`, in the records folder.
When the test ends, or on Ctrl-C, Scrimmage stops every process of the service, workers included; [Let Scrimmage start the service](../services.md#let-scrimmage-start-the-service) has the details.

## In a container

The same service runs in a container, with the runner still on the host.
There you start the service yourself, and Scrimmage attaches to it, as [Attach to a service you started](../services.md#attach-to-a-service-you-started) shows.
The control channel then runs over TLS, and the container runs as the token file's owner.
[A service in a container](../docker.md#a-service-in-a-container) has the Compose file, the commands and what crosses the network.

## In Kubernetes

In a cluster, the runner runs as a Job and the service as a Deployment of two replicas.
Each run goes to one replica or the other, and each run's summary names the pod that served it.
The token and the control key arrive as Secret volumes, and the records stay on a volume after the Job's pod is gone.
[In Kubernetes](../kubernetes.md#in-kubernetes) has every manifest, the commands, and how to read the result.

More: [An agent inside a web service](../services.md), [Configuration](../configuration.md), [Safety](../safety.md).
