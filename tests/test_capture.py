"""The parts of a recording that need no gate: type-strict equality, the key-name rule
and how a web body is written."""

import dataclasses
from typing import NamedTuple, cast

import pydantic
import pytest

from scrimmage import capture, mocks, state
from scrimmage.redact import REMOVED


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
        (True, 1, "returns: bool, got int"),
        (1, 1.0, "returns: int, got float"),
        ([1, 2], (1, 2), "returns: list, got tuple"),
        (
            {"items": [{"qty": 3}]},
            {"items": [{"qty": 3.0}]},
            "returns.items[0].qty: int, got float",
        ),
        ({"a": 1}, {"b": 1}, "returns: keys differ, 1 missing and 1 added"),
        ([1], [1, 2], "returns: 1 items, got 2"),
        (Stock("a", 1), Stock("a", True), "returns.count: int, got bool"),
        ("hunter2", "hunter3", "returns: the str differs"),
    ],
)
def test_equality_is_type_strict_at_every_level_and_names_the_place(
    real: object, built: object, message: str
) -> None:
    assert capture.differ(real, built, "returns") == message


@pytest.mark.design("record.no_values_in_why")
def test_a_reason_names_places_and_types_never_a_value() -> None:
    @dataclasses.dataclass
    class Inner:
        password: str

    secret = Inner("hunter2")
    assert secret.password == "hunter2"
    bad_key = Key("sk-1")
    bad_key.value = "hunter2"  # building it again fails with a message that quotes it
    reasons = [
        capture.differ({"password": "hunter2"}, {"password": "other"}, "returns"),
        capture.differ(secret, {"password": "hunter2"}, "returns"),
        capture.differ({"hunter2": 1}, {"other": 1}, "returns"),
        str(capture.tool_value({1: "hunter2"}, {}, get_inner, same, [])["why"]),
        str(capture.tool_value(bad_key, {}, get_key, same, [f"{__name__}."])["why"]),
    ]
    assert all(reason and "hunter2" not in reason for reason in reasons), reasons


@pytest.mark.design("record.strict_equal")
def test_nan_equals_nan_and_equal_values_are_equal() -> None:
    assert capture.differ(float("nan"), float("nan"), "r") is None
    assert capture.differ(Point(1, 2.0), Point(1, 2.0), "r") is None
    assert capture.differ(Order(orderId="1", total=1.0, items=[]), get_order("1"), "r")


@pytest.mark.design("record.secret_key_name")
@pytest.mark.parametrize(
    "key",
    ["password", "X-Auth-Token", "APIKey", "api_key", "private.key", "session", "Cookie",
     "client_secret", "AccessKey", "pwd2", "user credentials", "tokens", "secrets",
     "passwords", "cookies", "api_keys", "refresh_tokens", "PRIVATEKEY", "apikey",
     "session_token"],
)  # fmt: skip
def test_a_whole_word_part_names_a_secret(key: str) -> None:
    assert capture.secret_key(key)


@pytest.mark.design("record.key_name_whole_parts")
@pytest.mark.parametrize(
    "key",
    ["author", "authority", "max_tokens", "tokens_used", "sessions_total", "keys", "api",
     "private", "passport", "secretary", "monkey", "access", "status", "num_tokens"],
)  # fmt: skip
def test_a_key_that_only_holds_a_secret_word_inside_another_is_not_one(key: str) -> None:
    assert not capture.secret_key(key)


@pytest.mark.design("record.key_name_about")
@pytest.mark.parametrize(
    "key",
    ["session_id", "sessionId", "token_count", "token_type", "auth_mode", "secret_total",
     "access_key_id", "page_token", "next_page_token", "nextPageToken", "NextToken",
     "next_token", "continuation_token"],
)  # fmt: skip
def test_a_name_about_a_secret_or_a_page_cursor_is_not_a_secret(key: str) -> None:
    # A session or paging tool takes these as arguments, and any removal from arguments
    # means the call could never be recorded.
    assert not capture.secret_key(key)


