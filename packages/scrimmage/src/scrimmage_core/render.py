"""How a test's rows are shown: the table, a row's fault and a mock miss, as text.

Pure functions over the JSON of a row, and nothing else, so `scrimmage test` and
`scrimmage show` print the same bytes from the same data. Standard library only.
"""

from typing import cast

from scrimmage_core.judge import BASELINE_FAILED, wilson

TABLE_WIDTH = 120
HEAD = [
    "test",
    "row",
    "fault",
    "runs",
    "fired",
    "done",
    "passed",
    "rate",
    "range",
    "tools",
    "cost",
    "result",
]
TEST_COLUMN, FAULT_COLUMN = 0, 2


def plain(text: str) -> str:
    """Text for a terminal: a control character, such as ESC, BEL or CR, is shown as \\xNN.

    A record or a result can hold text an agent or a file author chose, and a terminal acts on
    these characters.
    """
    return "".join(
        f"\\x{ord(c):02x}" if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F else c for c in text
    )


def fault_text(fault: dict[str, object] | None) -> str:
    """The fault column: its call, the model name if it has one, what it does, and when."""
    if fault is None:
        return "-"
    call, model = str(fault["call"]), fault.get("model")
    target = call if model is None else f"{call} {model}"
    when = "" if fault["when"] == "always" else f" {fault['when']}"
    return f"{target} {fault['do']}{when}"


def row_cells(row: dict[str, object]) -> list[str]:
    """One table row, from a row's JSON, in the order of HEAD."""
    rate, span = "-", "-"
    passed, judged = int(str(row["passed"])), int(str(row["judged"]))
    # From the counts, not from the rounded rate and range the JSON carries. A row beside a
    # failed baseline shows none: its rate says little.
    if judged and row["result"] != BASELINE_FAILED:
        low, high = wilson(passed, judged)
        rate, span = f"{passed / judged:.0%}", f"{low * 100:.0f}-{high:.0%}"
    fired = row["fired"]
    return [
        str(row["test"]),
        str(row["row"]),
        fault_text(cast("dict[str, object] | None", row["fault"])),
        str(row["runs"]),
        "-" if fired is None else str(fired),
        str(row["done"]),
        f"{row['passed']}/{row['judged']}",
        rate,
        span,
        f"{cast(float, row['tools']):.1f}",
        f"{cast(float, row['cost_usd']):.5f}",
        str(row["result"]),
    ]


def fitted(table: list[list[str]]) -> list[int]:
    """Column widths within TABLE_WIDTH. Only the test and fault names can be cut, as every
    line under the table gives them in full; the shorter one keeps its width if it can."""
    widths = [max(len(line[i]) for line in table) for i in range(len(HEAD))]
    test, fault = widths[TEST_COLUMN], widths[FAULT_COLUMN]
    room = TABLE_WIDTH - sum(widths) + test + fault - 2 * (len(HEAD) - 1)
    if test + fault > room:
        widths[TEST_COLUMN] = min(test, max(room // 2, room - fault))
        widths[FAULT_COLUMN] = room - widths[TEST_COLUMN]
    return widths


def cut(text: str, width: int) -> str:
    """Cut from the middle, so a fault's schedule and the end of a long name still show."""
    if len(text) <= width:
        return text
    kept = width - 3
    return f"{text[: kept // 2]}...{text[-(kept - kept // 2) :]}"


def table_lines(table: list[list[str]], names: list[str]) -> list[str]:
    """The table as lines, then a "full name" line for each row whose names were cut.

    `table` starts with HEAD; `names` is "test row" for each row after it.
    """
    widths = fitted(table)
    lines = [
        "  ".join(cut(cell, w).ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip()
        for line in table
    ]
    for name, line in zip(names, table[1:], strict=True):
        if len(line[TEST_COLUMN]) > widths[TEST_COLUMN] or (
            len(line[FAULT_COLUMN]) > widths[FAULT_COLUMN]
        ):
            lines.append(f"full name: {name}: {line[FAULT_COLUMN]}")
    return lines


def nearest_text(doc: dict[str, object]) -> str:
    """The nearest mock as a person reads it, labelled as a heuristic."""
    near = cast("dict[str, object] | None", doc["nearest"])
    if near is None:
        return ""
    if doc["reason"] == "request":
        request = " ".join(str(part) for part in (near["method"], near["path"]) if part)
        return f"; nearest mock (heuristic: most path segments alike): {request or 'any request'}"
    keys = cast(list[str], near["differs"])
    more = int(str(near["differs_left_out"]))
    differs = f", differs at {', '.join(keys)}{f' and {more} more' if more else ''}" if keys else ""
    shown = near["args"] if near["args"] is not None else "arguments not readable"
    return f"; nearest mock (heuristic: fewest differing arguments): {shown}{differs}"


def miss_text(doc: dict[str, object], where: str) -> str:
    """One warning line for a group of misses. It states what happened and that the nearest
    mock is a guess; it never says the agent or the mock is wrong."""
    call, ran = str(doc["call"]), doc["action"] == "real"
    seen = f" with {doc['seen']}" if doc["seen"] is not None else ""
    tail = f" ({where})"
    if doc["reason"] == "request":
        request = f"{call} {doc['method']} {doc['path']}"
        verdict = "ran for real" if ran else "was refused"
        return (
            f"{request} {verdict}, though the test has mocks for that host; "
            f"none matched its method and path{nearest_text(doc)}{tail}"
        )
    if doc["reason"] == "no_mock":
        return (
            f"{call}{seen} has no mock in this test and was refused, and the test has mocks "
            f"that match arguments{tail}"
        )
    if doc["reason"] == "unreadable":
        return f"{call} missed a mock, but the evidence could not be built{tail}"
    verdict = (
        "matched no mock and ran for real, as it is under [agent] real"
        if ran
        else "matched no mock and was refused, as it is not under [agent] real by name"
    )
    return f"{call}{seen} {verdict}{nearest_text(doc)}; values cleaned by a heuristic{tail}"
