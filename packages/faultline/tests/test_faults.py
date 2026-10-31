import json

import pytest

from faultline.faults import (
    FaultError,
    ModelFault,
    ToolFault,
    ToolSession,
    empty_value,
    parse,
    parse_all,
)


def test_a_tool_fault_names_the_tool_and_the_kind():
    assert parse("tool:get_logs:empty") == ToolFault(
        "tool:get_logs:empty", "get_logs", "empty"
    )


def test_a_tool_name_may_hold_a_colon():
    assert parse("tool:ns:get_logs:empty") == ToolFault(
        "tool:ns:get_logs:empty", "ns:get_logs", "empty"
    )


def test_a_model_fault_is_on_step_one_unless_a_step_is_given():
    assert parse("model:rate_limit") == ModelFault("model:rate_limit", "rate_limit", 1)
    assert parse("model:rate_limit@3") == ModelFault(
        "model:rate_limit@3", "rate_limit", 3
    )


@pytest.mark.parametrize(
    "spec",
    [
        "tool:get_logs:timeout",
        "tool::empty",
        "tool:empty",
        "model:rate_limit@0",
        "model:rate_limit@x",
        "model:rate_limit@",
        "model:outage",
        "disk:full",
        "",
    ],
)
def test_an_unknown_spec_is_an_error_that_names_the_known_ones(spec):
    with pytest.raises(FaultError) as error:
        parse(spec)

    assert "Known: tool:NAME:empty, model:rate_limit[@STEP]" in str(error.value)


def test_the_empty_tool_result_is_a_valid_mcp_success_with_no_content():
    fault = parse_all(["tool:get_logs:empty"])[0]
    assert isinstance(fault, ToolFault)

    assert fault.answer(7, ToolSession()) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [], "isError": False},
    }


# The schema MCP's Python SDK declares for a tool that returns a string.
STRING_RESULT = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}


def test_the_empty_result_fits_the_session_it_answers_in():
    fault = parse("tool:get_logs:empty")
    assert isinstance(fault, ToolFault)

    answer = fault.answer("a", ToolSession(STRING_RESULT, tags_results=True))

    assert answer["result"] == {
        "content": [],
        "isError": False,
        "structuredContent": {"result": ""},
        "resultType": "complete",
    }


@pytest.mark.parametrize(
    ("schema", "empty"),
    [
        ({"type": "array", "items": {"type": "number"}}, []),
        ({"type": ["integer", "null"]}, None),
        (
            {
                "properties": {"rows": {"type": "array"}, "note": {"type": "string"}},
                "required": ["rows"],
            },
            {"rows": []},
        ),
    ],
)
def test_an_empty_value_exists_for_strings_arrays_objects_and_nulls(schema, empty):
    assert empty_value(schema) == empty


def test_a_required_number_has_no_empty_value():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }

    with pytest.raises(ValueError, match="integer"):
        empty_value(schema)


@pytest.mark.parametrize(
    ("path", "error_type"),
    [("/chat/completions", "requests"), ("/v1/messages", "rate_limit_error")],
)
def test_a_rate_limit_is_a_429_in_the_providers_own_shape(path, error_type):
    fault = parse("model:rate_limit")
    assert isinstance(fault, ModelFault)

    answer = fault.answer(path)

    assert answer.status == 429
    assert answer.headers["Retry-After"] == "1"
    assert json.loads(answer.body)["error"]["type"] == error_type
