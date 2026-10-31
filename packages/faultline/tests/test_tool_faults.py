"""Tool faults in `faultline mcp-wrap`, run as `faultline run` starts it, on a stub server."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from upstream import ASKS

from faultline.faults import ToolFault, ToolOptions, ToolRequest, ToolSession, parse
from faultline.model import ToolCall
from faultline.proxy.context import RunContext
from faultline.proxy.record import Exchange, Recorder

FAULTLINE = str(Path(sys.executable).parent / "faultline")
SERVER = str(Path(__file__).parent / "stdio_server.py")
TRACE_ID, ROOT = "ab" * 16, "cd" * 8


def line(message: dict) -> bytes:
    return (json.dumps(message) + "\n").encode()


def call(request_id: object, name: str, **arguments) -> bytes:
    params = {"name": name, "arguments": arguments}
    return line(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
    )


# A session as an MCP client starts one, then three tool calls.
OPENING = [
    line({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}),
    line({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    line({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
]


def wrapped(
    tmp_path: Path,
    sent: list[bytes],
    faults: list[str],
    stale_from: Path | None = None,
    in_turn: bool = False,
) -> tuple[list[bytes], bytes, list[dict]]:
    """The lines the client got, the bytes the server got, and the run's trace.

    With `in_turn`, each request waits for its answer before the next line goes,
    as an agent that calls one tool at a time does. Else all lines go at once.
    """
    trace = tmp_path / "trace"
    log = tmp_path / "server.log"
    tmp_path.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    run = RunContext(
        str(trace), TRACE_ID, ROOT, tuple(faults), None, stale_from and str(stale_from)
    )
    with subprocess.Popen(
        [FAULTLINE, "mcp-wrap", "--", sys.executable, SERVER, str(log), "0"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **run.env()},
    ) as wrapper:
        assert wrapper.stdin and wrapper.stdout and wrapper.stderr
        got: list[bytes] = []
        for message in sent:
            wrapper.stdin.write(message)
            wrapper.stdin.flush()
            if in_turn and "id" in json.loads(message):
                got.append(wrapper.stdout.readline())
        wrapper.stdin.close()
        got += wrapper.stdout.read().splitlines(keepends=True)
        error = wrapper.stderr.read()
        assert wrapper.wait(30) == 0, error
    return got, log.read_bytes(), spans(trace)


def spans(trace: Path) -> list[dict]:
    """The wrapper's execute_tool spans in call order, each as a dict of attributes."""
    found = []
    for path in sorted(trace.glob("mcp-*.jsonl")):
        for text in path.read_text().splitlines():
            for resource in json.loads(text)["resourceSpans"]:
                for scope in resource["scopeSpans"]:
                    found += scope["spans"]
    found.sort(key=lambda span: int(span["startTimeUnixNano"]))
    return [
        {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}
        for span in found
    ]


def direct(tmp_path: Path, sent: list[bytes]) -> list[bytes]:
    """The lines the server alone answers with, no wrapper in between."""
    result = subprocess.run(
        [sys.executable, SERVER, str(tmp_path / "direct.log"), "0"],
        input=b"".join(sent),
        capture_output=True,
        timeout=30,
        check=True,
    )
    return result.stdout.splitlines(keepends=True)


def test_a_tool_error_answers_the_chosen_call_and_no_other_byte_changes(tmp_path):
    targeted = call(3, "get_logs", service="cart")
    sent = [
        *OPENING,
        call(2, "get_logs", service="cart"),
        call("m", "get_metrics", service="cart"),
        targeted,
        b"not json at all\n",
        call(4, "get_logs", service="ad"),
    ]

    got, server_saw, recorded = wrapped(tmp_path, sent, ["tool:get_logs:error@2"])

    # The server got every byte but the targeted call's, and answered as it would alone.
    assert server_saw == b"".join(line for line in sent if line != targeted)
    [injected] = [line for line in got if json.loads(line).get("id") == 3]
    assert json.loads(injected)["result"] == {
        "content": [
            {
                "type": "text",
                "text": "Error executing tool get_logs: the backend did not respond "
                "(connection reset by peer)",
            }
        ],
        "isError": True,
    }
    assert [line for line in got if line != injected] == direct(
        tmp_path, [line for line in sent if line != targeted]
    )
    assert [
        (s["gen_ai.tool.name"], s.get("error.type"), s.get("faultline.injected"))
        for s in recorded
    ] == [
        ("get_logs", None, None),
        ("get_metrics", None, None),
        ("get_logs", "tool_error", "tool:get_logs:error@2"),
        ("get_logs", None, None),
    ]


