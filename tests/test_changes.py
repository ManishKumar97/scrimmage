"""The change an append or replace makes, as pure functions."""

import json
from dataclasses import dataclass

import pytest

from scrimmage_core import changes
from scrimmage_core.changes import Changed, Unchangeable
from scrimmage_core.config import Fault

KEY = "ghp_" + "a" * 36


def fault(do: str, text: str = "X", find: str | None = None) -> Fault:
    return Fault("tool:t", do, "always", text=text, find=find)


def changed(result: changes.Result) -> Changed:
    assert isinstance(result, Changed), result
    return result


def left(result: changes.Result) -> Unchangeable:
    assert isinstance(result, Unchangeable), result
    return result


def test_append_adds_text_at_the_end_exactly() -> None:
    done = changed(changes.change_text("SKU-42: 12 in stock", fault("append", "\n\n  note ")))
    assert done.value == "SKU-42: 12 in stock\n\n  note "
    assert (done.count, done.parts) == (1, ("result",))


def test_replace_with_find_changes_every_occurrence_and_counts_them() -> None:
    done = changed(changes.change_text("a-b-a-b-a", fault("replace", "<>", "a")))
    assert (done.value, done.count) == ("<>-b-<>-b-<>", 3)
    # Compared exactly, case and all.
    assert isinstance(changes.change_text("A", fault("replace", "x", "a")), Unchangeable)
    # Occurrences do not overlap, as in str.replace.
    assert changed(changes.change_text("aaa", fault("replace", "b", "aa"))).value == "ba"


def test_replace_without_find_replaces_the_whole_text() -> None:
    done = changed(changes.change_text("deny", fault("replace", "allow")))
    assert (done.value, done.count) == ("allow", 1)
    assert changed(changes.change_text("", fault("replace", "allow"))).value == "allow"


def test_a_find_not_in_the_text_is_not_exercised_and_not_a_mistake() -> None:
    result = left(changes.change_text("12 in stock", fault("replace", "x", "Arrives")))
    assert (result.kind, result.mistake) == ("data", False)
    assert result.message(fault("replace"), "tool:t") is None


def test_replace_in_data_changes_values_never_keys() -> None:
    value = {"find me": "find me", "n": 1, "list": ["x find me", {"find me": "find me!"}]}
    done = changed(changes.change_data(value, fault("replace", "OK", "find me")))
    assert done.value == {"find me": "OK", "n": 1, "list": ["x OK", {"find me": "OK!"}]}
    assert done.count == 3


def test_replace_in_data_returns_a_copy_and_keeps_each_container_type() -> None:
    value = {"a": ("find", ["find", ("find",)]), "b": {"c": "find"}}
    before = json.dumps(value)
    done = changed(changes.change_data(value, fault("replace", "OK", "find")))
    assert json.dumps(value) == before  # the tool's own object is not changed
    assert done.value == {"a": ("OK", ["OK", ("OK",)]), "b": {"c": "OK"}}
    new = done.value
    assert isinstance(new, dict)
    assert type(new["a"]) is tuple and type(new["a"][1]) is list and type(new["a"][1][1]) is tuple
    assert new is not value and new["b"] is not value["b"]


def test_count_and_parts_name_each_place_changed() -> None:
    value = {"order": {"status": "late late"}, "items": [{"note": "late"}, {"note": "ok"}]}
    done = changed(changes.change_data(value, fault("replace", "on time", "late")))
    assert done.count == 3
    assert done.parts == ("$.order.status", "$.items[0].note")
    many = {f"k{i}": "late" for i in range(9)}
    capped = changed(changes.change_data(many, fault("replace", "x", "late")))
    assert (capped.count, len(capped.parts)) == (9, 5)
    root = changed(
        changes.change_data({"a": "late"}, fault("replace", "x", "late"), "structuredContent")
    )
    assert root.parts == ("structuredContent.a",)


@pytest.mark.parametrize("do", ["append", "replace"])
def test_append_on_data_cannot_change_it(do: str) -> None:
    result = left(changes.change_data({"status": "late"}, fault(do)))
    assert result.kind == "shape" and "use replace with find" in result.why
    assert isinstance(changes.change({"status": "x"}, fault(do)), Unchangeable)
    assert isinstance(changes.change(["x"], fault(do)), Unchangeable)


