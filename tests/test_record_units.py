"""scrimmage record's choices that need no run: statuses, the file layout, the model rule."""

import tomllib
from pathlib import Path

import pytest

from scrimmage_core import config as settings
from scrimmage_core import record
from scrimmage_core.judge import RunJudgement
from scrimmage_core.plan import Row
from scrimmage_core.runner import RunResult

FILE = """# leading comment
[agent]
function = "bot:answer"
tools    = ["bot:get_order", "bot:other"]
real     = ["tool:get_order"]

[[test]]
name     = "one"
question = "q"
expect   = ["ok"]

[[test.mock]]
call    = "tool:get_order"
returns = {id = "old"}

[[test.mock]]
call    = "tool:other"
returns = [
    [1, 2],
]

# about the second test
[[test]]
name     = "two"
question = "q"
expect   = ["ok"]
"""


def loaded(tmp_path: Path, text: str = FILE) -> settings.Config:
    path = tmp_path / "scrimmage.toml"
    path.write_text(text)
    (tmp_path / "bot.py").write_text(
        "def answer(q):\n    return 'ok'\ndef get_order(o):\n    return o\n"
    )
    return settings.load(path)


def result_for(test: settings.Test) -> RunResult:
    return RunResult(
        "run-1", Row(test, "baseline", None), "answered", "ok", None, {}, judgement=RunJudgement()
    )


def line(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {"call_id": 1, "proc": {"pid": 1}}
    return {**base, **fields}


@pytest.mark.design("record.never_models")
@pytest.mark.parametrize(
    "label",
    [
        "model:openai",
        "model:openai/embeddings",
        "model:openai/moderations",
        "model:openai/responses",
    ],
)
def test_a_model_call_is_never_written_and_its_tool_is_never_mocked_whole(
    tmp_path: Path, label: str
) -> None:
    config = loaded(tmp_path)
    test = config.tests[1]
    recorder = record.Recorder(test, result_for(test), tmp_path)
    found = recorder.run(
        [
            line(
                call="tool:helper",
                kind="tool",
                action="real",
                call_id=2,
                recorded={"code": "x", "why": "y"},
            ),
            line(call=label, kind="model", action="real", call_id=3, parent=2),
            line(call=label, kind="model", action="real", call_id=4),
        ],
        [],
    )
    assert found.written == []
    assert [e.code for e in found.manual.values()] == ["model_inside"]


@pytest.mark.design("record.status_rule")
@pytest.mark.parametrize(
    ("status", "written"),
    [
        (200, True), (204, True), (404, True), (409, True), (422, True), (301, False),
        (401, False), (403, False), (407, False), (408, False), (429, False), (500, False),
        (503, False),
    ],
)  # fmt: skip
def test_only_replies_that_are_data_are_written(status: int, written: bool) -> None:
    assert record.data_status(status) is written


@pytest.mark.design("record.copy_plus_mocks")
def test_refresh_takes_out_the_mock_and_its_comment_and_keeps_everything_else(
    tmp_path: Path,
) -> None:
    text = FILE.replace(
        '[[test.mock]]\ncall    = "tool:get_order"',
        "# scrimmage record, 2026-11-01, run-a: tool:get_order, 1 call.\n"
        '[[test.mock]]\ncall    = "tool:get_order"',
    )
    config = loaded(tmp_path, text)
    plan, problems = record.prepare(config, [], ["tool:get_order"])
    assert plan is not None and not problems
    tests = plan.config.tests
    records = [record.TestRecord(test, result_for(test)) for test in tests]
    saved = record.Saved(
        ("tool:get_order", "{}"),
        {"call": "tool:get_order", "recorded": True, "args": {}, "returns": {"id": "new"}},
        "",
        "tool:get_order with {}",
    )
    records[0].written.append(record.Written(saved, 1))
    built = [record.block_text(found, "2026-11-08") for found in records]
    new, problem = record.spliced(text, plan, records, built, "2026-11-08", "scrimmage.toml")
    assert problem is None
    doc = tomllib.loads(new)
    assert [m["call"] for m in doc["test"][0]["mock"]] == ["tool:other", "tool:get_order"]
    assert doc["test"][0]["mock"][1]["returns"] == {"id": "new"}
    assert "old" not in new and "2026-11-01" not in new
    # Comments stay with what they describe; the array continuation line [1, 2] was not a table.
    assert new.index("# about the second test") > new.index("tool:get_order with {}")
    assert new.startswith("# leading comment\n# Written by scrimmage record from scrimmage.toml")


HEAD = '[agent]\nfunction = "bot:answer"\ntools = ["bot:get_order"]\nreal = ["tool:get_order"]\n'


@pytest.mark.design("record.layout_refused")
@pytest.mark.parametrize(
    "tests",
    [
        '\n[[test]]\nname = "one"\nquestion = """\n[[test]]\n"""\nexpect = ["ok"]\n',
        # The second test's real header is quoted, so only the line inside the string counts,
        # and the first test's mocks would land inside that string.
        '\n[[test]]\nname = "one"\nquestion = "q"\nexpect = ["ok"]\n'
        '\n[["test"]]\nname = "two"\nquestion = """\n[[test]]\n"""\nexpect = ["ok"]\n',
    ],
    ids=["count-differs", "read-back-differs"],
)
def test_a_test_line_inside_a_multi_line_string_is_refused_by_the_read_back(
    tmp_path: Path, tests: str
) -> None:
    text = HEAD + tests
    config = loaded(tmp_path, text)
    plan, _ = record.prepare(config, [], [])
    assert plan is not None
    records = [record.TestRecord(test, result_for(test)) for test in plan.config.tests]
    saved = record.Saved(
        ("tool:get_order", "{}"),
        {"call": "tool:get_order", "recorded": True, "args": {}, "returns": "x"},
        "",
        "w",
    )
    records[0].written.append(record.Written(saved, 1))
    built = [record.block_text(found, "2026-11-08") for found in records]
    _, problem = record.spliced(text, plan, records, built, "2026-11-08", "scrimmage.toml")
    assert problem is not None


@pytest.mark.design("record.big_diff_warns")
def test_the_batch_warning_starts_above_1000_added_lines() -> None:
    assert record.batch_warning(1000) is None
    assert "1001 lines" in (record.batch_warning(1001) or "")


@pytest.mark.design("record.skeleton")
@pytest.mark.parametrize("code", record.MANUAL_CODES)
def test_each_listed_code_gives_a_comment_that_adds_nothing(code: str) -> None:
    entry = record.ManualEntry("tool:x", code, "why\n[[test.fault]]", None, None, calls=1)
    text = record.skeleton(entry)
    assert tomllib.loads(text) == {}
    assert all(line.startswith("#") for line in text.splitlines())
    assert '# call    = "tool:x"' in text