def cancel(request_id: object) -> bytes:
    params = {"requestId": request_id, "reason": "timed out after 2.0s"}
    return line(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}
    )


def test_a_tool_timeout_never_answers_the_chosen_call(tmp_path):
    held = call(3, "get_logs", service="cart")
    sent = [
        *OPENING,
        call(2, "get_logs", service="cart"),
        held,
        call(4, "get_metrics", service="cart"),
        cancel(3),  # the client gave up on call 3
    ]

    got, server_saw, recorded = wrapped(tmp_path, sent, ["tool:get_logs:timeout@2"])

    # Neither the call nor its cancel reached the server, and no answer came back.
    assert server_saw == b"".join(sent[:4] + sent[5:6])
    assert [json.loads(line)["id"] for line in got] == [0, 1, 2, 4]
    assert [
        (s["gen_ai.tool.name"], s.get("error.type"), s.get("faultline.injected"))
        for s in recorded
    ] == [
        ("get_logs", None, None),
        ("get_logs", "timeout", "tool:get_logs:timeout@2"),
        ("get_metrics", None, None),
    ]


def test_a_held_call_never_cancelled_is_a_timeout_when_the_server_ends(tmp_path):
    sent = [*OPENING, call(2, "get_logs", service="cart")]

    got, server_saw, recorded = wrapped(tmp_path, sent, ["tool:get_logs:timeout"])

    assert server_saw == b"".join(OPENING)
    assert [json.loads(line)["id"] for line in got] == [0, 1]
    assert [(s.get("error.type"), s.get("faultline.injected")) for s in recorded] == [
        ("timeout", "tool:get_logs:timeout")
    ]


# Three calls of get_logs with one get_metrics call between them.
THREE_LOGS = [
    *OPENING,
    call(2, "get_logs", service="cart"),
    call(3, "get_metrics", service="cart"),
    call(4, "get_logs", service="ad"),
    call(5, "get_logs", service="cart"),
]
EMPTY = {"content": [], "isError": False}


@pytest.mark.parametrize(
    ("spec", "emptied"),
    [
        ("tool:get_logs:empty", [2, 4, 5]),  # every call of the tool
        ("tool:get_logs:empty@2", [4]),  # its second call only
        ("tool:get_logs:empty@4", []),  # no fourth call: nothing changes
    ],
)
def test_an_empty_result_answers_the_calls_its_selector_names(tmp_path, spec, emptied):
    got, server_saw, recorded = wrapped(tmp_path, THREE_LOGS, [spec])

    answers = {json.loads(line)["id"]: json.loads(line)["result"] for line in got}
    assert [i for i, result in answers.items() if result == EMPTY] == emptied
    assert set(answers) == {0, 1, 2, 3, 4, 5}
    # The server never saw an emptied call, and saw every other one.
    assert server_saw == b"".join(
        line for line in THREE_LOGS if json.loads(line).get("id") not in emptied
    )
    assert [s.get("faultline.injected") for s in recorded] == [
        spec if number in emptied else None for number in (2, 3, 4, 5)
    ]


def echo(arguments: dict) -> dict:
    """The stub server's own result for a call with these arguments."""
    text = f"echo {json.dumps(arguments)}"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def answers_of(got: list[bytes]) -> dict:
    return {json.loads(line)["id"]: json.loads(line)["result"] for line in got}


# get_logs on cart twice, then on ad: only the second cart call has an earlier result.
CART_CART_AD = [
    *OPENING,
    call(2, "get_logs", service="cart"),
    call(3, "get_metrics", service="cart"),
    call(4, "get_logs", service="cart"),
    call(5, "get_logs", service="ad"),
]


