"""The /openapi.json template check, as a pure function."""

import pytest

from scrimmage_core.openapi_check import NOT_CHECKED_NOTE, check_template

CHAT: dict[str, object] = {
    "type": "object",
    "required": ["message"],
    "additionalProperties": False,
    "properties": {
        "message": {"type": "string"},
        "session": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "limit": {"type": "integer"},
        "options": {
            "type": "object",
            "required": ["mode"],
            "properties": {"mode": {"type": "string"}, "deep": {"type": "object"}},
        },
    },
}


def inlined(schema: dict[str, object], path: str = "/chat") -> dict[str, object]:
    media = {"application/json": {"schema": schema}}
    return {"paths": {path: {"post": {"requestBody": {"content": media}}}}}


def with_ref(schema: dict[str, object], path: str = "/chat") -> dict[str, object]:
    doc = inlined({"$ref": "#/components/schemas/ChatIn"}, path)
    doc["components"] = {"schemas": {"ChatIn": schema}}
    return doc


def both(schema: dict[str, object]) -> list[dict[str, object]]:
    return [inlined(schema), with_ref(schema)]


def test_a_template_that_fits_has_no_warning_and_no_note() -> None:
    for doc in both(CHAT):
        result = check_template(doc, "/chat", "POST", {"message": "x", "session": "s1"})
        assert result.warnings == ()
        assert result.notes == ()


def test_a_template_missing_a_required_field_warns() -> None:
    for doc in both(CHAT):
        result = check_template(doc, "/chat", "POST", {"session": "s1"})
        assert result.warnings == (
            '[agent] body: /openapi.json says POST /chat requires "message"; '
            "the template has no such field",
        )


def test_a_template_with_an_extra_field_against_additional_properties_false_warns() -> None:
    for doc in both(CHAT):
        result = check_template(doc, "/chat", "POST", {"message": "x", "extra": 1})
        assert result.warnings == (
            '[agent] body: /openapi.json says POST /chat allows no field "extra"; '
            "the template has it",
        )


def test_an_extra_field_is_fine_when_the_schema_does_not_forbid_it() -> None:
    schema = {k: v for k, v in CHAT.items() if k != "additionalProperties"}
    result = check_template(inlined(schema), "/chat", "POST", {"message": "x", "extra": 1})
    assert result.warnings == ()


def test_a_template_with_a_wrong_type_warns() -> None:
    for doc in both(CHAT):
        result = check_template(doc, "/chat", "POST", {"message": 7})
        assert result.warnings == (
            '[agent] body: /openapi.json says POST /chat wants field "message" as string; '
            "the template has integer",
        )


def test_an_integer_fits_number_and_a_boolean_is_not_an_integer() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"n": {"type": "number"}, "i": {"type": "integer"}},
    }
    assert check_template(inlined(schema), "/chat", "POST", {"n": 3}).warnings == ()
    assert len(check_template(inlined(schema), "/chat", "POST", {"i": True}).warnings) == 1


def test_a_nullable_field_is_checked_through_its_non_null_branch() -> None:
    for doc in both(CHAT):
        wrong = check_template(doc, "/chat", "POST", {"message": "x", "session": 5})
        assert wrong.warnings == (
            '[agent] body: /openapi.json says POST /chat wants field "session" as string; '
            "the template has integer",
        )
        for session in (None, "s"):
            fits = check_template(doc, "/chat", "POST", {"message": "x", "session": session})
            assert fits.warnings == ()
            assert fits.notes == ()


def test_an_object_property_is_checked_one_level_down() -> None:
    for doc in both(CHAT):
        missing = check_template(doc, "/chat", "POST", {"message": "x", "options": {}})
        assert missing.warnings == (
            '[agent] body: /openapi.json says POST /chat requires "options.mode"; '
            "the template has no such field",
        )
        wrong = check_template(doc, "/chat", "POST", {"message": "x", "options": {"mode": 1}})
        assert len(wrong.warnings) == 1
        assert '"options.mode"' in wrong.warnings[0]


def test_nothing_is_checked_two_levels_down() -> None:
    inner: dict[str, object] = {"type": "object", "required": ["c"]}
    middle: dict[str, object] = {"type": "object", "properties": {"b": inner}}
    schema: dict[str, object] = {"type": "object", "properties": {"a": middle}}
    result = check_template(inlined(schema), "/chat", "POST", {"a": {"b": {}}})
    assert result.warnings == ()
    assert result.notes == (NOT_CHECKED_NOTE,)


