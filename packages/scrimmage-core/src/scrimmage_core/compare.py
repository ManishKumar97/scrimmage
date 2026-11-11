"""Comparing two finished tests, row by row. Pure functions over two result documents.

A row is matched by its test and its fault, never by its name: `fault 2` is only the second
fault of the day. The difference of two pass rates gets a 95% interval from the two Wilson
ranges the rows already carry (Newcombe's method 10), and nothing else.
"""

import json
import math
from typing import cast

from scrimmage_core.judge import wilson
from scrimmage_core.render import fault_text
from scrimmage_core.results import shape_problems

BETTER, WORSE, UNCLEAR, NO_RESULT, NEW, GONE = (
    "better",
    "worse",
    "unclear",
    "no result",
    "new",
    "gone",
)
CHANGES = (BETTER, WORSE, UNCLEAR, NO_RESULT, NEW, GONE)
PASS, FAIL = "pass", "fail"
UNCLEAR_TEXT = "these runs cannot tell the rates apart"
CHANGED_TEXT = "the test itself changed, so a change in rate may come from the test, not the agent"
DIFFERENCE_LINE = (
    "difference: a 95% interval of after minus before (Newcombe), from these runs alone"
)
CHANCE_LINE = (
    "each row is checked at 95%, so about 1 in 20 rows that did not change reads better or "
    "worse by chance"
)
GONE_LINE = (
    "a fault that was edited is a new row, and identical faults in one test match by their place"
)
Doc = dict[str, object]


def problems_with(doc: Doc, which: str) -> list[str]:
    """Why a result cannot be compared at all, if it cannot."""
    return shape_problems(doc, which)


def fault_key(row: Doc) -> str | None:
    """The row's fault with every null field dropped, as canonical JSON; None for a baseline."""
    fault = cast("Doc | None", row["fault"])
    if fault is None:
        return None
    kept = {k: v for k, v in fault.items() if v is not None}
    return json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def keyed(doc: Doc) -> dict[tuple[str, str | None, int], Doc]:
    """The rows by (test, fault, n), n being the row's place among the test's identical faults."""
    seen: dict[tuple[str, str | None], int] = {}
    out: dict[tuple[str, str | None, int], Doc] = {}
    for row in cast("list[Doc]", doc["rows"]):
        base = (str(row["test"]), fault_key(row))
        seen[base] = seen.get(base, 0) + 1
        out[(*base, seen[base])] = row
    return out


def side(row: Doc | None) -> Doc | None:
    if row is None:
        return None
    keys = ("passed", "judged", "rate", "range", "result")
    return {key: row.get(key) for key in keys}


def newcombe(before: Doc, after: Doc) -> tuple[float, float, float]:
    """The difference of the rates (after minus before) and its 95% interval.

    Worked from the counts with the judge's own Wilson interval, not from the rounded rate
    and range the rows print.
    """
    p1, (l1, u1) = rate_and_range(before)
    p2, (l2, u2) = rate_and_range(after)
    d = p2 - p1
    low = d - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2)
    high = d + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2)
    return d, low, high


def rate_and_range(side_doc: Doc) -> tuple[float, tuple[float, float]]:
    passed, judged = int(str(side_doc["passed"])), int(str(side_doc["judged"]))
    return passed / judged, wilson(passed, judged)


def judge_change(before: Doc, after: Doc) -> tuple[str, tuple[float, float, float] | None]:
    """How a row changed. A side with no result is never given a rate change."""
    if before["result"] == NO_RESULT or after["result"] == NO_RESULT:
        return NO_RESULT, None
    if not before["judged"] or not after["judged"]:
        return NO_RESULT, None
    d, low, high = newcombe(before, after)
    if low > 0:
        return BETTER, (d, low, high)
    if high < 0:
        return WORSE, (d, low, high)
    return UNCLEAR, (d, low, high)


def tests_differ(before: Doc, after: Doc) -> bool | None:
    """True or False when both rows carry a hash of the same format; None when we cannot tell."""
    one, two = before.get("test_sha256"), after.get("test_sha256")
    if one is None or two is None or before.get("test_sha256_v") != after.get("test_sha256_v"):
        return None
    return one != two


