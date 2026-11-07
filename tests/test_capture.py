"""The parts of a recording that need no gate: type-strict equality, the key-name rule
and how a web body is written."""

import dataclasses
from typing import NamedTuple

import pydantic
import pytest

from scrimmage_core import capture
from scrimmage_core.redact import REMOVED


@dataclasses.dataclass
class Stock:
    sku: str
    count: int


class Point(NamedTuple):
    x: int
    y: float


class Order(pydantic.BaseModel):
    order_id: str = pydantic.Field(alias="orderId")
    total: float
    items: list[str]


def get_order(order_id: str) -> Order:
    return Order(orderId=order_id, total=9.0, items=["SKU-1"])


@pytest.mark.design("record.strict_equal")
@pytest.mark.parametrize(
    ("real", "built", "message"),
    [
        (True, 1, "returns: bool True, got int 1"),
        (1, 1.0, "returns: int 1, got float 1.0"),
        ([1, 2], (1, 2), "returns: list [1, 2], got tuple (1, 2)"),
        ({"items": [{"qty": 3}]}, {"items": [{"qty": 3.0}]}, "returns.items[0].qty: int 3"),
        ({"a": 1}, {"b": 1}, "returns: keys differ"),
        ([1], [1, 2], "returns: 1 items, got 2"),
        (Stock("a", 1), Stock("a", True), "returns.count: int 1, got bool True"),
    ],
)
def test_equality_is_type_strict_at_every_level_and_names_the_place(
    real: object, built: object, message: str
) -> None:
    found = capture.differ(real, built, "returns")
    assert found is not None and found.startswith(message), found


@pytest.mark.design("record.strict_equal")
def test_nan_equals_nan_and_equal_values_are_equal() -> None:
    assert capture.differ(float("nan"), float("nan"), "r") is None
    assert capture.differ(Point(1, 2.0), Point(1, 2.0), "r") is None
    assert capture.differ(Order(orderId="1", total=1.0, items=[]), get_order("1"), "r")


@pytest.mark.design("record.secret_key_name")
@pytest.mark.parametrize(
    "key",
    ["password", "X-Auth-Token", "APIKey", "api_key", "private.key", "session", "Cookie",
     "client_secret", "next_token", "AccessKey", "pwd2", "user credentials"],
)  # fmt: skip
def test_a_whole_word_part_names_a_secret(key: str) -> None:
    assert capture.secret_key(key)


@pytest.mark.design("record.key_name_whole_parts")
@pytest.mark.parametrize(
    "key",
    ["author", "authority", "max_tokens", "tokens_used", "sessions_total", "keys", "api",
     "private", "passport", "secretary", "monkey"],
)  # fmt: skip
def test_a_key_that_only_holds_a_secret_word_inside_another_is_not_one(key: str) -> None:
    assert not capture.secret_key(key)


@pytest.mark.design("record.secret_key_name")
def test_the_key_rule_runs_on_a_json_body_before_the_form_is_chosen() -> None:
    plain = capture.web_body('{"user": "bob", "password": "hunter2"}', [])
    assert (plain.form, plain.value) == ("json", {"user": "bob", "password": REMOVED})
    assert plain.removed == [{"key": "password", "rule": "key_name"}]
    # A null keeps a body from being json; exact text cannot drop the password and stay exact.
    with_null = capture.web_body('{"password": "hunter2", "note": null}', [])
    assert (with_null.form, with_null.code) == (None, "secret_changed_shape")
    exact = capture.web_body('{"note": null}', [])
    assert (exact.form, exact.value) == ("text", '{"note": null}')
    pattern = [{"key": None, "rule": "pattern"}]
    assert capture.web_body('{"note": null, "k": "[secret removed]"}', pattern).code == (
        "secret_changed_shape"
    )
    text = capture.web_body("key: [secret removed]", pattern)
    assert (text.form, text.removed) == ("text", pattern)
    assert capture.web_body(str(2**70), []).form == "text"


class Nested:
    @dataclasses.dataclass
    class Inner:
        n: int


def get_inner() -> object:
    return Nested.Inner(1)


@dataclasses.dataclass
class Picky:
    n: int

    def __post_init__(self) -> None:
        if self.n > 1:
            raise TypeError("too big")


def get_picky() -> Picky:
    return Picky(1)


def same(value: object) -> object:
    return value


@pytest.mark.design("record.unsavable")
def test_a_value_whose_class_a_mock_cannot_name_or_build_is_unsavable() -> None:
    nested = capture.tool_value(get_inner(), {}, get_inner, same, [f"{__name__}."])
    assert "not a class at the top of its module" in str(nested["why"])
    outside = capture.tool_value(Stock("a", 1), {}, get_inner, same, ["acme."])
    assert "outside the agent's packages (acme.)" in str(outside["why"])
    picky = get_picky()
    picky.n = 2  # changed after it was made, so building it again fails
    failed = capture.tool_value(picky, {}, get_picky, same, [f"{__name__}."])
    assert failed["code"] == "unsavable"
    assert str(failed["why"]).endswith("the mock does not fit test_capture:Picky: too big")
    keys = capture.tool_value({1: "a"}, {}, get_inner, same, [])
    assert keys["why"] == "returns: a key that is not text, int 1"


def test_arguments_a_mock_cannot_hold_convert_to_none() -> None:
    assert capture.converted({"a": object()}) is None
    assert capture.converted({"a": (1, 2)}) == {"a": [1, 2]}


def rebuild(value: object) -> object:
    return {"content": [{"type": "text", "text": value}], "resultType": "complete"}


REAL = {"content": [{"type": "text", "text": "found"}], "resultType": "complete"}


@pytest.mark.design("record.mcp_round_trip")
def test_an_mcp_result_is_saved_only_when_its_mock_gives_the_same_result() -> None:
    assert capture.mcp_value(REAL, {"q": "a"}, rebuild) == {
        "value": "found",
        "args": {"q": "a"},
        "removed": [],
    }
    with_meta = {**REAL, "_meta": {"trace": "1"}}
    assert capture.mcp_value(with_meta, None, rebuild)["code"] == "unsavable"
    assert capture.mcp_value("not json", None, rebuild)["code"] == "unsavable"

    def broken(value: object) -> object:
        raise ValueError("the library refused it")

    assert "the library refused it" in str(capture.mcp_value(REAL, None, broken)["why"])
    secret = capture.mcp_value(REAL, {"token": "abc"}, rebuild)
    assert secret["code"] == "secret_changed_shape"


def test_only_values_with_a_toml_form_are_written_as_json() -> None:
    assert capture.holds_in_toml({"a": [1, "b", 2.5, True]})
    assert not capture.holds_in_toml({"a": [1, None]})
    assert not capture.holds_in_toml([2**64])
