"""Comparing two finished tests: rows matched by what they test, and rates told apart."""

import json
import os
import signal
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import Project
from report_cases import AGENT, BAD, FAULT, GOOD, SORRY, TEST, run
from scrimmage_core import cli, compare, config, entry, judge
from scrimmage_core import plan as planner
from scrimmage_core.runner import Runner, RunResult
from show_cases import faulted_run, real_id

pytestmark = pytest.mark.usefixtures("signals_kept")
Doc = dict[str, Any]
SOURCE = (
    AGENT.replace("runs     = 3", "runs     = 10")
    + TEST.replace('forbid   = ["refunded"]', 'forbid   = ["refunded"]\npass_rate = 0.5')
    + FAULT
)


def row(
    passed: int,
    judged: int,
    *,
    fault: Doc | None = None,
    test: str = "order-status",
    name: str | None = None,
    result: str | None = None,
    sha: str | None = "t1",
    version: int = 1,
) -> Doc:
    """A row as `scrimmage test --json` prints it, with only the fields a comparison reads."""
    low, high = judge.wilson(passed, judged) if judged else (0.0, 0.0)
    found = result or ("pass" if judged and passed == judged else "fail")
    doc: Doc = {
        "test": test,
        "row": name or ("baseline" if fault is None else "fault 1"),
        "fault": fault,
        "passed": passed,
        "judged": judged,
        "rate": round(passed / judged, 4) if judged else None,
        "range": [round(low, 4), round(high, 4)] if judged else None,
        "result": found,
    }
    if sha is not None:
        doc.update(test_sha256=sha, test_sha256_v=version)
    return doc


def fault(call: str = "tool:get_order", do: str = "timeout", **own: object) -> Doc:
    return {
        "call": call,
        "do": do,
        "when": "always",
        "model": None,
        "method": None,
        "path": None,
        "raises": None,
        "expect": None,
        "forbid": None,
        "pass_rate": None,
        **own,
    }


def result(*rows: Doc, version: str = "0.4.0") -> Doc:
    return {"v": 1, "scrimmage": version, "command": "test", "rows": list(rows)}


def by_key(compared: list[Doc]) -> dict[tuple[str, str], Doc]:
    return {(str(c["test"]), "baseline" if c["fault"] is None else "fault"): c for c in compared}


def test_rows_match_by_test_and_fault_not_by_position() -> None:
    a, b = fault("tool:get_order"), fault("tool:check_stock", "error")
    earlier = result(
        row(3, 3), row(3, 3, fault=a, name="fault 1"), row(3, 3, fault=b, name="fault 2")
    )
    # A fault added above another: `fault 2` now means the first one.
    later = result(
        row(3, 3),
        row(3, 3, fault=fault("web", "timeout"), name="fault 1"),
        row(3, 3, fault=a, name="fault 2"),
        row(1, 3, fault=b, name="fault 3"),
    )
    compared: list[Any] = compare.rows(earlier, later)
    pairs = {(c["row_before"], c["row_after"]): (c["fault"] or {}).get("call") for c in compared}
    assert pairs[("fault 1", "fault 2")] == "tool:get_order"
    assert pairs[("fault 2", "fault 3")] == "tool:check_stock"
    assert pairs[(None, "fault 1")] == "web"
    assert [c["change"] for c in compared if c["row_before"] is None] == ["new"]


def test_identical_faults_stay_two_rows_by_place() -> None:
    same = fault("tool:get_order")
    earlier = result(row(3, 3, fault=same, name="fault 1"), row(0, 3, fault=same, name="fault 2"))
    later = result(row(3, 3, fault=same, name="fault 1"), row(3, 3, fault=same, name="fault 2"))
    compared: list[Any] = compare.rows(earlier, later)
    assert [
        (c["n"], c["row_before"], c["before"]["passed"], c["after"]["passed"]) for c in compared
    ] == [
        (1, "fault 1", 3, 3),
        (2, "fault 2", 0, 3),
    ]
    # One of the two removed: the survivor is n = 1, and the other is gone.
    dropped = compare.rows(earlier, result(row(3, 3, fault=same, name="fault 1")))
    assert [(c["n"], c["change"]) for c in dropped] == [(1, "unclear"), (2, "gone")]


def test_a_faults_own_rules_are_part_of_its_key() -> None:
    plain = fault()
    ruled = fault(expect=[["sorry"]], forbid=["Oct 30"], pass_rate=0.5)
    earlier = result(row(3, 3, fault=plain, name="fault 1"))
    later = result(row(3, 3, fault=ruled, name="fault 1"))
    compared: list[Any] = compare.rows(earlier, later)
    assert sorted(str(c["change"]) for c in compared) == ["gone", "new"]
    assert compare.fault_key(row(3, 3, fault=plain)) == (
        '{"call":"tool:get_order","do":"timeout","when":"always"}'
    )
    assert compare.fault_key(row(3, 3)) is None