def test_a_ref_outside_components_is_not_followed() -> None:
    # Each target would give a warning if it were followed.
    strict: dict[str, object] = {"type": "object", "required": ["z"]}
    for target in ("#/x/Strict", "https://example.com/schema.json", "#/components/other/Strict"):
        doc = inlined({"$ref": target})
        doc["x"] = {"Strict": strict}
        doc["components"] = {"other": {"Strict": strict}}
        top = check_template(doc, "/chat", "POST", {"message": "x"})
        assert (top.warnings, top.notes) == ((), (NOT_CHECKED_NOTE,))
        prop = {"type": "object", "properties": {"m": {"$ref": target}}}
        doc2 = inlined(prop)
        doc2["x"] = {"Strict": strict}
        nested = check_template(doc2, "/chat", "POST", {"m": {}})
        assert (nested.warnings, nested.notes) == ((), (NOT_CHECKED_NOTE,))


def test_a_ref_cycle_ends() -> None:
    doc = inlined({"$ref": "#/components/schemas/A"})
    doc["components"] = {"schemas": {"A": {"$ref": "#/components/schemas/A"}}}
    result = check_template(doc, "/chat", "POST", {})
    assert (result.warnings, result.notes) == ((), (NOT_CHECKED_NOTE,))


def test_a_nullable_chain_cut_at_the_depth_limit_sets_the_note() -> None:
    schemas: dict[str, object] = {
        f"A{i}": {"anyOf": [{"$ref": f"#/components/schemas/A{i + 1}"}, {"type": "null"}]}
        for i in range(12)
    }
    doc = inlined({"$ref": "#/components/schemas/A0"})
    doc["components"] = {"schemas": schemas}
    result = check_template(doc, "/chat", "POST", {})
    assert (result.warnings, result.notes) == ((), (NOT_CHECKED_NOTE,))


def test_a_list_body_with_items_sets_the_note() -> None:
    tool: dict[str, object] = {"type": "object", "required": ["name"]}
    schema: dict[str, object] = {"type": "array", "items": {"$ref": "#/components/schemas/Tool"}}
    doc = inlined(schema)
    doc["components"] = {"schemas": {"Tool": tool}}
    result = check_template(doc, "/chat", "POST", [{}])
    assert (result.warnings, result.notes) == ((), (NOT_CHECKED_NOTE,))


def test_array_items_in_a_property_set_the_note() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"a": {"type": "array", "items": {"type": "string"}}},
    }
    result = check_template(inlined(schema), "/chat", "POST", {"a": [1]})
    assert (result.warnings, result.notes) == ((), (NOT_CHECKED_NOTE,))


def test_keys_the_code_does_not_read_set_the_note_and_annotations_do_not() -> None:
    for key, val in (
        ("prefixItems", []),
        ("patternProperties", {}),
        ("discriminator", {}),
        ("multipleOf", 2),
        ("exclusiveMinimum", 0),
        ("uniqueItems", True),
        ("additionalProperties", {"type": "string"}),
    ):
        schema: dict[str, object] = {"type": "object", key: val}
        result = check_template(inlined(schema), "/chat", "POST", {})
        assert result.notes == (NOT_CHECKED_NOTE,), key
    notes: dict[str, object] = {
        "type": "object",
        "title": "T",
        "description": "d",
        "default": {},
        "examples": [{}],
        "deprecated": False,
        "readOnly": False,
        "writeOnly": False,
        "additionalProperties": True,
    }
    assert check_template(inlined(notes), "/chat", "POST", {}).notes == ()


def test_a_float_with_an_integral_value_counts_as_an_integer() -> None:
    schema: dict[str, object] = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert check_template(inlined(schema), "/chat", "POST", {"n": 3.0}).warnings == ()
    odd = check_template(inlined(schema), "/chat", "POST", {"n": 3.5})
    assert odd.warnings == (
        '[agent] body: /openapi.json says POST /chat wants field "n" as integer; '
        "the template has number",
    )


def test_a_value_fastapi_would_coerce_is_a_heuristic_note_not_a_warning() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "max_tokens": {"type": "integer"},
            "temp": {"type": "number"},
            "stream": {"type": "boolean"},
        },
    }
    body = {"max_tokens": "10", "temp": "0.5", "stream": "true"}
    result = check_template(inlined(schema), "/chat", "POST", body)
    assert result.warnings == ()
    assert result.notes[0] == (
        '[agent] body: field "max_tokens" is a string where /openapi.json wants integer; '
        "FastAPI usually coerces this (heuristic)"
    )
    assert len(result.notes) == 3
    for bad in ({"max_tokens": "ten"}, {"max_tokens": "1.5"}, {"stream": "maybe"}):
        assert len(check_template(inlined(schema), "/chat", "POST", bad).warnings) == 1