@pytest.mark.design("record.secret_key_name")
def test_every_string_in_a_list_under_a_secret_key_is_removed() -> None:
    value = {"tokens": ["a", ["b", 3]], "names": ["ann"], "api_keys": "k"}
    cleaned, removed = capture.clean(value)
    assert cleaned == {"tokens": [REMOVED, [REMOVED, 3]], "names": ["ann"], "api_keys": REMOVED}
    assert removed == [{"key": "tokens", "rule": "key_name"}] * 2 + [
        {"key": "api_keys", "rule": "key_name"}
    ]


@pytest.mark.design("record.unsavable")
@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("a\udcff", "returns: a text with a lone surrogate"),
        ({"a\udcff": 1}, "returns: a key that is a text with a lone surrogate"),
        ({1: "a"}, "returns: a key that is not text, a int"),
    ],
)
def test_text_with_no_utf8_form_is_unsavable(value: object, why: str) -> None:
    with pytest.raises(capture.Unsavable, match=why):
        capture.plain(value, "returns")


@pytest.mark.design("record.unsavable")
def test_an_unsavable_reason_names_a_real_dict_key_by_position_never_by_name() -> None:
    secret = "sk-live-PROBEKEY-7f3a9c"
    reply = {"ok": 1, secret: {"deep": 2**70}}
    with pytest.raises(capture.Unsavable) as caught:
        capture.plain(reply, "returns")
    assert str(caught.value) == "returns{1}{0}: an integer beyond 64 bits, which TOML cannot hold"
    recorded = capture.tool_value(reply, {}, get_inner, same, [])
    assert recorded["code"] == "unsavable", recorded
    leaked = secret in str(caught.value) or secret in str(recorded)
    assert not leaked, "a key from the real reply was named"


@pytest.mark.design("record.unsavable")
def test_an_argument_is_named_at_the_top_and_by_position_below_it() -> None:
    secret = "sk-live-PROBEKEY-7f3a9c"
    with pytest.raises(capture.Unsavable) as caught:
        capture.plain({"query": {"ok": 1, secret: 2**70}}, "args", named=True)
    assert str(caught.value) == "args.query{1}: an integer beyond 64 bits, which TOML cannot hold"
    leaked = secret in str(caught.value)
    assert not leaked, "a key below the top of the arguments was named"


@pytest.mark.design("record.unsavable")
def test_a_value_that_holds_itself_or_nests_too_deep_is_unsavable() -> None:
    itself: list[object] = []
    itself.append(itself)
    deep: list[object] = []
    inner = deep
    for _ in range(3000):
        inner.append([])
        inner = cast(list[object], inner[0])
    for value in (itself, deep):
        with pytest.raises(capture.Unsavable, match="nested too deep, or holds itself"):
            capture.plain(value, "returns")
    assert capture.converted({"a": itself}) is None


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


@dataclasses.dataclass
class Key:
    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("sk-"):
            raise ValueError(f"{self.value} is not a key")


def get_key() -> Key:
    return Key("sk-1")


@pytest.mark.design("record.unsavable")
def test_a_value_whose_class_a_mock_cannot_name_or_build_is_unsavable() -> None:
    nested = capture.tool_value(get_inner(), {}, get_inner, same, [f"{__name__}."])
    assert "not a class at the top of its module" in str(nested["why"])
    outside = capture.tool_value(Stock("a", 1), {}, get_inner, same, ["acme."])
    assert "outside the agent's packages (acme.)" in str(outside["why"])
    picky = get_picky()
    picky.n = 2  # changed after it was made, so building it again fails
    failed = capture.tool_value(picky, {}, get_picky, same, [f"{__name__}."])
    assert failed == {
        "code": "unsavable",
        "why": "returns: a mock built from it fails with TypeError",
    }


def find_stock(sku: str) -> Stock | None:
    return None