@pytest.mark.parametrize(
    "before, after, expected",
    [
        ((1, 3), (3, 3), (-0.059, 0.939)),
        ((0, 3), (3, 3), (0.206, 1.0)),
        ((2, 5), (5, 5), (0.030, 0.882)),
        ((2, 5), (4, 5), (-0.163, 0.726)),
        ((5, 10), (10, 10), (0.117, 0.763)),
    ],
)
def test_the_difference_interval_matches_newcombe_on_the_table_above(
    before: tuple[int, int], after: tuple[int, int], expected: tuple[float, float]
) -> None:
    (compared,) = compare.rows(result(row(*before)), result(row(*after)))
    diff = cast(Doc, compared["difference"])
    # By hand: Wilson 95% ranges, then Newcombe's method 10 on them.
    z = 1.96

    def wilson(p: int, n: int) -> tuple[float, float, float]:
        centre = (p / n + z * z / (2 * n)) / (1 + z * z / n)
        half = z * (p / n * (1 - p / n) / n + z * z / (4 * n * n)) ** 0.5 / (1 + z * z / n)
        return p / n, max(0.0, centre - half), min(1.0, centre + half)

    p1, l1, u1 = wilson(*before)
    p2, l2, u2 = wilson(*after)
    low = (p2 - p1) - ((p2 - l2) ** 2 + (u1 - p1) ** 2) ** 0.5
    high = (p2 - p1) + ((u2 - p2) ** 2 + (p1 - l1) ** 2) ** 0.5
    got_low, got_high = cast("list[float]", diff["range"])
    assert (round(low, 3), round(high, 3)) == pytest.approx(expected, abs=1.5e-3)
    assert (got_low, got_high) == pytest.approx((low, high), abs=2e-4)
    assert diff["value"] == pytest.approx(p2 - p1, abs=1e-4)


def test_overlapping_ranges_can_still_be_better() -> None:
    before, after = row(0, 3), row(3, 3)
    (low_b, high_b), (low_a, high_a) = before["range"], after["range"]  # type: ignore[misc]
    assert low_a < high_b  # the two Wilson ranges overlap
    (compared,) = compare.rows(result(before), result(after))
    assert compared["change"] == "better"
    (worse,) = compare.rows(result(row(10, 10)), result(row(5, 10)))
    assert worse["change"] == "worse"
    assert low_b == 0.0 and high_a == 1.0


def test_a_side_with_no_result_gives_no_result() -> None:
    # 1 judged of 3 planned (the floor is 2): the judge gave the row no result.
    thin = row(1, 1, result="no result")
    full = row(3, 3)
    for earlier, later in ((thin, full), (full, thin)):
        (compared,) = compare.rows(result(earlier), result(later))
        assert compared["change"] == "no result" and compared["difference"] is None
    zero = row(0, 0, result="no result")  # 0 of 0 is never a division
    (compared,) = compare.rows(result(zero), result(full))
    assert compared["change"] == "no result"
    assert judge.floor_runs(3) == 2


def test_a_row_beside_a_failed_baseline_gives_no_result() -> None:
    """Its counts are there, yet its rate means little, so it is never given a rate change."""
    beside = {**row(9, 10, fault=fault()), "result": "baseline failed", "rate": None, "range": None}
    earlier = row(1, 10, fault=fault())
    for before, after in ((earlier, beside), (beside, earlier)):
        (compared,) = compare.rows(result(before), result(after))
        assert compared["change"] == "no result" and compared["difference"] is None
        assert "a baseline that failed" in compare.change_text(compared)
        shown = compared["after"] if after is beside else compared["before"]
        assert compare.span(cast(Doc, shown)) == "9/10 -"  # no range beside it either


def test_an_unclear_change_says_the_runs_cannot_tell_the_rates_apart() -> None:
    (compared,) = compare.rows(result(row(2, 5)), result(row(4, 5)))
    assert compared["change"] == "unclear"
    lines = compare.text(compare.document("before", result(), [compared]))
    assert any("these runs cannot tell the rates apart" in line for line in lines)
    assert not any(line.rstrip().endswith("same") for line in lines)