@dataclass
class Order:
    status: str


class Text(str):
    pass


@pytest.mark.parametrize(
    "value",
    [Order("late"), 7, 1.5, True, b"bytes", None, {"a": Order("x")}, {"a": {1, 2}}, {"a": b"x"}],
)
def test_a_value_that_is_not_text_or_plain_data_cannot_be_changed(value: object) -> None:
    for do, find in (("append", None), ("replace", None), ("replace", "x")):
        result = left(changes.change(value, fault(do, "X", find)))
        assert (result.kind, result.mistake) == ("shape", False)


def test_a_declared_type_with_no_text_is_a_mistake_at_once() -> None:
    result = left(changes.change("fine", fault("append"), Order))
    assert (result.kind, result.mistake) == ("declared", True)
    message = result.message(fault("append"), "tool:get_stock")
    assert message is not None
    assert message.startswith("tool:get_stock is declared to return Order, not text, so append")
    assert "fault the web request the tool makes" in message
    for declared in (int, bytes, type(None), dict[str, str]):
        assert left(changes.change("fine", fault("append"), declared)).mistake, declared
    # dict[str, str] can take a replace with find, and str can take both.
    assert changed(changes.change({"a": "x"}, fault("replace", "y", "x"), dict[str, str]))
    assert left(changes.change({"a": "x"}, fault("replace", "y"), dict[str, str])).mistake
    for declared in (str, str | None, "Any", None):
        assert not isinstance(
            changes.change("fine", fault("append"), declared or None), Unchangeable
        )
    assert changes.can_take(object, fault("append")) is None
    assert changes.can_take(Order | int, fault("append")) is False
    assert changes.can_take(Order | str, fault("append")) is True


def test_a_declared_optional_text_returning_none_is_only_not_exercised() -> None:
    result = left(changes.change(None, fault("append"), str | None))
    assert (result.why, result.kind, result.mistake) == ("the tool returned None", "shape", False)
    assert result.message(fault("append"), "tool:t") is None


def test_a_str_subclass_cannot_be_changed() -> None:
    result = left(changes.change(Text("abc"), fault("append")))
    assert "Text" in result.why
    assert isinstance(
        changes.change({"a": Text("abc")}, fault("replace", "x", "abc")), Unchangeable
    )


def test_data_that_holds_itself_cannot_be_changed_and_does_not_crash() -> None:
    loop: dict[str, object] = {"a": "find"}
    loop["self"] = loop
    assert "holds itself" in left(changes.change_data(loop, fault("replace", "x", "find"))).why
    cycle: list[object] = []
    cycle.append(cycle)
    assert isinstance(changes.change(cycle, fault("replace", "x", "find")), Unchangeable)
    deep: object = "find"
    for _ in range(5000):
        deep = [deep]
    assert "deeper than" in left(changes.change(deep, fault("replace", "x", "find"))).why
    # The same object twice is not a cycle.
    shared = ["find"]
    done = changed(changes.change_data({"a": shared, "b": shared}, fault("replace", "x", "find")))
    assert done.count == 2


def test_text_that_parses_as_a_json_object_is_changed_the_json_way() -> None:
    text = '{"status": "late", "note": "say \\"late\\""}'
    done = changed(changes.change_text(text, fault("replace", 'a "quoted" one', "late")))
    assert json.loads(str(done.value)) == {
        "status": 'a "quoted" one',
        "note": 'say "a "quoted" one"',
    }
    assert ":" in str(done.value) and "\n" not in str(done.value)  # compact, as it was one line
    assert done.parts == ("result.status", "result.note")
    pretty = json.dumps({"s": "late"}, indent=2)
    assert "\n" in str(changed(changes.change_text(pretty, fault("replace", "x", "late"))).value)
    # Append would break the JSON; a whole replace does not need it to be JSON.
    assert "JSON" in left(changes.change_text(text, fault("append"))).why
    assert changed(changes.change_text(text, fault("replace", "plain"))).value == "plain"
    # A number or a string that parses as JSON is still text.
    assert changed(changes.change_text("123", fault("append", "4"))).value == "1234"
    assert changed(changes.change_text("[not json", fault("append", "!"))).value == "[not json!"