@pytest.mark.design("record.strict_equal")
def test_an_optional_class_return_still_records_and_round_trips() -> None:
    allowed = [f"{__name__}."]
    saved = capture.tool_value(Stock("a", 1), {"sku": "a"}, find_stock, same, allowed)
    assert saved == {
        "value": {"sku": "a", "count": 1},
        "args": {"sku": "a"},
        "removed": [],
        "type": f"{__name__}:Stock",
    }
    # The mock built from the saved value, with or without its type, is the real value.
    for kind in (f"{__name__}:Stock", None):
        assert mocks.build(saved["value"], kind, find_stock, allowed) == Stock("a", 1)
    assert capture.tool_value(None, {}, find_stock, same, allowed)["value"] is None


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

    assert capture.mcp_value(REAL, None, broken)["why"] == (
        "result: a mock built from it fails with ValueError"
    )
    secret = capture.mcp_value(REAL, {"token": "abc"}, rebuild)
    assert secret["code"] == "secret_changed_shape"
    assert capture.mcp_value(REAL, {"q": "a\udcff"}, rebuild)["code"] == "unsavable"


@pytest.mark.design("record.mcp_rebuilt_after_cleaning")
def test_a_cleaned_mcp_value_that_no_longer_fits_is_secret_changed_shape() -> None:
    key = "sk-proj-a1B2c3D4e5F6g7H8i9J0"
    real = {"content": [{"type": "text", "text": key}], "resultType": "complete"}

    def strict(value: object) -> object:
        if not str(value).startswith("sk-"):
            raise ValueError("does not match the output schema")
        return rebuild(value)

    found = capture.mcp_value(real, {}, strict)
    assert found["code"] == "secret_changed_shape" and key not in str(found)
    assert capture.mcp_value(real, {}, rebuild)["removed"] == [{"key": None, "rule": "pattern"}]


@pytest.mark.parametrize("pin", [24680, 2468.0])
def test_a_number_under_a_secret_key_shows_only_its_type(pin: float) -> None:
    shown = capture.shown_args({"password": pin, "order": "1"}, ["password"])
    assert shown == f'{{"password": <{type(pin).__name__}>, "order": <str>}}'
    assert str(pin) not in (shown or "")
    assert capture.shown_args({"amount": 24680}, ["amount"]) == '{"amount": 24680}'


WINDOWS_PATH = "password=C:\\new2zz"  # a Python string: a backslash, then the letter n


def get_path() -> str:
    return WINDOWS_PATH


@pytest.mark.design("security.redaction_shapes")
def test_a_backslash_n_in_a_python_string_is_in_the_value_on_the_tool_value_path() -> None:
    """A Python string has no escapes left, so its \\n is a backslash and a letter."""
    doc = capture.tool_value(WINDOWS_PATH, {}, get_path, same, [])
    assert doc["value"] == REMOVED, doc  # what a recorded mock of the call returns
    nested = capture.tool_value({"path": [WINDOWS_PATH]}, {}, get_path, same, [])
    assert nested["value"] == {"path": [REMOVED]}, nested
    assert "2zz" not in str(state.content(WINDOWS_PATH)["preview"])  # the call line's preview


@pytest.mark.design("security.redaction_shapes")
def test_a_backslash_n_in_a_python_string_is_in_the_value_on_the_mock_path() -> None:
    """A mock's arguments as a miss shows them, and the names and values of a mock's table."""
    shown = capture.shown_args({"q": WINDOWS_PATH, "n": 1}, ["q"])
    assert shown is not None and "2zz" not in shown, shown
    shown_key = capture.shown_args({WINDOWS_PATH: "x"}, [WINDOWS_PATH])
    assert shown_key is not None and "2zz" not in shown_key, shown_key
    cleaned, removed = capture.clean({WINDOWS_PATH: 1, "k": [WINDOWS_PATH]})
    assert cleaned == {REMOVED: 1, "k": [REMOVED]} and len(removed) == 2


def test_a_value_a_gate_puts_on_a_line_is_redacted_as_a_python_string() -> None:
    assert state.scrub(WINDOWS_PATH) == REMOVED
    assert state.scrub(["x", WINDOWS_PATH]) == ["x", REMOVED]