@pytest.mark.parametrize(
    ("spec", "stale"),
    [
        ("tool:get_logs:stale", [4]),  # every call that has an earlier result
        ("tool:get_logs:stale@2", [4]),  # the second call only
    ],
)
def test_a_stale_result_is_the_first_result_for_the_same_arguments(
    tmp_path, spec, stale
):
    got, server_saw, recorded = wrapped(tmp_path, CART_CART_AD, [spec], in_turn=True)

    answers = answers_of(got)
    assert [answers[i] for i in (2, 4, 5)] == [
        echo({"service": "cart"}),
        echo({"service": "cart"}),  # served from call 2 when stale, else the server
        echo({"service": "ad"}),  # never the result of another service
    ]
    assert server_saw == b"".join(
        line for line in CART_CART_AD if json.loads(line).get("id") not in stale
    )
    assert [s.get("faultline.injected") for s in recorded] == [
        spec if number in stale else None for number in (2, 3, 4, 5)
    ]
    assert [s.get("error.type") for s in recorded] == [None] * 4


def test_a_stale_call_with_new_arguments_passes_and_says_so(tmp_path):
    sent = [
        *OPENING,
        call(2, "get_logs", service="payment"),
        call(3, "get_logs", service="payment"),
        call(4, "get_logs", service="cart"),
    ]

    got, server_saw, recorded = wrapped(
        tmp_path, sent, ["tool:get_logs:stale"], in_turn=True
    )

    answers = answers_of(got)
    assert answers[4] == echo({"service": "cart"})  # not payment's result
    assert server_saw == b"".join(sent[:4] + sent[5:])
    assert [s.get("faultline.injected") for s in recorded] == [
        None,
        "tool:get_logs:stale",
        None,
    ]
    assert recorded[2]["faultline.injected.skipped"] == (
        "tool:get_logs:stale: no earlier result of get_logs for these arguments "
        "in this run to serve"
    )


def test_a_targeted_call_with_nothing_earlier_passes_and_says_so(tmp_path):
    _, _, recorded = wrapped(
        tmp_path, THREE_LOGS, ["tool:get_logs:stale@1"], in_turn=True
    )

    first = recorded[0]
    assert first.get("faultline.injected") is None
    assert first["faultline.injected.skipped"] == (
        "tool:get_logs:stale@1: no earlier result of get_logs for these arguments "
        "in this run to serve"
    )


def earlier_run(trace: Path, *calls: tuple[dict, str]) -> None:
    """A run's trace with get_logs results from an earlier day."""
    recorder = Recorder(trace / "model.jsonl", "ef" * 16, "12" * 8)
    # A trace is a run when it holds a model call.
    recorder.exchange(Exchange(1, 1, "/chat/completions", b"{}", 200, ASKS, 0, 1))
    recorder.flush()
    for arguments, result in calls:
        recorder.tool_call(
            ToolCall("get_logs", "", json.dumps(arguments), 0.1, result, started=1)
        )
    recorder.tool_call(  # a result a fault made is not one the tool gave
        ToolCall(
            "get_logs",
            "",
            '{"service": "cart"}',
            0.1,
            "",
            started=1,
            injected="tool:get_logs:empty",
        )
    )
    recorder.root(1, 2, ["agent"], 0)


def test_a_stale_result_comes_from_an_earlier_run_for_the_same_arguments(tmp_path):
    earlier = tmp_path / "earlier"
    earlier_run(earlier, ({"service": "ad"}, "old logs of ad"))

    got, server_saw, recorded = wrapped(
        tmp_path / "now",
        THREE_LOGS,
        ["tool:get_logs:stale"],
        stale_from=earlier,
        in_turn=True,
    )

    answers = answers_of(got)
    assert answers[4] == {
        "content": [{"type": "text", "text": "old logs of ad"}],
        "isError": False,
    }
    # Cart has no result in the earlier run: call 2 passes, call 5 gets its result.
    assert (answers[2], answers[5]) == (echo({"service": "cart"}),) * 2
    assert server_saw == b"".join(THREE_LOGS[:5])
    assert [s.get("faultline.injected") for s in recorded] == [
        None,
        None,
        "tool:get_logs:stale",
        "tool:get_logs:stale",
    ]
    assert recorded[0]["faultline.injected.skipped"] == (
        "tool:get_logs:stale: no earlier result of get_logs for these arguments "
        "in this run or in the earlier run to serve"
    )