def test_json_with_nan_is_not_exercised() -> None:
    result = left(changes.change_text('{"a": "x", "b": NaN}', fault("replace", "y", "x")))
    assert (result.kind, result.mistake) == ("data", False)
    assert "NaN" in result.why


def test_a_change_over_a_redacted_span_is_not_made_and_names_the_rule() -> None:
    text = f"token {KEY} end"
    result = left(changes.change_text(text, fault("replace", "X", KEY[:10])))
    assert (result.kind, result.rule, result.mistake) == ("span", "github-token", True)
    message = result.message(fault("replace", "X", "ghp_"), "tool:get_user")
    assert message == (
        "replace on tool:get_user would change text the github-token rule removes from the "
        "record, so the line could not show what changed; choose a find away from it"
    )
    # A find away from the secret is made.
    assert changed(changes.change_text(text, fault("replace", "TOKEN", "token"))).value == (
        f"TOKEN {KEY} end"
    )
    # Inside structured data, per string value.
    clash = left(changes.change_data({"a": f"{KEY}"}, fault("replace", "y", KEY[4:9])))
    assert clash.kind == "span"
    # A whole replace keeps nothing of the real text, so it may replace a secret.
    assert changed(changes.change_text(KEY, fault("replace", "allow"))).value == "allow"


def test_an_append_whose_joint_makes_a_new_secret_span_is_not_made() -> None:
    real = "key: sk-" + "a" * 10
    result = left(changes.change_text(real, fault("append", "b" * 10)))
    assert (result.kind, result.rule) == ("span", "openai-key")
    # An append that ends a key which already is one, and does not change its span, is made.
    plain = changed(changes.change_text("hello", fault("append", " world")))
    assert plain.value == "hello world"
    # An append at the end of a real secret is a clash: it extends the span.
    assert left(changes.change_text(KEY, fault("append", "zzz"))).kind == "span"
    # An injected text that holds a key is allowed: it is shown as written, not redacted.
    assert changed(changes.change_text("ok", fault("append", f" {KEY}"))).count == 1
    # A secret in the real text away from the joint does not matter.
    done = changed(changes.change_text(f"{KEY} done", fault("append", "!")))
    assert str(done.value).endswith("done!")


def test_a_small_gzip_bomb_is_not_unpacked_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    import gzip

    monkeypatch.setattr(changes, "WEB_CAP", 1000)
    bomb = gzip.compress(b"x" * 5_000_000)
    assert len(bomb) < 10_000
    headers = [("content-type", "text/plain"), ("content-encoding", "gzip")]
    result = changes.change_web(200, "GET", headers, bomb, fault("append"))
    assert isinstance(result, Unchangeable) and result.why == "the body is over 10 MB"
    assert len(changes.unpacked(bomb, "gzip") or b"") == 1001
    # A body inside the cap still unpacks.
    ok = changes.change_web(200, "GET", headers, gzip.compress(b"hello"), fault("append", "!"))
    assert isinstance(ok, changes.WebChanged) and ok.body == b"hello!"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_a_cut_off_packed_body_is_never_changed_as_if_whole(encoding: str) -> None:
    import zlib

    text = b"order 42 shipped on monday and will arrive friday afternoon"
    packer = zlib.compressobj(wbits=16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    whole = packer.compress(text) + packer.flush()
    cut = whole[:-12]
    assert changes.unpacked(whole, encoding) == text
    assert changes.unpacked(cut, encoding) is None
    headers = [("content-type", "text/plain"), ("content-encoding", encoding)]
    result = changes.change_web(200, "GET", headers, cut, fault("append", " IGNORE"))
    assert isinstance(result, Unchangeable) and result.why == "the body will not unpack"


@pytest.mark.parametrize("tail", [b"", b"trailing bytes"], ids=["second-member", "trailing-bytes"])
def test_a_body_with_more_after_its_first_gzip_member_is_never_changed(tail: bytes) -> None:
    import gzip

    body = gzip.compress(b"part one ") + (tail or gzip.compress(b"part two"))
    assert changes.unpacked(body, "gzip") is None
    headers = [("content-type", "text/plain"), ("content-encoding", "gzip")]
    result = changes.change_web(200, "GET", headers, body, fault("append", "!"))
    assert isinstance(result, Unchangeable) and result.why == "the body will not unpack"
