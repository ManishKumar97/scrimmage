"""The change an append or replace makes, as pure functions."""

import copy
import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scrimmage import changes
from scrimmage.changes import Changed, Unchangeable
from scrimmage.config import Fault

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


JSON = [("content-type", "application/json")]


@pytest.mark.parametrize(
    ("status", "op", "headers", "body", "why"),
    [
        (200, "chat", [("content-type", "text/html")], b"{}", "the reply is text/html, not a"),
        (200, "chat", [], b"{}", "the reply is of no type, not a chat completion"),
        (200, "chat", [*JSON, ("content-encoding", "zstd")], b"{}", "encoded with zstd"),
        (200, "chat", [*JSON, ("content-encoding", "gzip")], b"not gzip", "will not unpack"),
        (200, "chat", JSON, b"{oops", "the body is not JSON that parses"),
        (200, "chat", JSON, b"[]", "the reply has no choices"),
        (200, "chat", JSON, b'{"choices": [{"message": {}}]}', "the reply has no text"),
        (200, "chat", JSON, b'{"choices": [7]}', "the reply has no text"),
        (500, "chat", JSON, b"{}", "the reply status is 500, which is not changed"),
        (200, "responses", JSON, b"{}", "Chat Completions only, and this call is responses"),
    ],
)
def test_a_chat_reply_that_cannot_take_a_replace_says_why(
    status: int, op: str, headers: list[tuple[str, str]], body: bytes, why: str
) -> None:
    said = changes.change_chat(status, op, headers, body, fault("replace", "safe"))
    assert isinstance(said, Unchangeable) and why in said.why


def test_a_chat_replace_drops_the_body_headers_and_counts_its_length() -> None:
    body = json.dumps({"choices": [{"message": {"content": "unsafe"}}]}).encode()
    headers = [*JSON, ("transfer-encoding", "chunked"), ("x-request-id", "r1")]
    done = changes.change_chat(200, "chat", headers, body, fault("replace", "safe"))
    assert not isinstance(done, Unchangeable)
    assert json.loads(done.body)["choices"][0]["message"]["content"] == "safe"
    assert dict(done.headers) == {
        "content-type": "application/json",
        "x-request-id": "r1",
        "content-length": str(len(done.body)),
    }
    # A find that touches a secret is not made: the line could not show what changed.
    near = json.dumps({"choices": [{"message": {"content": f"x {KEY}"}}]}).encode()
    said = changes.change_chat(200, "chat", JSON, near, fault("replace", "y", find=KEY[:6]))
    assert isinstance(said, Unchangeable) and said.mistake


def test_a_replace_in_a_json_text_inside_data_is_judged_per_string() -> None:
    """The URL's query is removed by redaction, but an edit to the msg string touches none of
    it. The whole JSON text is not one removed span."""
    body = json.dumps({"msg": "hello", "next": "https://api.example.com/items?page=2"})
    done = changed(changes.change_data({"body": body}, fault("replace", "HI", "hello")))
    assert isinstance(done.value, dict)
    assert json.loads(done.value["body"]) == {
        "msg": "HI",
        "next": "https://api.example.com/items?page=2",
    }


def test_a_replace_that_touches_a_secret_in_a_json_text_inside_data_is_refused() -> None:
    body = json.dumps({"msg": "hello", "key": KEY})
    clash = left(changes.change_data({"body": body}, fault("replace", "y", KEY[4:9])))
    assert (clash.kind, clash.rule) == ("span", "json")
    # a string that holds a removed span is one place, so an edit beside the secret in it is
    # refused too
    body = json.dumps({"msg": f"hello {KEY}"})
    clash = left(changes.change_data({"body": body}, fault("replace", "y", "hello")))
    assert clash.kind == "span"


# The faults in a model's reply, on real gpt-4o-mini replies.
FIXTURES = Path(__file__).parent / "fixtures" / "openai"
REAL_ANSWER_FILE = FIXTURES / "real_chat_answer.json"
REAL_TOOL_CALL_FILE = FIXTURES / "real_chat_tool_call.json"
REAL_ANSWER = json.loads(REAL_ANSWER_FILE.read_text())
REAL_TOOL_CALL = json.loads(REAL_TOOL_CALL_FILE.read_text())
THAI = "สวัสดี"  # the token of Anthropic's postmortem
REAL_HEADERS = [*JSON, ("x-request-id", "req_real"), ("content-length", "999")]


def reply_fault(word: str) -> Fault:
    return Fault("model", word, "always", model="gpt-4o-mini")