def rows(earlier: Doc, later: Doc) -> list[Doc]:
    """Every row of both results, compared. A row that got worse in its result comes first."""
    old, new = keyed(earlier), keyed(later)
    out: list[Doc] = []
    for key in [*new, *(k for k in old if k not in new)]:
        before, after = old.get(key), new.get(key)
        base: Doc = {
            "test": key[0],
            "fault": None if key[1] is None else json.loads(key[1]),
            "n": key[2],
            "row_before": None if before is None else before["row"],
            "row_after": None if after is None else after["row"],
            "before": side(before),
            "after": side(after),
            "difference": None,
            "result_change": None,
            "test_changed": None,
        }
        if before is None or after is None:
            base["change"] = NEW if before is None else GONE
        else:
            change, diff = judge_change(before, after)
            base["change"] = change
            if diff is not None:
                value, low, high = diff
                base["difference"] = {
                    "value": round(value, 4),
                    "range": [round(low, 4), round(high, 4)],
                }
            base["result_change"] = f"{before['result']} -> {after['result']}"
            base["test_changed"] = tests_differ(before, after)
        out.append(base)
    out.sort(key=lambda row: 0 if row["result_change"] == f"{PASS} -> {FAIL}" else 1)
    return out


def document(earlier_path: str, earlier: Doc, compared: list[Doc]) -> Doc:
    return {
        "earlier": earlier_path,
        "earlier_scrimmage": earlier.get("scrimmage"),
        "rows_compared": len(compared),
        "rows": compared,
    }


def span(side_doc: Doc | None) -> str:
    if side_doc is None:
        return "-"
    if not side_doc["judged"]:
        return f"{side_doc['passed']}/{side_doc['judged']} -"
    low, high = wilson(int(str(side_doc["passed"])), int(str(side_doc["judged"])))
    return f"{side_doc['passed']}/{side_doc['judged']} {low:.2f}-{high:.2f}"


def change_text(row: Doc) -> str:
    parts: list[str] = []
    if row["result_change"] == f"{PASS} -> {FAIL}":
        parts.append(f"got worse: {row['result_change']}")
    change = row["change"]
    diff = cast("Doc | None", row["difference"])
    if change in (BETTER, WORSE) and diff is not None:
        low, high = cast("list[float]", diff["range"])
        parts.append(f"{change} (difference {low:.2f} to {high:.2f})")
    elif change == UNCLEAR:
        parts.append(f"unclear: {UNCLEAR_TEXT}")
    elif change == NO_RESULT:
        parts.append("no result: a side has too few judged runs to compare")
    else:
        parts.append(str(change))
    if row["test_changed"] is True:
        parts.append("the test changed")
    return "; ".join(parts)


def text(compared: Doc) -> list[str]:
    """The comparison as lines for a person."""
    listed = cast("list[Doc]", compared["rows"])
    version = compared["earlier_scrimmage"]
    lines = [
        f"compared with {compared['earlier']} (scrimmage {version}): "
        f"{len(listed)} row{'' if len(listed) == 1 else 's'}"
    ]
    table = [["test", "row", "before", "after", "change"]]
    for row in listed:
        fault = cast("Doc | None", row["fault"])
        label = "baseline" if fault is None else fault_text(fault)
        table.append(
            [
                str(row["test"]),
                label,
                span(cast("Doc | None", row["before"])),
                span(cast("Doc | None", row["after"])),
                change_text(row),
            ]
        )
    widths = [max(len(line[i]) for line in table) for i in range(4)]
    lines += [
        "  ".join(cell.ljust(w) for cell, w in zip(line[:4], widths, strict=True)) + f"  {line[4]}"
        for line in table
    ]
    lines.append(DIFFERENCE_LINE)
    lines.append(CHANCE_LINE)
    if any(row["change"] in (NEW, GONE) for row in listed):
        lines.append(f"note: {GONE_LINE}")
    if any(row["test_changed"] is True for row in listed):
        lines.append(f"note: {CHANGED_TEXT}")
    if any(row["test_changed"] is None and row["change"] not in (NEW, GONE) for row in listed):
        lines.append(
            "note: a row has no test hash of this format, so it cannot be told if the test changed"
        )
    return lines