def test_a_stale_result_from_text_fills_a_string_output_schema(tmp_path):
    fault = parse("tool:get_logs:stale")
    assert isinstance(fault, ToolFault)
    earlier = tmp_path / "earlier"
    earlier_run(earlier, ({"service": "ad"}, "old logs of ad"))
    injector = fault.injector(ToolOptions(stale_from=earlier))
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    session = ToolSession(schema, tags_results=True)

    injection = injector.request(
        ToolRequest(9, "get_logs", {"service": "ad"}, 1, session)
    )

    assert injection is not None and injection.answer is not None
    assert injection.answer["result"] == {
        "content": [{"type": "text", "text": "old logs of ad"}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": {"result": "old logs of ad"},
    }


def test_stale_from_must_be_a_directory(tmp_path):
    result = subprocess.run(
        [FAULTLINE, "run", "--stale-from", str(tmp_path / "none")]
        + ["--upstream", "http://127.0.0.1:9", "--", "true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "is not a directory" in result.stderr


def broken_trace(trace: Path) -> None:
    """Span files that cannot be read: a directory, a symlink loop, no permission."""
    (trace / "a.jsonl").mkdir(parents=True)
    (trace / "loop.jsonl").symlink_to(trace / "loop.jsonl")
    locked = trace / "locked.jsonl"
    locked.write_text("")
    locked.chmod(0)


def test_run_refuses_a_stale_from_dir_it_cannot_read(tmp_path):
    broken_trace(tmp_path / "earlier")

    result = subprocess.run(
        [FAULTLINE, "run", "--stale-from", str(tmp_path / "earlier")]
        + ["--upstream", "http://127.0.0.1:9", "--", "true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "cannot read a.jsonl: Is a directory" in result.stderr
    assert "cannot read locked.jsonl: Permission denied" in result.stderr
    assert "cannot read loop.jsonl: Too many levels of symbolic links" in result.stderr


def test_the_wrapper_skips_unreadable_span_files_and_says_so(tmp_path, capsys):
    earlier = tmp_path / "earlier"
    broken_trace(earlier)
    earlier_run(earlier, ({"service": "ad"}, "old logs of ad"))
    fault = parse("tool:get_logs:stale")
    assert isinstance(fault, ToolFault)

    injector = fault.injector(ToolOptions(stale_from=earlier))

    injection = injector.request(ToolRequest(1, "get_logs", {"service": "ad"}, 1))
    assert injection is not None and injection.answer is not None
    assert injection.answer["result"]["content"][0]["text"] == "old logs of ad"
    warned = capsys.readouterr().err
    assert "skipped a.jsonl: Is a directory" in warned
    assert "skipped loop.jsonl: Too many levels of symbolic links" in warned


@pytest.mark.parametrize(
    ("calls", "warning"),
    [
        ((), "holds no run; tool:get_logs:stale serves only results from this run"),
        (
            "metrics only",
            (
                "holds no successful result of get_logs; tool:get_logs:stale serves "
                "only results from this run"
            ),
        ),
    ],
)
def test_a_stale_from_dir_with_nothing_to_serve_says_so(
    tmp_path, capsys, calls, warning
):
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    if calls:
        earlier_run(earlier)  # a run whose only get_logs result a fault made
    fault = parse("tool:get_logs:stale")
    assert isinstance(fault, ToolFault)

    fault.injector(ToolOptions(stale_from=earlier))

    assert warning in capsys.readouterr().err


@pytest.mark.parametrize(
    ("spec", "wrong"),
    [
        ("tool:get_logs:wrong=payment", [2, 4, 5]),  # every call
        ("tool:get_logs:wrong=payment@2", [4]),  # the second call only
    ],
)
def test_a_wrong_result_names_another_service_and_records_it(tmp_path, spec, wrong):
    got, server_saw, recorded = wrapped(tmp_path, THREE_LOGS, [spec])

    answers = answers_of(got)
    asked = {2: "cart", 4: "ad", 5: "cart"}
    # The server answered every call. Each targeted answer now names payment.
    assert server_saw == b"".join(THREE_LOGS)
    assert {i: answers[i] for i in asked} == {
        i: echo({"service": "payment" if i in wrong else service})
        for i, service in asked.items()
    }
    assert answers[3] == echo({"service": "cart"})  # get_metrics is untouched
    assert [
        (
            s.get("faultline.injected"),
            s.get("faultline.injected.value"),
            s.get("faultline.injected.argument"),
        )
        for s in recorded
    ] == [
        (spec, "payment", "service") if n in wrong else (None, None, None)
        for n in (2, 3, 4, 5)
    ]
    assert all(s.get("error.type") is None for s in recorded)


def test_a_wrong_result_with_nothing_to_replace_passes_and_says_so(tmp_path):
    sent = [*OPENING, call(2, "get_logs", lines=5)]  # no string argument

    got, _, recorded = wrapped(tmp_path, sent, ["tool:get_logs:wrong=payment"])

    assert answers_of(got)[2] == echo({"lines": 5})
    [span] = recorded
    assert span.get("faultline.injected") is None
    assert span["faultline.injected.skipped"] == (
        "tool:get_logs:wrong=payment: the call passed no string argument to replace"
    )


def wrong_answer(spec: str, arguments: dict, text: str):
    """What tool:...:wrong does to one successful result holding `text`."""
    fault = parse(spec)
    assert isinstance(fault, ToolFault)
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"result": text},
            "isError": False,
        },
    }
    return fault.injector().response(ToolRequest(1, "get_logs", arguments, 1), message)


LOGS_OF_AD = "--- pod ad-5d9f7 (Ready)\nthread main: loaded 3 ads, header read\nerror"


def test_wrong_replaces_the_service_as_a_whole_word_only():
    injection = wrong_answer(
        "tool:get_logs:wrong=payment",
        {"level": "error", "service": "ad"},  # level comes first and is left alone
        LOGS_OF_AD,
    )

    assert injection is not None and injection.answer is not None
    expected = (
        "--- pod payment-5d9f7 (Ready)\nthread main: loaded 3 ads, header read\nerror"
    )
    result = injection.answer["result"]
    assert result["content"][0]["text"] == expected
    assert result["structuredContent"] == {"result": expected}
    assert (injection.value, injection.argument) == ("payment", "service")


def test_without_a_service_wrong_replaces_the_first_string_argument():
    injection = wrong_answer(
        "tool:get_logs:wrong=payment",
        {"lines": 5, "deployment": "ad", "level": "error"},
        LOGS_OF_AD,
    )

    assert injection is not None and injection.answer is not None
    text = injection.answer["result"]["content"][0]["text"]
    assert text.startswith("--- pod payment-5d9f7 (Ready)\n")
    assert text.endswith("\nerror")  # the level argument's value is untouched
    assert injection.argument == "deployment"


def test_wrong_with_the_value_nowhere_as_a_word_passes_and_says_so():
    injection = wrong_answer(
        "tool:get_logs:wrong=payment", {"service": "ad"}, "loaded 3 ads, header read"
    )

    assert injection is not None and injection.answer is None
    assert injection.skipped == "the service 'ad' is not in the result"


def test_an_error_result_is_not_data_to_make_wrong():
    fault = parse("tool:get_logs:wrong=ad")
    assert isinstance(fault, ToolFault)
    message = {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": True}}

    injection = fault.injector().response(
        ToolRequest(1, "get_logs", {"service": "cart"}, 1), message
    )

    assert injection is not None and injection.answer is None
    assert injection.skipped == "the tool failed, so there was no data to change"