def reply_with(content: object, **message: object) -> dict[str, Any]:
    """The real answer with this content in its one choice, and these message fields."""
    reply = copy.deepcopy(REAL_ANSWER)
    reply["choices"][0]["message"] = {**reply["choices"][0]["message"], "content": content}
    reply["choices"][0]["message"].update(message)
    return reply


def change(
    word: str, reply: object, status: int = 200, op: str = "chat", headers: Any = None
) -> changes.WebChanged | Unchangeable:
    body = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
    held = REAL_HEADERS if headers is None else headers
    return changes.CHAT_CHANGES[word](status, op, held, body, reply_fault(word))


def made(word: str, reply: object) -> tuple[dict[str, Any], changes.WebChanged]:
    """The change of this word on a 200 chat reply: the new reply, parsed, and the change."""
    done = change(word, reply)
    assert isinstance(done, changes.WebChanged), done
    return json.loads(done.body), done


def not_made(word: str, reply: object) -> Unchangeable:
    said = change(word, reply)
    assert isinstance(said, Unchangeable), said
    return said


def text_of(reply: dict[str, Any], choice: int = 0) -> str:
    return reply["choices"][choice]["message"]["content"]


@pytest.mark.design("fault.garble")
def test_garble_puts_the_incident_token_in_every_fifth_word() -> None:
    said = "Your order 1234 shipped on Monday and arrives Friday"
    new, done = made("garble", reply_with(said))
    assert text_of(new) == f"Your order 1234 shipped {THAI} Monday and arrives Friday"
    assert (done.count, done.parts) == (1, ("choices[0].message.content",))
    # The 5th, 10th and 15th words; the spaces and line breaks between words stay.
    words = " ".join(f"w{i}" for i in range(1, 11)) + "\n\nw11 w12 w13 w14  w15 w16"
    new, done = made("garble", reply_with(words))
    assert text_of(new) == f"w1 w2 w3 w4 {THAI} w6 w7 w8 w9 {THAI}\n\nw11 w12 w13 w14  {THAI} w16"
    assert done.count == 3
    # Every choice with text is garbled; one with none keeps its place.
    two = reply_with("a b c d e")
    two["choices"].append({**copy.deepcopy(two["choices"][0]), "index": 1})
    two["choices"].insert(1, {"index": 2, "message": {"content": None, "refusal": "no"}})
    new, done = made("garble", two)
    assert [c["message"]["content"] for c in new["choices"]] == [
        f"a b c d {THAI}",
        None,
        f"a b c d {THAI}",
    ]
    assert done.parts == ("choices[0].message.content", "choices[2].message.content")


@pytest.mark.design("fault.garble")
def test_garble_changes_the_last_word_of_a_short_reply() -> None:
    new, done = made("garble", reply_with("Hello! How are you?"))
    assert (text_of(new), done.count) == (f"Hello! How are {THAI}", 1)
    assert text_of(made("garble", reply_with("Done."))[0]) == THAI
    # Text with no word, a refusal and tool calls alone have nothing to garble.
    for reply, why in (
        (reply_with("  \n"), "the reply has no text"),
        (reply_with(None, refusal="I can't."), "the reply is a refusal with no text"),
        (REAL_TOOL_CALL, "the reply has only tool calls and no text"),
    ):
        said = not_made("garble", reply)
        assert (said.why, said.kind) == (why, "data")


