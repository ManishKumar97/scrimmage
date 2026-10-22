import subprocess

from fake_model import FakeClient, logs_call, rejected, run_ida, text_reply, tool_reply
from fake_tools import fake_cluster, logs_registry
from openai.types.chat import ChatCompletionMessageCustomToolCall

from faultline import kube_tools
from faultline.tools import MAX_TOOL_OUTPUT_CHARS, Registry, clean_output


def test_tool_exception_goes_back_to_the_model():
    def hang(service):
        raise subprocess.TimeoutExpired(cmd="kubectl logs", timeout=30)

    client = FakeClient(tool_reply(logs_call("cart")), text_reply("kubectl timed out"))

    result = run_ida("why?", client, "fake", tools=logs_registry(hang))

    assert result.stop_reason == "done"
    assert client.tool_results() == [
        (
            "Error: get_logs raised TimeoutExpired: "
            "Command 'kubectl logs' timed out after 30 seconds"
        )
    ]


def test_bad_arguments_go_back_and_the_model_recovers():
    client = FakeClient(
        tool_reply(("get_logs", '{"svc": "cart"}')),
        tool_reply(("get_logs", "{not json")),
        tool_reply(logs_call("cart")),
        tool_reply(("get_logs", '{"service": 42}')),
        tool_reply(logs_call("cart")),
        text_reply("found it"),
    )
    tools = logs_registry(lambda service: f"logs of {service}")

    result = run_ida("why?", client, "fake", tools=tools)

    assert (result.answer, result.stop_reason) == ("found it", "done")
    missing, not_json, ok, wrong_type, _ = client.tool_results()
    assert missing == 'Error: bad arguments {"svc": "cart"} (service: Field required).'
    assert not_json.startswith(
        "Error: bad arguments {not json (arguments: Invalid JSON"
    )
    assert wrong_type == (
        'Error: bad arguments {"service": 42} (service: Input should be a valid string).'
    )
    assert ok == "logs of cart"


def test_unknown_tool_goes_back_to_the_model():
    client = FakeClient(tool_reply(("restart_pod", "{}")), text_reply("ok"))

    run_ida("why?", client, "fake", tools=logs_registry(str))

    assert client.tool_results() == [
        "Error: unknown tool 'restart_pod'. Available tools: get_logs."
    ]


def test_garbage_results_are_cleaned():
    assert clean_output(None) == "None"
    assert clean_output(b"\x00\xff") == "b'\\x00\\xff'"
    assert clean_output("ok\x00\x1b[31mred") == "ok[31mred"
    assert clean_output("   ") == "The tool returned an empty result."

    huge = "x" * 50_000 + "LAST LINE"
    cleaned = clean_output(huge)
    assert cleaned.startswith("[first 30009 characters cut]")
    assert cleaned.endswith("LAST LINE")
    assert len(cleaned) < MAX_TOOL_OUTPUT_CHARS + 50


def test_garbage_result_reaches_the_model_as_text():
    client = FakeClient(tool_reply(logs_call("cart")), text_reply("odd output"))
    tools = logs_registry(lambda service: {"not": "text"})

    result = run_ida("why?", client, "fake", tools=tools)

    assert result.stop_reason == "done"
    assert client.tool_results() == ["{'not': 'text'}"]


def test_repeated_tool_errors_stop_cleanly():
    bad = tool_reply(("get_logs", "{}"))
    client = FakeClient(bad, bad, bad)

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "tool_errors"
    assert result.answer.startswith("Stopped after 3 tool errors in a row.")
    assert len(client.requests) == 3


def test_one_success_resets_the_error_count():
    bad = tool_reply(("get_logs", "{}"))
    client = FakeClient(
        bad, bad, tool_reply(logs_call("cart")), bad, bad, text_reply("done")
    )

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "done"


def test_an_unknown_deployment_counts_as_a_tool_error(monkeypatch):
    fake_cluster(monkeypatch)
    bad = tool_reply(logs_call("order"))
    client = FakeClient(bad, bad, bad)
    logs = Registry()
    logs.tool(kube_tools.get_logs)

    result = run_ida("why?", client, "fake", tools=logs)

    assert result.stop_reason == "tool_errors"
    assert client.tool_results()[0] == (
        "Error: get_logs raised RuntimeError: "
        "no deployment named 'order' in astronomy-shop"
    )


def test_unparseable_tool_call_from_the_model_stops_cleanly():
    client = FakeClient(rejected(400, "bad tool call", code="tool_use_failed"))

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "bad_call"


def test_model_api_failure_stops_cleanly():
    too_large = rejected(413, "Request too large: Limit 8000, Requested 9625")
    client = FakeClient(tool_reply(("get_logs", "{}")), too_large)

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "model_error"
    assert "Limit 8000" in result.answer


def test_a_custom_tool_call_is_a_model_error():
    # Only function tools are sent, so another kind means the provider broke the contract.
    custom = {"id": "call_0", "type": "custom", "custom": {"name": "x", "input": "y"}}
    reply = tool_reply()
    reply.choices[0].message.tool_calls = [
        ChatCompletionMessageCustomToolCall.model_validate(custom)
    ]
    client = FakeClient(reply)

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "model_error"
    assert "unexpected tool call kind 'custom'" in result.answer


def test_a_reply_cut_off_at_the_output_limit_is_not_done():
    client = FakeClient(text_reply("The cart service fails because", "length"))

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "truncated"
    assert result.answer == "The cart service fails because"


def test_a_reply_stopped_by_the_content_filter_is_not_done():
    client = FakeClient(text_reply("", "content_filter"))

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "content_filtered"


def test_a_400_that_is_not_a_tool_call_failure_is_a_model_error():
    client = FakeClient(rejected(400, "context length exceeded"))

    result = run_ida("why?", client, "fake", tools=logs_registry(str))

    assert result.stop_reason == "model_error"
    assert "context length exceeded" in result.answer


def test_tool_calls_in_a_cut_off_reply_do_not_run():
    ran = []
    tools = logs_registry(lambda service: ran.append(service) or "logs")
    reply = tool_reply(logs_call("cart"))
    reply.choices[0].finish_reason = "length"

    result = run_ida("why?", FakeClient(reply), "fake", tools=tools)

    assert result.stop_reason == "truncated"
    assert ran == []


def test_the_second_openai_request_carries_the_calls_and_their_results():
    client = FakeClient(
        tool_reply(logs_call("cart"), logs_call("redis")), text_reply("done")
    )

    run_ida("why?", client, "fake", tools=logs_registry(lambda service: service))

    messages = client.requests[1]["messages"]
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert messages[1]["content"] == "why?"
    calls = messages[2]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_0", "call_1"]
    assert [c["function"]["name"] for c in calls] == ["get_logs", "get_logs"]
    assert [(m["tool_call_id"], m["content"]) for m in messages[3:]] == [
        ("call_0", "cart"),
        ("call_1", "redis"),
    ]