def test_a_pass_to_fail_row_is_listed_first_even_when_unclear() -> None:
    other = fault("tool:check_stock", "error")
    earlier = result(row(3, 3), row(3, 3, fault=other, name="fault 1"))
    later = result(row(3, 3), row(1, 3, fault=other, name="fault 1"))
    compared: list[Any] = compare.rows(earlier, later)
    assert [c["row_after"] for c in compared] == ["fault 1", "baseline"]
    first = compared[0]
    assert first["result_change"] == "pass -> fail" and first["change"] == "unclear"
    text = compare.text(compare.document("before", result(), compared))
    line = next(x for x in text if "tool:check_stock error" in x)
    assert "got worse: pass -> fail" in line and "cannot tell the rates apart" in line


def test_a_changed_test_is_marked() -> None:
    earlier = result(row(3, 3, sha="old"), row(3, 3, test="b", sha="same"), row(3, 3, test="c"))
    later = result(
        row(3, 3, sha="new"),
        row(3, 3, test="b", sha="same"),
        row(3, 3, test="c", sha=None),
    )
    compared = by_key(compare.rows(earlier, later))
    assert compared[("order-status", "baseline")]["test_changed"] is True
    assert compared[("b", "baseline")]["test_changed"] is False
    assert compared[("c", "baseline")]["test_changed"] is None
    text = "\n".join(compare.text(compare.document("before", earlier, list(compared.values()))))
    assert "the test itself changed, so a change in rate may come from the test" in text
    other_format = compare.rows(result(row(3, 3, version=1)), result(row(3, 3, version=2)))
    assert other_format[0]["test_changed"] is None  # another hash format: we cannot tell


def test_new_and_gone_rows() -> None:
    kept, dropped, added = fault("a"), fault("b"), fault("c")
    earlier = result(row(3, 3, fault=kept), row(3, 3, fault=dropped, name="fault 2"))
    later = result(row(3, 3, fault=kept), row(2, 3, fault=added, name="fault 2"))
    compared: list[Any] = compare.rows(earlier, later)
    by_change = {str(c["change"]): c for c in compared}
    assert by_change["gone"]["after"] is None and by_change["gone"]["row_after"] is None
    assert by_change["new"]["before"] is None and by_change["new"]["difference"] is None
    text = compare.text(compare.document("before", earlier, compared))
    assert any("a fault that was edited is a new row" in line for line in text)


def test_the_count_and_the_one_in_twenty_line_are_printed() -> None:
    earlier, later = result(row(0, 3), version="0.3.9"), result(row(3, 3))
    compared = compare.document("scrimmage-runs/old", earlier, compare.rows(earlier, later))
    assert compared["rows_compared"] == 1
    lines = compare.text(compared)
    assert lines[0] == "compared with scrimmage-runs/old (scrimmage 0.3.9): 1 row"
    assert (
        "each row is checked at 95%, so about 1 in 20 rows that did not change reads better or "
        "worse by chance"
    ) in lines
    assert (
        "difference: a 95% interval of after minus before (Newcombe), from these runs alone"
        in lines
    )
    assert "better (difference 0.21 to 1.00)" in lines[2]


# The Acme file, with ten runs, and answers set by the test in place of an agent's.
def scripted(
    project: Project, monkeypatch: pytest.MonkeyPatch, base: int, faulted: int, runs: int = 10
) -> None:
    """The next run_all gives `base` good answers of 10 on the baseline, `faulted` on the fault."""
    project.toml.write_text(SOURCE.replace("runs     = 10", f"runs     = {runs}"))
    cfg = config.load(project.toml)
    rows = planner.rows(cfg)

    def run_all(self: Runner) -> list[RunResult]:
        out: list[RunResult] = []
        for r, good in zip(rows, (base, faulted), strict=True):
            out += [
                run(r, i, answer=(GOOD if r.fault is None else SORRY) if i < good else BAD)
                for i in range(runs)
            ]
        return out

    monkeypatch.setattr(Runner, "run_all", run_all)


def folders(project: Project) -> list[Path]:
    return sorted(p for p in (project.root / "scrimmage-runs").iterdir() if p.is_dir())