@pytest.mark.design("fault.truncate")
def test_truncate_cuts_at_a_space_and_sets_length() -> None:
    said = text_of(REAL_ANSWER)  # "Order 1234 has been shipped and is expected to arrive..."
    new, done = made("truncate", REAL_ANSWER)
    assert text_of(new) == said[: said.rfind(" ", 0, len(said) // 2 + 1)]
    assert text_of(new) == "Order 1234 has been shipped and is"
    assert new["choices"][0]["finish_reason"] == "length"
    assert (done.count, done.parts, done.status) == (1, ("choices[0].message.content",), None)
    # A space right at the middle is cut at, and goes with the cut, as a real one does; with
    # no space, the first half is kept, never less than one character.
    for text, kept in (("abc def", "abc"), ("abcdefgh", "abcd"), ("a", "a"), (" leading", " lea")):
        assert text_of(made("truncate", reply_with(text))[0]) == kept, text
    # A choice cut in its text never reached its tool calls, so they go with the cut.
    mixed = reply_with("Let me look that order up for you now.")
    calls = REAL_TOOL_CALL["choices"][0]["message"]["tool_calls"]
    mixed["choices"][0]["message"]["tool_calls"] = calls
    new, _ = made("truncate", mixed)
    assert "tool_calls" not in new["choices"][0]["message"]
    assert new["choices"][0]["finish_reason"] == "length"


@pytest.mark.design("fault.truncate_tool_call")
def test_truncate_on_tool_calls_only_sends_the_real_400() -> None:
    """A real limit inside a tool call gives no cut-off call: OpenAI answers 400, as saved on
    18 November 2026. The headers but the length stay the real reply's."""
    real = json.loads((FIXTURES / "real_chat_tool_call_truncated.json").read_text())
    new, done = made("truncate", REAL_TOOL_CALL)
    assert (done.status, new) == (real["status"], real["body"])
    assert dict(done.headers) == {
        "content-type": "application/json",
        "x-request-id": "req_real",
        "content-length": str(len(done.body)),
    }
    assert (done.count, done.parts) == (1, ("error",))
    # With several choices, one of tool calls alone makes the whole reply that 400: a real
    # limit inside it fails the request, whatever the other choices hold.
    several = copy.deepcopy(REAL_TOOL_CALL)
    several["choices"] += [
        {**copy.deepcopy(REAL_ANSWER["choices"][0]), "index": 1},
    ]
    new, done = made("truncate", several)
    assert (done.status, new) == (400, real["body"])
    # The tool-calls-only choice need not be the first: any choice makes the 400.
    text_first = copy.deepcopy(REAL_ANSWER)
    text_first["choices"].append({**copy.deepcopy(REAL_TOOL_CALL["choices"][0]), "index": 1})
    new, done = made("truncate", text_first)
    assert (done.status, new) == (400, real["body"])
    # A reply with neither text nor a tool call is left as it is.
    said = not_made("truncate", reply_with(None))
    assert (said.why, said.kind) == ("the reply has no text and no tool call", "data")


@pytest.mark.design("fault.bad_tool_args")
def test_bad_tool_args_breaks_every_tool_call() -> None:
    reply = copy.deepcopy(REAL_TOOL_CALL)
    [call] = reply["choices"][0]["message"]["tool_calls"]
    second = {**copy.deepcopy(call), "id": "call_2"}
    second["function"]["arguments"] = '{"sku": {"id": "SKU-42"}} '
    reply["choices"][0]["message"]["tool_calls"].append(second)
    reply["choices"].append({**copy.deepcopy(reply["choices"][0]), "index": 1})
    new, done = made("bad tool args", reply)
    broken = [
        [c["function"]["arguments"] for c in choice["message"]["tool_calls"]]
        for choice in new["choices"]
    ]
    assert broken == [['{"order_id":"1234"', '{"sku": {"id": "SKU-42"} ']] * 2
    for arguments in (a for choice in broken for a in choice):
        with pytest.raises(json.JSONDecodeError):
            json.loads(arguments)
    assert done.count == 4
    assert done.parts[:2] == (
        "choices[0].message.tool_calls[0].function.arguments",
        "choices[0].message.tool_calls[1].function.arguments",
    )
    # The names and ids stay; a reply with no tool call is left as it is.
    assert new["choices"][0]["message"]["tool_calls"][1]["id"] == "call_2"
    assert new["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_order"
    said = not_made("bad tool args", REAL_ANSWER)
    assert (said.why, said.kind) == ("the reply has no tool call with arguments to break", "data")


@pytest.mark.design("fault.reply_keeps_the_rest")
@pytest.mark.parametrize(
    ("word", "source"),
    [
        ("garble", REAL_ANSWER_FILE),
        ("truncate", REAL_ANSWER_FILE),
        ("bad tool args", REAL_TOOL_CALL_FILE),
    ],
)
def test_each_change_keeps_id_model_usage_and_fingerprint(word: str, source: Path) -> None:
    """usage stays what the model was billed for, so the line's cost is the real one."""
    reply = json.loads(source.read_text())
    new, done = made(word, reply)
    kept = ("id", "object", "created", "model", "usage", "service_tier", "system_fingerprint")
    assert {k: new[k] for k in kept} == {k: reply[k] for k in kept}
    assert list(new) == list(reply)  # no field added or dropped, in the real order
    # The new body has its own length and no encoding; the other headers are the real ones.
    assert dict(done.headers)["content-length"] == str(len(done.body))
    assert dict(done.headers)["x-request-id"] == "req_real"
    assert reply == json.loads(source.read_text())  # the real reply is not changed in place


@pytest.mark.design("fault.reply_keeps_the_rest")
@pytest.mark.parametrize("word", ["garble", "truncate", "bad tool args"])
def test_a_reply_change_reads_a_gzip_body_and_writes_it_plain(word: str) -> None:
    reply = REAL_TOOL_CALL if word == "bad tool args" else REAL_ANSWER
    packed = gzip.compress(json.dumps(reply).encode())
    headers = [*JSON, ("content-encoding", "gzip"), ("content-length", str(len(packed)))]
    done = change(word, packed, headers=headers)
    assert isinstance(done, changes.WebChanged)
    assert "content-encoding" not in dict(done.headers)
    assert json.loads(done.body)["usage"] == reply["usage"]
    # A stream, a status other than 200 and a Responses reply are not changed.
    for status, op, kind in (
        (200, "chat", "text/event-stream"),
        (500, "chat", "application/json"),
        (200, "responses", "application/json"),
    ):
        said = change(word, packed, status, op, [("content-type", kind)])
        assert isinstance(said, Unchangeable)
        assert said.why.startswith((f"{word} on a model call", "the reply status is 500"))


# The Responses API: a real gpt-4o-mini answer, and function_call items in the spec's form.
REAL_RESPONSE_FILE = FIXTURES / "real_response_answer.json"
REAL_RESPONSE = json.loads(REAL_RESPONSE_FILE.read_text())["body"]
KEPT_ON_RESPONSES = ("object", "created_at", "model", "usage", "service_tier", "billing")


def response_with(*items: dict[str, Any]) -> dict[str, Any]:
    """The real Responses answer with these output items."""
    reply = copy.deepcopy(REAL_RESPONSE)
    reply["output"] = [copy.deepcopy(item) for item in items]
    return reply


def message(*texts: str) -> dict[str, Any]:
    parts = [{"type": "output_text", "annotations": [], "logprobs": [], "text": t} for t in texts]
    return {"id": "msg_1", "type": "message", "status": "completed", "content": parts,
            "role": "assistant"}  # fmt: skip


def function_call(arguments: str, n: int = 1) -> dict[str, Any]:
    return {
        "id": f"fc_{n}",
        "type": "function_call",
        "status": "completed",
        "arguments": arguments,
        "call_id": f"call_{n}",
        "name": "get_order",
    }


def on_responses(word: str, reply: object) -> changes.WebChanged | Unchangeable:
    change_of = changes.model_change(word, "responses")
    body = json.dumps(reply).encode()
    return change_of(200, "responses", REAL_HEADERS, body, reply_fault(word))


def made_on_responses(word: str, reply: object) -> tuple[dict[str, Any], changes.WebChanged]:
    done = on_responses(word, reply)
    assert isinstance(done, changes.WebChanged), done
    return json.loads(done.body), done


def not_made_on_responses(word: str, reply: object) -> tuple[str, str]:
    said = on_responses(word, reply)
    assert isinstance(said, Unchangeable), said
    return said.why, said.kind


def kept(reply: dict[str, Any]) -> dict[str, Any]:
    return {k: reply[k] for k in KEPT_ON_RESPONSES}


@pytest.mark.design("fault.truncate_responses")
def test_truncate_on_responses_sets_incomplete_and_max_output_tokens() -> None:
    """As a real reply cut by max_output_tokens is: the reply and its item incomplete, the
    reason max_output_tokens, completed_at null, and usage as it was billed."""
    real = json.loads((FIXTURES / "real_response_incomplete.json").read_text())["body"]
    new, done = made_on_responses("truncate", REAL_RESPONSE)
    assert new["output"][0]["content"][0]["text"] == "Order 1234 has"
    for key in ("status", "incomplete_details", "completed_at"):
        assert new[key] == real[key], key
    assert new["output"][0]["status"] == real["output"][0]["status"] == "incomplete"
    assert kept(new) == kept(REAL_RESPONSE)
    assert list(new) == list(REAL_RESPONSE)  # no field added or dropped, in the real order
    assert (done.count, done.parts, done.status) == (1, ("output[0].content[0].text",), None)
    # The first text is cut, and what came after it was never made, so it goes.
    both = response_with(
        message("Let me look that order up.", "Second part."), function_call('{"a":"1"}')
    )
    new, _ = made_on_responses("truncate", both)
    assert [item["type"] for item in new["output"]] == ["message"]
    assert [part["text"] for part in new["output"][0]["content"]] == ["Let me look"]
    # With no text, the last function call's arguments are cut, and that item is incomplete.
    calls = response_with(function_call('{"order_id":"1234"}'), function_call('{"x":"5678"}', 2))
    new, done = made_on_responses("truncate", calls)
    assert [item["arguments"] for item in new["output"]] == ['{"order_id":"1234"}', '{"x":"']
    assert [item["status"] for item in new["output"]] == ["completed", "incomplete"]
    assert (new["status"], done.parts) == ("incomplete", ("output[1].arguments",))
    # A reply with neither, such as a refusal, is left as it is.
    refusal = response_with({**message(), "content": [{"type": "refusal", "refusal": "No."}]})
    assert not_made_on_responses("truncate", refusal) == (
        "the reply is a refusal with no text",
        "data",
    )
    assert not_made_on_responses("truncate", response_with()) == (
        "the reply has no text and no tool call",
        "data",
    )
    assert json.loads(REAL_RESPONSE_FILE.read_text())["body"] == REAL_RESPONSE  # not in place


@pytest.mark.design("fault.bad_tool_args_responses")
def test_bad_tool_args_on_responses_breaks_each_function_call_item() -> None:
    reply = response_with(
        message("Looking."),
        function_call('{"order_id":"1234"}'),
        function_call('{"sku": {"id": "SKU-42"}} ', 2),
    )
    new, done = made_on_responses("bad tool args", reply)
    broken = [item["arguments"] for item in new["output"][1:]]
    assert broken == ['{"order_id":"1234"', '{"sku": {"id": "SKU-42"} ']
    for arguments in broken:
        with pytest.raises(json.JSONDecodeError):
            json.loads(arguments)
    assert (done.count, done.parts) == (2, ("output[1].arguments", "output[2].arguments"))
    # The names, call ids and statuses stay, and so does the text.
    assert [(i["call_id"], i["name"], i["status"]) for i in new["output"][1:]] == [
        ("call_1", "get_order", "completed"),
        ("call_2", "get_order", "completed"),
    ]
    assert new["output"][0]["content"] == reply["output"][0]["content"]
    assert new["status"] == "completed"
    assert kept(new) == kept(reply)
    assert not_made_on_responses("bad tool args", REAL_RESPONSE) == (
        "the reply has no tool call with arguments to break",
        "data",
    )


@pytest.mark.design("fault.garble_responses")
def test_garble_on_responses_changes_each_output_text_part() -> None:
    reply = response_with(
        message("Your order 1234 shipped on Monday", "Done."),
        function_call('{"order_id":"1234"}'),
        {**message("a b c d e"), "id": "msg_2"},
    )
    new, done = made_on_responses("garble", reply)
    texts = [[p["text"] for p in i["content"]] for i in new["output"] if i["type"] == "message"]
    assert texts == [[f"Your order 1234 shipped {THAI} Monday", THAI], [f"a b c d {THAI}"]]
    assert done.count == 3
    assert done.parts == (
        "output[0].content[0].text",
        "output[0].content[1].text",
        "output[2].content[0].text",
    )
    assert {**new["output"][1], "id": ""} == {**reply["output"][1], "id": ""}  # it stays
    new, _ = made_on_responses("garble", REAL_RESPONSE)
    assert new["output"][0]["content"][0]["text"] == f"Order 1234 has been {THAI}"
    assert kept(new) == kept(REAL_RESPONSE) and new["status"] == "completed"
    for items, why in (
        ((function_call("{}"),), "the reply has only tool calls and no text"),
        ((message("  "),), "the reply has no text"),
    ):
        assert not_made_on_responses("garble", response_with(*items)) == (why, "data")


ID = r"[0-9a-f]{50}"


@pytest.mark.design("fault.responses_fresh_ids")
@pytest.mark.parametrize("word", ["garble", "truncate", "bad tool args"])
def test_a_changed_response_gets_fresh_ids_in_openai_forms(word: str) -> None:
    """OpenAI reads an item resent under a stored id as it stored it, so the changed reply and
    its items get new made-up ids, which OpenAI reads as sent. call_id stays: the agent pairs
    its function_call_output by it."""
    call = {**function_call('{"order_id":"1234"}'), "id": "fc_" + "a" * 50}
    reply = response_with(REAL_RESPONSE["output"][0], call)
    if word != "bad tool args":
        reply["output"] = reply["output"][:1]
    new, _ = made_on_responses(word, reply)
    again, _ = made_on_responses(word, reply)
    assert re.fullmatch(f"resp_{ID}", new["id"]) and new["id"] != REAL_RESPONSE["id"]
    assert new["id"] != again["id"]
    for got, real in zip(new["output"], reply["output"], strict=True):
        prefix = {"message": "msg_", "function_call": "fc_"}[real["type"]]
        assert re.fullmatch(prefix + ID, got["id"]) and got["id"] != real["id"]
        assert got.get("call_id") == real.get("call_id")
