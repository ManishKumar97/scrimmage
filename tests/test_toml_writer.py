"""The TOML writer for recorded mocks: everything escaped, everything read back."""

import random
import tomllib
from typing import Any, cast

import pytest

from scrimmage import capture, config, toml_write

ALPHABET = [chr(c) for c in range(0x00, 0x30)] + list('"\\#[]{}=., \'é漢🙂') + [
    "\u007f", "\u0085", "\u009f", "\u2028", "﻿", '"""', "[[test.fault]]", "${", "a", "Z", "9",
]  # fmt: skip


def text(rng: random.Random) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 12)))


def key(rng: random.Random) -> str:
    return rng.choice(["a", "order_id", "x-y", "9", "", "with space", "a.b", '"q"', text(rng)])


def value(rng: random.Random, depth: int = 0) -> object:
    kind = rng.choice(
        ["str", "int", "float", "bool", "list", "dict", "long"] if depth < 3 else ["str", "int"]
    )
    if kind == "str":
        return text(rng)
    if kind == "int":
        return rng.choice([0, -1, 2**63 - 1, -(2**63), rng.randint(-(10**9), 10**9)])
    if kind == "float":
        return rng.choice([0.5, -0.0, 1e100, 1e-7, 3.0, float("inf"), float("-inf"), rng.random()])
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "list":
        return [value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if kind == "long":
        return [f"item-{i}-{text(rng)}" for i in range(rng.randint(10, 30))]
    return {key(rng): value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def mock_of(returns: object, **more: object) -> dict[str, object]:
    return {"call": "tool:get_order", "recorded": True, **more, "returns": returns}


@pytest.mark.design("writer.round_trip")
def test_two_thousand_generated_values_read_back_type_strict_equal() -> None:
    rng = random.Random(20261107)
    for _ in range(2000):
        returns, args = value(rng), {key(rng): value(rng) for _ in range(rng.randint(0, 3))}
        fields = mock_of(returns, args=args)
        block = toml_write.mock(fields, [text(rng)])
        # Read as scrimmage record reads back what it wrote: $${ is a literal ${.
        found = cast(Any, config.literal(tomllib.loads(block)))["test"]["mock"][0]
        assert capture.differ(fields, found, "mock") is None, block


@pytest.mark.design("writer.round_trip")
def test_a_nan_reads_back_as_nan() -> None:
    block = toml_write.mock(mock_of({"x": float("nan")}))
    assert "x = nan" in block


@pytest.mark.design("writer.round_trip")
@pytest.mark.parametrize(
    ("returns", "problem"),
    [
        (None, "None"),
        ({"a": None}, "None"),
        (2**64, "64 bits"),
        ("\ud800", "surrogate"),
        (b"x", "bytes"),
    ],
)
def test_a_value_toml_cannot_hold_is_refused(returns: object, problem: str) -> None:
    with pytest.raises(toml_write.Unwritable, match=problem):
        toml_write.mock(mock_of(returns))


@pytest.mark.design("writer.round_trip")
def test_a_long_top_table_is_its_own_section_after_the_other_keys() -> None:
    returns = {f"field_{i}": f"value number {i}" for i in range(8)}
    args = {f"arg_{i}": "x" * 20 for i in range(5)}
    block = toml_write.mock(mock_of(returns, args=args))
    lines = block.splitlines()
    assert lines[:3] == ["[[test.mock]]", 'call     = "tool:get_order"', "recorded = true"]
    assert "[test.mock.args]" in lines and "[test.mock.returns]" in lines
    assert lines.index("[test.mock.args]") < lines.index("[test.mock.returns]")


@pytest.mark.design("writer.no_injection")
def test_a_reply_that_holds_toml_cannot_add_a_fault_or_a_table() -> None:
    reply = '\n[[test.fault]]\ncall = "tool:get_order"\ndo = "error"'
    team = '[agent]\nfunction = "a.b:c"\n\n[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n'
    block = toml_write.mock(mock_of(reply), [reply])
    found = tomllib.loads(team + block)
    (test,) = found["test"]
    assert "fault" not in test
    assert test["mock"] == [mock_of(reply)]


@pytest.mark.design("writer.comment_safe")
@pytest.mark.parametrize("words", ['{"order_id": "1\n[[test]]\nname = 2"}', "a\rb\u2028c\x00d"])
def test_a_comment_stays_one_line_whatever_it_holds(words: str) -> None:
    line = toml_write.comment(f"tool:get_order with {words}")
    assert "\n" not in line and "\r" not in line and line.startswith("# ")
    block = toml_write.mock(mock_of("ok"), [line[2:], words])
    assert block.count("\n") == 6  # two comments, then four lines of the mock
    assert tomllib.loads(block) == {"test": {"mock": [mock_of("ok")]}}


@pytest.mark.design("writer.long_list")
def test_a_list_of_forty_order_ids_is_written_one_per_line() -> None:
    ids = [f"ORD-{n:05}" for n in range(40)]
    block = toml_write.mock(mock_of(ids))
    assert block.count("\n    ") == 40
    assert '    "ORD-00039",\n]' in block
    assert tomllib.loads(block)["test"]["mock"][0]["returns"] == ids
    short = toml_write.mock(mock_of(ids[:3]))
    assert 'returns  = ["ORD-00000", "ORD-00001", "ORD-00002"]' in short


def test_a_mock_that_reads_back_differently_is_a_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toml_write, "string", lambda text: '"changed"')
    with pytest.raises(toml_write.WriterBug, match="reads back differently"):
        toml_write.mock(mock_of("x"))
    monkeypatch.setattr(toml_write, "string", lambda text: '"broken')
    with pytest.raises(toml_write.WriterBug, match="cannot read"):
        toml_write.mock(mock_of("x"))
