import json

import pytest

from faultline.faults import (
    FaultError,
    FaultShapeError,
    ModelFault,
    ToolFault,
    ToolRequest,
    ToolSession,
    empty_value,
    known,
    parse,
    parse_all,
)


def answer_of(
    fault: ToolFault, request_id: object, session: ToolSession | None = None
) -> dict:
    """What the fault answers the tool's first call with."""
    request = ToolRequest(request_id, fault.tool, {}, 1, session or ToolSession())
    injection = fault.injector().request(request)
    assert injection is not None and injection.answer is not None
    return injection.answer


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
        "tool:get_logs:melt",
        "tool::empty",
        "tool:get_logs:empty@0",
        "tool:get_logs:empty@",
        "tool:get_logs:wrong",
        "tool:get_logs:wrong=",
        "tool:get_logs:empty=cart",
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

    assert str(error.value).endswith(f"Known: {', '.join(known())}")
    assert "tool:NAME:empty[@CALL]" in known()
    assert "model:rate_limit[@STEP]" in known()


def test_the_empty_tool_result_is_a_valid_mcp_success_with_no_content():
    fault = parse_all(["tool:get_logs:empty"])[0]
    assert isinstance(fault, ToolFault)

    assert answer_of(fault, 7) == {
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

    answer = answer_of(fault, "a", ToolSession(STRING_RESULT, tags_results=True))

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


LIMITED = "Rate limit reached. Injected by faultline."


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/v1/chat/completions",
            {
                "error": {
                    "message": LIMITED,
                    "type": "requests",
                    "param": None,
                    "code": "rate_limit_exceeded",
                }
            },
        ),
        (
            "/v1/messages",
            {
                "type": "error",
                "error": {"type": "rate_limit_error", "message": LIMITED},
            },
        ),
    ],
)
def test_a_rate_limit_is_a_429_in_the_providers_own_shape(path, body):
    fault = parse("model:rate_limit")
    assert isinstance(fault, ModelFault)
    assert fault.action.answer

    answer = fault.action.answer(path)

    assert answer.status == 429
    assert answer.headers == {
        "Content-Type": "application/json",
        "retry-after": "1",
    }
    assert json.loads(answer.body) == body


def test_local_references_are_followed_for_the_empty_value():
    schema = {
        "type": "object",
        "properties": {"rows": {"$ref": "#/$defs/Rows"}, "note": {"type": "string"}},
        "required": ["rows", "note"],
        "$defs": {
            "Rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}},
            "Row": {"type": "object"},
        },
    }

    assert empty_value(schema) == {"rows": [], "note": ""}


def test_the_first_branch_with_an_empty_value_is_taken():
    schema = {
        "anyOf": [{"type": "integer"}, {"$ref": "#/definitions/Text"}],
        "definitions": {"Text": {"type": "string"}},
    }

    assert empty_value(schema) == ""


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ({"$ref": "#/$defs/Missing"}, "points at nothing"),
        ({"$ref": "https://example.com/schema.json"}, "non-local reference"),
        ({"$ref": "#"}, "refers to itself"),
    ],
)
def test_a_reference_that_cannot_be_followed_has_no_empty_value(schema, problem):
    with pytest.raises(ValueError, match=problem):
        empty_value(schema)


def test_an_empty_result_that_cannot_fit_the_schema_says_so_and_holds_an_answer():
    fault = parse("tool:count:empty")
    assert isinstance(fault, ToolFault)
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }

    with pytest.raises(FaultShapeError) as error:
        answer_of(fault, 1, ToolSession(schema, tags_results=True))

    assert "integer" in str(error.value)
    assert error.value.answer["result"] == {
        "content": [],
        "isError": False,
        "resultType": "complete",
    }


@pytest.mark.parametrize(
    ("spec", "value", "call"),
    [
        ("tool:get_logs:wrong=payment", "payment", None),
        ("tool:get_logs:wrong=payment@2", "payment", 2),
        ("tool:get_logs:wrong=ns:payment", "ns:payment", None),  # a colon in it
        ("tool:get_logs:wrong=ops@example.com", "ops@example.com", None),
    ],
)
def test_a_value_is_everything_after_the_equals_sign(spec, value, call):
    assert parse(spec) == ToolFault(spec, "get_logs", "wrong", call, value)


@pytest.mark.parametrize(
    ("value", "named"),
    [
        ('a"b', "a double quote"),
        ("a\\b", "a backslash"),
        ("a\nb", "the control character '\\n'"),
        ("a\x7fb", "the control character '\\x7f'"),
    ],
)
def test_a_value_that_would_break_json_in_a_result_is_refused(value, named):
    with pytest.raises(FaultError) as error:
        parse(f"tool:get_logs:wrong={value}")

    assert f"the value may not hold {named}" in str(error.value)
    assert "would break JSON" in str(error.value)


def test_a_tool_selector_names_the_nth_call_of_the_tool():
    assert parse("tool:get_logs:error@2") == ToolFault(
        "tool:get_logs:error@2", "get_logs", "error", 2
    )
    assert parse("tool:get_logs:error") == ToolFault(  # every call
        "tool:get_logs:error", "get_logs", "error", None
    )


@pytest.mark.parametrize(("spec", "targeted"), [("@2", [2]), ("", [1, 2, 3])])
def test_an_injector_targets_only_the_calls_its_selector_names(spec, targeted):
    fault = parse(f"tool:get_logs:error{spec}")
    assert isinstance(fault, ToolFault)
    injector = fault.injector()

    hit = [
        n
        for n in (1, 2, 3)
        if injector.request(ToolRequest(n, "get_logs", {}, n)) is not None
    ]

    assert hit == targeted


def test_a_tool_error_is_an_mcp_tool_error_the_model_can_read():
    fault = parse("tool:get_logs:error")
    assert isinstance(fault, ToolFault)

    answer = answer_of(fault, 4, ToolSession(STRING_RESULT, tags_results=True))

    # A tool error, not a protocol error: no structuredContent is required with it.
    assert answer == {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": "Error executing tool get_logs: the backend did not "
                    "respond (connection reset by peer)",
                }
            ],
            "isError": True,
            "resultType": "complete",
        },
    }