def test_compare_keeps_the_tests_exit_code(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(project, monkeypatch, 10, 10)
    assert cli.main(["test", str(project.toml)]) == 0
    capsys.readouterr()
    first = folders(project)[0]
    # The baseline now passes 5 of 10: worse, and still meeting the pass_rate of 0.5.
    scripted(project, monkeypatch, 5, 10)
    plain = cli.main(["test", "--json", str(project.toml)])
    plain_doc = json.loads(capsys.readouterr().out)
    with_compare = cli.main(["test", "--json", str(project.toml), "--compare", str(first)])
    doc = json.loads(capsys.readouterr().out)
    assert plain == with_compare == doc["exit"] == 0
    changes = {r["row_after"]: r["change"] for r in doc["compare"]["rows"]}
    assert changes == {"baseline": "worse", "fault 1": "unclear"}
    assert plain_doc["compare"] is None


def test_test_output_above_the_comparison_is_unchanged(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(project, monkeypatch, 4, 10)
    assert cli.main(["test", str(project.toml)]) == 1
    alone = capsys.readouterr().out
    first = folders(project)[0]
    assert cli.main(["test", str(project.toml), "--compare", str(first / "result.json")]) == 1
    both = capsys.readouterr().out
    (newest,) = set(folders(project)) - {first}

    def plain(text: str, folder: Path) -> str:
        return text.replace(str(folder), "FOLDER")

    head = plain(alone, first)
    assert plain(both, newest).startswith(head)
    below = plain(both, newest)[len(head) :].splitlines()
    assert below[0].startswith("compared with ") and str(first / "result.json") in below[0]
    assert "tool:get_order timeout" in "\n".join(below)


def test_an_earlier_set_without_a_result_file_is_a_mistake_before_any_run(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(self: Runner) -> list[RunResult]:
        raise AssertionError("a run was started")

    monkeypatch.setattr(Runner, "run_all", never)
    older = project.root / "scrimmage-runs" / "20260101T000000Z-aaaaaa"  # from before J4
    older.mkdir(parents=True)
    (older / "run-aaaaaaaaaaaa.jsonl").write_text("{}\n")
    code = cli.main(["test", "--json", str(project.toml), "--compare", str(older)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 2 == doc["exit"] and doc["status"] == "mistake"
    assert "no result.json" in doc["mistakes"][0]["message"]
    assert "run the earlier side again" in doc["mistakes"][0]["message"]
    assert folders(project) == [older]  # no records folder was made for the refused test
    text = cli.main(["test", str(project.toml), "--compare", str(older)])
    assert text == 2 and "no result.json" in capsys.readouterr().err
    other = project.root / "other.json"  # a result of another command is not an earlier test
    other.write_text(json.dumps({"v": 1, "command": "check"}))
    assert cli.main(["test", str(project.toml), "--compare", str(other)]) == 2
    other.write_text(json.dumps({"v": 2, "command": "test", "rows": []}))
    assert cli.main(["test", str(project.toml), "--compare", str(other)]) == 2


def test_a_stopped_later_test_gives_no_comparison_and_says_why(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(project, monkeypatch, 10, 10)
    cli.main(["test", str(project.toml)])
    capsys.readouterr()
    first = folders(project)[0]
    sent: list[bool] = []

    def one(self: Runner, row: object) -> None:
        if not sent:
            sent.append(True)
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.undo()
    monkeypatch.setattr(Runner, "one", one)
    entry.pending.clear()
    code = cli.main(["test", "--json", str(project.toml), "--compare", str(first)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 3 and doc["compare"] is None
    notes = [n["message"] for n in doc["notes"]]
    assert "the test did not finish, so it has no result to compare" in notes
    # The budget stopping the test is the same: its rows have fewer runs than planned.
    monkeypatch.setattr(Runner, "one", lambda self, row: setattr(self, "stopped_by", "budget"))
    code = cli.main(["test", "--json", str(project.toml), "--compare", str(first)])
    doc = json.loads(capsys.readouterr().out)
    assert doc["compare"] is None and doc["result_file"] is None
    assert "the test did not finish, so it has no result to compare" in [
        n["message"] for n in doc["notes"]
    ]
    assert code == doc["exit"]


def test_show_compares_two_finished_sets_and_runs_nothing(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(project, monkeypatch, 1, 10)
    cli.main(["test", str(project.toml)])
    (before,) = folders(project)
    scripted(project, monkeypatch, 10, 10)
    cli.main(["test", str(project.toml)])
    capsys.readouterr()
    (after,) = set(folders(project)) - {before}
    for folder in (before, after):  # the scripted runs left no records; show wants some
        faulted_run(folder, real_id(folder.name), "order-status", "baseline")

    def never(self: Runner) -> list[RunResult]:
        raise AssertionError("a run was started")

    monkeypatch.setattr(Runner, "run_all", never)
    code = cli.main(["show", str(after), "--compare", str(before), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 == doc["exit"], doc["mistakes"]
    baseline = next(r for r in doc["compare"]["rows"] if r["fault"] is None)
    assert baseline["change"] == "better" and baseline["before"]["passed"] == 1
    assert cli.main(["show", str(after), "--compare", str(before)]) == 0
    assert "compared with " in capsys.readouterr().out
    assert cli.main(["show", str(after), "--compare", str(project.root)]) == 2


def test_change_equals_exact_newcombe_on_a_grid() -> None:
    """The change for every pair of counts up to 20 runs a side, from unrounded Wilson ranges.

    The rows carry ranges rounded to 4 places, and a change must not turn on that rounding.
    """
    for n in range(1, 21):
        for m in range(1, 21):
            for a in range(n + 1):
                for b in range(m + 1):
                    p1, p2 = a / n, b / m
                    l1, u1 = judge.wilson(a, n)
                    l2, u2 = judge.wilson(b, m)
                    low = (p2 - p1) - ((p2 - l2) ** 2 + (u1 - p1) ** 2) ** 0.5
                    high = (p2 - p1) + ((u2 - p2) ** 2 + (p1 - l1) ** 2) ** 0.5
                    want = "better" if low > 0 else "worse" if high < 0 else "unclear"
                    (got,) = compare.rows(
                        result(row(a, n, result="pass")), result(row(b, m, result="pass"))
                    )
                    assert got["change"] == want, (a, n, b, m)
    (zero_nine,) = compare.rows(result(row(0, 9)), result(row(9, 27)))
    assert zero_nine["change"] == "better"


def test_a_damaged_earlier_result_is_a_mistake_before_any_run(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(self: Runner) -> list[RunResult]:
        raise AssertionError("a run was started")

    monkeypatch.setattr(Runner, "run_all", never)
    damaged = project.root / "damaged.json"
    for rows in (
        [{"test": "x"}],
        ["x"],
        [{**row(1, 1), "passed": "1"}],
        [{**row(1, 1), "fault": 3}],
    ):
        damaged.write_text(json.dumps({"v": 1, "command": "test", "rows": rows}))
        code = cli.main(["test", "--json", str(project.toml), "--compare", str(damaged)])
        doc = json.loads(capsys.readouterr().out)
        assert code == 2 and "damaged" in doc["mistakes"][0]["message"], rows


def test_a_comparison_that_fails_keeps_the_tests_exit_and_warns(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(project, monkeypatch, 10, 10)
    cli.main(["test", str(project.toml)])
    capsys.readouterr()
    (first,) = folders(project)
    monkeypatch.setattr(compare, "rows", lambda *_: (_ for _ in ()).throw(KeyError("fault")))
    code = cli.main(["test", "--json", str(project.toml), "--compare", str(first)])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 == doc["exit"] and doc["compare"] is None
    assert "could not be made" in doc["warnings"][0]["message"]


def interrupted_after_one_run(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first run ends with an answer; SIGTERM comes as the second starts."""
    cfg = config.load(project.toml)
    first = planner.rows(cfg)[0]
    sent: list[bool] = []

    def one(self: Runner, row: object) -> RunResult | None:
        if sent:
            return None
        sent.append(True)
        done = run(first, 0)
        os.kill(os.getpid(), signal.SIGTERM)
        return done

    monkeypatch.setattr(Runner, "one", one)


def test_a_stopped_test_keeps_a_result_for_the_runs_that_ended(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    interrupted_after_one_run(project, monkeypatch)
    code = cli.main(["test", "--json", str(project.toml)])
    doc = json.loads(capsys.readouterr().out)
    assert code == doc["exit"] == 3 and doc["stopped_by"] == "interrupted"
    (folder,) = folders(project)
    assert doc["result_file"] == str(folder / "result.json")
    kept = json.loads((folder / "result.json").read_text())
    assert kept["stopped_by"] == "interrupted" and kept["exit"] == 3
    assert [(r["row"], r["runs"], r["done"]) for r in kept["rows"] if r["done"]] == [
        ("baseline", 1, 1)
    ]
    infra = [m["message"] for m in kept["infra"]]
    assert infra[-1] == "stopped by SIGTERM; every run was stopped"
    capsys.readouterr()
    faulted_run(folder, real_id(folder.name), "order-status", "baseline")  # the fake run's record
    assert cli.main(["show", str(folder)]) == 0
    shown = capsys.readouterr()
    assert "baseline" in shown.out and "stopped by a signal" in shown.out


def test_a_stopped_result_is_not_a_result_to_compare_with(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    interrupted_after_one_run(project, monkeypatch)
    assert cli.main(["test", str(project.toml)]) == 3
    (folder,) = folders(project)
    line = capsys.readouterr().err
    assert f"1 run had ended, and {folder / 'result.json'} holds them" in line
    doc = json.loads((folder / "result.json").read_text())
    (why,) = compare.problems_with(doc, "earlier")
    assert "stopped" in why