@pytest.mark.parametrize(
    "word", ["1", "0", "yes", "no", "on", "off", "t", "f", "y", "n", "True", "YES", "Off", "T"]
)
def test_every_string_pydantic_reads_as_a_bool_is_a_note(word: str) -> None:
    schema: dict[str, object] = {"type": "object", "properties": {"stream": {"type": "boolean"}}}
    result = check_template(inlined(schema), "/chat", "POST", {"stream": word})
    assert result.warnings == ()
    assert len(result.notes) == 1
    assert result.notes[0].endswith("FastAPI usually coerces this (heuristic)")


@pytest.mark.parametrize("word", ["maybe", "2", "", "none", "truee", " true ", "yes\n"])
def test_a_string_pydantic_rejects_as_a_bool_still_warns(word: str) -> None:
    schema: dict[str, object] = {"type": "object", "properties": {"stream": {"type": "boolean"}}}
    result = check_template(inlined(schema), "/chat", "POST", {"stream": word})
    assert len(result.warnings) == 1


def test_a_router_prefix_route_is_found_in_full_and_the_note_names_the_route() -> None:
    doc = inlined(CHAT, "/api/chat")
    found = check_template(doc, "/api/chat", "POST", {"x": 1}, prefix="/api")
    assert found.warnings[0].startswith("[agent] body: /openapi.json says POST /api/chat requires")
    lost = check_template(doc, "/api/other", "POST", {}, prefix="/api")
    assert lost.notes == (
        "[agent] body: /openapi.json has no JSON request body for POST /api/other; not checked",
    )


def test_a_list_valued_type_prints_as_plain_words() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"s": {"type": ["string", "null"]}},
    }
    result = check_template(inlined(schema), "/chat", "POST", {"s": 5})
    assert result.warnings == (
        '[agent] body: /openapi.json says POST /chat wants field "s" as string or null; '
        "the template has integer",
    )


def test_a_service_under_a_prefix_is_found_at_the_prefix() -> None:
    doc = inlined(CHAT, "/chat")
    found = check_template(doc, "/api/chat", "POST", {"x": 1}, prefix="/api")
    assert found.warnings[0].startswith("[agent] body: /openapi.json says POST /chat requires")
    assert found.notes == ()
    # Without the prefix taken off, the path is not in the document.
    lost = check_template(doc, "/api/chat", "POST", {"x": 1})
    assert lost.warnings == ()
    assert len(lost.notes) == 1
    assert "POST /api/chat" in lost.notes[0]


def test_no_openapi_document_is_one_note() -> None:
    for doc in (None, "not json", [], {}, {"paths": {}}, inlined(CHAT, "/other")):
        result = check_template(doc, "/chat", "POST", {"message": 1})
        assert result.warnings == ()
        assert len(result.notes) == 1
        assert result.notes[0].startswith("[agent] body: /openapi.json has no JSON request body")


def test_a_path_without_a_post_or_a_json_body_is_one_note() -> None:
    put = {"requestBody": {"content": {"text/plain": {}}}}
    doc = {"paths": {"/chat": {"get": {}, "put": put}}}
    for method in ("POST", "PUT", "GET"):
        result = check_template(doc, "/chat", method, {})
        assert (result.warnings, len(result.notes)) == ((), 1)


def test_the_label_names_the_agent_in_every_line() -> None:
    result = check_template(inlined(CHAT), "/chat", "POST", {}, label="tool:search")
    assert result.warnings[0].startswith("[tool:search] body: ")


def test_a_keyword_outside_the_subset_is_not_checked_and_the_note_says_so() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"m": {"type": "string", "pattern": "^a"}},
    }
    result = check_template(inlined(schema), "/chat", "POST", {"m": "zzz"})
    assert result.warnings == ()
    assert result.notes == (NOT_CHECKED_NOTE,)
    plain = check_template(inlined(CHAT), "/chat", "POST", {"message": "x"})
    assert plain.notes == ()


def test_a_body_that_is_not_an_object_warns() -> None:
    result = check_template(inlined(CHAT), "/chat", "POST", ["x"])
    assert result.warnings == (
        "[agent] body: /openapi.json says POST /chat wants the body as object; "
        "the template has array",
    )
