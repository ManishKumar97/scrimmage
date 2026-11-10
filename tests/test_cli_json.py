"""--json: one document on standard output, one envelope, and the same report as the text."""

import ast
import contextlib
import importlib.metadata
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

import scrimmage_core
from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from report_cases import CASES, FOLDER, GOLDEN
from scrimmage_core import capture, cli, config, entry, judge, outcomes, record, runner
from scrimmage_core import init as init_module
from scrimmage_core.runner import Runner, build_report, print_report, report_json

Fake = Callable[..., FakeOpenAI]
pytestmark = pytest.mark.usefixtures("signals_kept")
SCHEMAS = Path(scrimmage_core.__file__).parent / "schemas"
CLI_SCHEMA = json.loads((SCHEMAS / "cli.v1.json").read_text())
SCRIMMAGE = str(Path(sys.executable).parent / "scrimmage")
SCRIM = str(Path(sys.executable).parent / "scrim")
LISTS = ("mistakes", "notes", "warnings", "infra")
PREFIXES = {
    "mistakes": "test mistake: ",
    "notes": "note: ",
    "warnings": "warning: ",
    "infra": "infrastructure error: ",
}


@dataclass
class Done:
    code: int
    out: str
    err: str

    @property
    def doc(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.out))


def strict(node: object) -> object:
    """The shipped schema closed, for our own documents: no unknown field, no unknown value.

    The shipped one is open, so a reader on any v1 copy accepts what a later release adds.
    Ours must not add anything the schema does not list, so tests check against this.
    """
    if isinstance(node, list):
        return [strict(item) for item in cast(list[object], node)]
    if not isinstance(node, dict):
        return node
    # An "if" only picks the branch, so closing it would make it never match.
    table = {k: v if k == "if" else strict(v) for k, v in cast(dict[str, object], node).items()}
    if "anyOf" in table:
        kept = [
            alt
            for alt in cast(list[dict[str, object]], table["anyOf"])
            if not str(alt.get("$comment", "")).startswith("open:")
        ]
        table["anyOf"] = kept
    if "properties" in table:
        table["additionalProperties"] = False
    return table


def closed_schema() -> dict[str, object]:
    closed = cast(dict[str, object], strict(CLI_SCHEMA))
    # Only the commands this release has: the open envelope is for a later one.
    closed["allOf"] = [
        *cast(list[object], closed["allOf"])[1:],
        {"required": ["command"], "properties": {"command": {"enum": list(cli.STATUS)}}},
    ]
    return closed


def validate(doc: object) -> None:
    """A document fits the shipped schema, and the closed one too."""
    for schema in (CLI_SCHEMA, closed_schema()):
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(doc)


def scrimmage(
    *args: str, env: dict[str, str] | None = None, cwd: Path | None = None, command: str = SCRIMMAGE
) -> Done:
    """The scrimmage command, as a team runs it: its own process, its own streams."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    proc = subprocess.run(
        [command, *args],
        env={**clean, **(env or {})},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return Done(proc.returncode, proc.stdout, proc.stderr)


def model_env(fake: FakeOpenAI) -> dict[str, str]:
    return {"OPENAI_BASE_URL": fake.base_url, "OPENAI_API_KEY": "test"}


def one_document(done: Done, command: str) -> dict[str, object]:
    """Standard output is one JSON value that fits the schema, and stderr is empty."""
    assert done.err == "", done.err
    doc = done.doc
    validate(doc)
    assert doc["command"] == command and doc["exit"] == done.code
    return doc


def messages(doc: dict[str, object], key: str) -> list[str]:
    items = cast(list[dict[str, object]], doc[key])
    return [str(item["message"]) for item in items]


def runs_of_test(project: Project, fake_openai: Fake) -> dict[int, Done]:
    """One `scrimmage test --json` per exit code."""
    toml = str(project.toml)
    done = {0: scrimmage("test", "--json", toml, env=model_env(fake_openai(ACME)))}
    project.edit('expect   = ["shipped", ', 'expect   = ["refunded", ')
    done[1] = scrimmage("test", "--json", toml, env=model_env(fake_openai(ACME)))
    project.edit('expect   = ["refunded", ', 'expect   = ["shipped", ')
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    failing = fake_openai(["500:error_500"])
    done[3] = scrimmage("test", "--json", toml, env=model_env(failing))
    project.edit("[agent]\n", "[agent]\nbogus = 1\n")
    done[2] = scrimmage("test", "--json", toml)
    return done


def check_runs(project: Project) -> dict[int, Done]:
    """One `scrimmage check --json` per exit code; 3 by making the file's reader raise."""
    toml = str(project.toml)
    done = {0: scrimmage("check", "--json", toml)}
    broken = (
        "import sys; from scrimmage_core import cli, config\n"
        "def boom(path): raise RuntimeError('the reader broke')\n"
        "config.load = boom\n"
        f"sys.exit(cli.main(['check', '--json', {toml!r}]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", broken], capture_output=True, text=True, check=False
    )
    done[3] = Done(proc.returncode, proc.stdout, proc.stderr)
    project.edit('name     = "order-status"', 'name     = ""')
    done[2] = scrimmage("check", "--json", toml)
    return done


@pytest.mark.design("json.one_document")
@pytest.mark.slow
def test_each_command_prints_one_document_for_each_exit_code(
    project: Project, fake_openai: Fake
) -> None:
    tests = runs_of_test(project, fake_openai)
    assert sorted(tests) == [0, 1, 2, 3]
    for code, done in tests.items():
        assert done.code == code, done.out + done.err
        doc = one_document(done, "test")
        assert doc["status"] == cli.STATUS["test"][code]
    project.edit("[agent]\nbogus = 1\n", "[agent]\n")
    checks = check_runs(project)
    for code, done in checks.items():
        assert done.code == code, done.out + done.err
        doc = one_document(done, "check")
        assert doc["status"] == cli.STATUS["check"][code]
    assert "the reader broke" in messages(checks[3].doc, "infra")[0]
    assert checks[0].doc["summary"] == {
        "tests": 1,
        "rows": 2,
        "runs": 2,  # the exit 3 fixture left runs = 1
        "tools": 4,
        "budget_usd": 5.0,
    }
    assert checks[2].doc["summary"] is None


@pytest.mark.design("json.version")
@pytest.mark.slow
def test_the_envelope_names_the_installed_version(project: Project) -> None:
    doc = one_document(scrimmage("check", "--json", str(project.toml)), "check")
    assert doc["scrimmage"] == importlib.metadata.version("scrimmage-core")
    assert doc["v"] == 1 and doc["config"] == str(project.toml)


@pytest.mark.slow
def test_scrim_is_the_same_command_under_a_short_name(project: Project) -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    assert scripts["scrim"].value == scripts["scrimmage"].value
    doc = one_document(scrimmage("check", "--json", str(project.toml), command=SCRIM), "check")
    assert doc["exit"] == 0


@pytest.mark.design("json.message_objects")
@pytest.mark.slow
def test_every_message_is_an_object_with_its_text(project: Project, fake_openai: Fake) -> None:
    done = runs_of_test(project, fake_openai)
    seen = 0
    for result in done.values():
        doc = result.doc
        for key in LISTS:
            for item in cast(list[object], doc[key]):
                assert isinstance(item, dict) and set(item) == {"message"}
                assert isinstance(cast(dict[str, object], item)["message"], str)
                seen += 1
    assert seen >= 3
    assert any("bogus: unknown setting" in m for m in messages(done[2].doc, "mistakes"))
    assert messages(done[3].doc, "infra")


CALL_THEN_HANG = '''"""Makes one model call, then hangs, so a signal comes after money was spent."""

import time
from pathlib import Path

import openai


def answer(question):
    openai.OpenAI(max_retries=0).chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": question}]
    )
    Path("called").touch()  # the call has returned: its cost is on the record's line
    time.sleep(600)
    return "never"
'''


@pytest.fixture
def leftovers(project: Project) -> Iterator[list[subprocess.Popen[str]]]:
    """What a test started, stopped at its end even when it fails.

    A test whose signal was lost would leave scrimmage test, and the agent under it in its
    600 s sleep, running long after. The agent runs in the project's folder, so it is
    found there; scrimmage test is the process the test adds to the list.
    """
    started: list[subprocess.Popen[str]] = []
    yield started
    for proc in started:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    root = project.root.resolve()
    for entry_ in Path("/proc").iterdir():
        if entry_.name.isdigit():
            with contextlib.suppress(OSError):
                if (entry_ / "cwd").resolve() == root:
                    os.kill(int(entry_.name), signal.SIGKILL)


@pytest.mark.design("json.interrupted")
@pytest.mark.slow
def test_sigint_during_a_test_still_prints_one_document(
    project: Project, fake_openai: Fake, leftovers: list[subprocess.Popen[str]]
) -> None:
    (project.root / "acme" / "call_then_hang.py").write_text(CALL_THEN_HANG)
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.call_then_hang:answer"\nruns = 1\nrun_timeout = 120',
    )
    project.edit(project.toml.read_text().split("[[test.fault]]")[1], "")
    project.edit("[[test.fault]]", "")
    fake = fake_openai(["chat_answer"])
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    proc = subprocess.Popen(
        [SCRIMMAGE, "test", "--json", str(project.toml)],
        env={**env, **model_env(fake)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    leftovers.append(proc)
    wait_for(project.root / "called")  # the agent made its call and is in its sleep
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=60)
    done = Done(proc.returncode, out, err)
    assert done.code == 3, out + err
    doc = one_document(done, "test")
    assert doc["status"] == "infra"
    assert messages(doc, "infra") == ["stopped by SIGINT; every run was stopped"]
    assert doc["records"] is not None and doc["rows"] == []
    # The money already spent is not lost because the test was stopped.
    assert cast(float, doc["cost_usd"]) > 0, doc["cost_usd"]


def test_ctrl_c_outside_the_runs_is_still_one_document(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(path: Path) -> config.Config:
        raise KeyboardInterrupt

    monkeypatch.setattr(config, "load", interrupted)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    validate(doc)
    assert captured.err == "" and messages(doc, "infra") == ["stopped by SIGINT"]


def test_a_failure_of_scrimmage_itself_is_still_one_document(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(self: Runner) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(Runner, "run_all", broken)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    doc = json.loads(capsys.readouterr().out)
    validate(doc)
    assert "no space left on device" in messages(doc, "infra")[0]
    assert doc["records"] is not None and doc["cost_usd"] == 0.0
    assert doc["tests"] == 1 and doc["budget_usd"] == 5.0

    def report_broke(*args: object) -> None:
        raise ValueError("the report broke")

    monkeypatch.setattr(Runner, "run_all", lambda self: [])
    monkeypatch.setattr(runner, "build_report", report_broke)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    doc = json.loads(capsys.readouterr().out)
    validate(doc)
    assert "the report broke" in messages(doc, "infra")[0]
    # The runner had started, so the document still names its records and the test.
    assert str(doc["records"]).startswith(str(project.root / "scrimmage-runs"))
    assert doc["tests"] == 1 and doc["cost_usd"] == 0.0


def test_a_usage_error_stays_text_even_with_json(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main(["test", "--json", "--nope"])
    assert stop.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "unrecognized arguments: --nope" in captured.err


def enums(schema: object) -> dict[str, list[object]]:
    """Every enum in the schema, by the name of the property or definition that holds it."""
    found: dict[str, list[object]] = {}

    def walk(node: object, name: str) -> None:
        if isinstance(node, dict):
            table = cast(dict[str, object], node)
            if "enum" in table:
                found.setdefault(name, []).extend(cast(list[object], table["enum"]))
            for key, value in table.items():
                inner = name if key in ("enum", "items", "anyOf", "oneOf", "allOf") else key
                walk(value, inner)
        elif isinstance(node, list):
            for item in cast(list[object], node):
                walk(item, name)

    walk(schema, "")
    return found


def constants_assigned(path: Path, function: str, variable: str) -> set[str]:
    """The text constants a function assigns to one variable."""
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Constant):
                    names = {t.id for t in inner.targets if isinstance(t, ast.Name)}
                    if variable in names:
                        out.add(str(inner.value.value))
    return out


def stopped_by_values(path: Path) -> set[str]:
    """Every constant the runner assigns to self.stopped_by, a module constant read by name.

    A local name, such as a refusal's reason, is left out: the runner keeps only reasons
    in STOP_TEST there, which the schema's list is held to as a whole.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Attribute) and t.attr == "stopped_by" for t in node.targets):
            continue
        for inner in ast.walk(node.value):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                found.add(inner.value)
            elif isinstance(inner, ast.Name) and isinstance(getattr(runner, inner.id, None), str):
                found.add(str(getattr(runner, inner.id)))
    return found


def broken_kinds(path: Path) -> set[str]:
    """The first argument of every Broken(...) the judge builds with a constant."""
    tree = ast.parse(path.read_text())
    return {
        str(node.args[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "Broken"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


@pytest.mark.design("json.enums_listed")
def test_each_enum_lists_every_value_the_code_gives() -> None:
    found = enums(CLI_SCHEMA)
    defs = cast(dict[str, dict[str, object]], CLI_SCHEMA["$defs"])
    for command, words in cli.STATUS.items():
        status = cast(dict[str, object], defs[command]["properties"])["status"]
        assert enums(status) == {"": list(words.values())}, command
    assert found["command"] == [*cli.STATUS, *cli.STATUS]  # each command's own, then the envelope's
    assert found["result"] == list(judge.VERDICTS)
    assert found["judged"] == list(judge.RUN_VERDICTS)
    assert found["outcome"] == list(runner.OUTCOMES)
    assert found["kind"] == list(judge.KINDS)
    assert found["stopped_by"] == list(runner.STOPPED_BY)
    assert set(found["code"]) == set(record.MANUAL_CODES) | set(record.SKIPPED_CODES)
    assert set(found["rule"]) == {capture.PATTERN, capture.KEY_NAME}
    source_record = Path(record.__file__).read_text()
    for code in (*record.MANUAL_CODES, *record.SKIPPED_CODES):
        assert f'"{code}"' in source_record, code
    assert found["found_by"] == [*init_module.FOUND_BY] * 3  # a tool's, an adapter tool's, a host's
    assert found["problem"] == list(init_module.PROBLEMS)
    source = Path(runner.__file__)
    outcomes = constants_assigned(source, "one", "outcome")
    assert outcomes and outcomes <= set(runner.OUTCOMES)  # an empty lookup proves nothing
    reasons = stopped_by_values(source)
    assert runner.UNKNOWN_LABEL in reasons and reasons <= set(runner.STOPPED_BY), reasons
    assert broken_kinds(Path(judge.__file__)) <= set(judge.KINDS)
    assert set(judge.ENDED_BADLY) | {judge.REFUSED} <= set(judge.KINDS)
    assert set(runner.STOP_TEST) | {runner.UNKNOWN_LABEL} == set(runner.STOPPED_BY)


def fault_cell(fault: dict[str, object] | None) -> str:
    if fault is None:
        return "-"
    target = str(fault["call"]) if fault["model"] is None else f"{fault['call']} {fault['model']}"
    when = "" if fault["when"] == "always" else f" {fault['when']}"
    return f"{target} {fault['do']}{when}"


def cells(row: dict[str, object]) -> list[str]:
    """A JSON row printed as the table prints its row."""
    rate, span = "-", "-"
    if row["rate"] is not None:
        low, high = cast(list[float], row["range"])
        rate, span = f"{cast(float, row['rate']):.0%}", f"{low * 100:.0f}-{high:.0%}"
    return [
        str(row["test"]),
        str(row["row"]),
        fault_cell(cast(dict[str, object] | None, row["fault"])),
        str(row["runs"]),
        "-" if row["fired"] is None else str(row["fired"]),
        str(row["done"]),
        f"{row['passed']}/{row['judged']}",
        rate,
        span,
        f"{cast(float, row['tools']):.1f}",
        f"{cast(float, row['cost_usd']):.5f}",
        str(row["result"]),
    ]


def broken_line(broken: dict[str, object], folder: Path) -> str:
    runs = cast(list[str], broken["runs"])
    logs = broken["kind"] in judge.ENDED_BADLY
    names = [str(folder / f"{r}.log") if logs else r for r in runs[: runner.SHOWN_RUNS]]
    more = f" and {len(runs) - runner.SHOWN_RUNS} more" if len(runs) > runner.SHOWN_RUNS else ""
    return f"  {outcomes.plural(len(runs), 'run')}: {broken['text']}: {', '.join(names)}{more}"


def rule_head(row: dict[str, object]) -> str:
    """The line above a row's broken rules, as the text prints it."""
    where = f"{row['test']} {row['row']}"
    broken = cast(list[dict[str, object]], row["broken"])
    refused = [str(b["text"]) for b in broken if b["kind"] == judge.REFUSED]
    if refused:
        call = refused[0].removeprefix(f"{judge.REFUSED} ")
        advice = (
            "See the line below" if call == runner.PROCESS_LABEL else "Mocking it would hide this"
        )
        return (
            f"fail: {where}: under this fault the agent called {call}, which the test does not "
            f"allow. {advice}"
        )
    word = "fail" if row["result"] == judge.FAIL else "note"
    rate = cast(float, row["pass_rate"])
    return f"{word}: {where}: {row['passed']}/{row['judged']} passed, pass_rate is {rate:g}"


def text_head(doc: dict[str, object], folder: Path) -> list[str]:
    """Every line the text prints through its cost line, rebuilt from the JSON alone."""
    rows = cast(list[dict[str, object]], doc["rows"])
    runs = sum(cast(int, row["runs"]) for row in rows)
    tests = cast(int, doc["tests"])
    lines = [
        f"scrimmage test: {outcomes.plural(tests, 'test')}, {outcomes.plural(len(rows), 'row')}, "
        f"{outcomes.plural(runs, 'run')}; records in {doc['records']}"
    ]
    warnings = messages(doc, "warnings")
    if warnings[:1] == [runner.TOOL_STAR_LINE]:  # the one warning said above the table
        lines.append(f"warning: {runner.TOOL_STAR_LINE}")
    table = [runner.HEAD, *(cells(row) for row in rows)]
    widths = runner.fitted(table)
    lines += [
        "  ".join(runner.cut(c, w).ljust(w) for c, w in zip(line, widths, strict=True)).rstrip()
        for line in table
    ]
    for row, line in zip(rows, table[1:], strict=True):
        test, fault = runner.TEST_COLUMN, runner.FAULT_COLUMN
        if len(line[test]) > widths[test] or len(line[fault]) > widths[fault]:
            lines.append(f"full name: {row['test']} {row['row']}: {line[fault]}")
    lines.append("range: a 95% Wilson interval, from these runs alone")
    for row in rows:
        broken = cast(list[dict[str, object]], row["broken"])
        if broken:
            lines += [rule_head(row), *(broken_line(b, folder) for b in broken)]
    budget = doc["budget_usd"]
    limit = "no budget" if budget is None else f"a budget of ${cast(float, budget):g}"
    lines.append(f"model calls cost ${cast(float, doc['cost_usd']):.4f} of {limit}")
    return lines


def assert_same(text: str, doc: dict[str, object], table: list[list[str]], folder: Path) -> None:
    """The JSON says every line the text says, and the text says nothing the JSON does not.

    Up to the cost line the text is rebuilt from the JSON, line for line. After it, every
    line must carry a list's prefix, and each list must hold those lines in their order.
    """
    rows = cast(list[dict[str, object]], doc["rows"])
    assert [cells(row) for row in rows] == table[1:]
    lines = text.splitlines()
    head = text_head(doc, folder)
    assert lines[: len(head)] == head
    rest = lines[len(head) :]
    above = 1 if f"warning: {runner.TOOL_STAR_LINE}" in head else 0
    for line in rest:
        assert line.startswith(tuple(PREFIXES.values())), f"a line with no list: {line!r}"
    for key, prefix in PREFIXES.items():
        printed = [line.removeprefix(prefix) for line in rest if line.startswith(prefix)]
        listed = messages(doc, key)[above:] if key == "warnings" else messages(doc, key)
        assert listed == printed, key
    for row in rows:
        assert len(cast(list[object], row["run_details"])) == row["runs"]


def render(case_config: config.Config, results: list[runner.RunResult], folder: Path, run: object):
    rep = build_report(case_config, results, folder, run)  # type: ignore[arg-type]
    out = io.StringIO()
    code = print_report(rep, out)
    found = rep.every_message
    doc = cli.document("test", code, "scrimmage.toml", found, "0.0.0", **report_json(rep))
    validate(doc)
    return out.getvalue(), doc, rep


def e2e(project: Project, fake: FakeOpenAI) -> tuple[str, dict[str, object]]:
    project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    cfg = config.load(project.toml)
    run = Runner(cfg)
    try:
        results = run.run_all()
    finally:
        run.server.server.server_close()
    text, doc, rep = render(cfg, results, run.folder, run)
    assert_same(text, doc, rep.table, run.folder)
    return text, doc


@pytest.mark.design("json.same_as_table")
@pytest.mark.slow
def test_the_json_rows_and_lists_say_what_the_table_says(
    project: Project, fake_openai: Fake
) -> None:
    for name, make in CASES.items():
        case = make()
        text, doc, rep = render(case.config, case.results, FOLDER, case.runner)
        assert_same(text, doc, rep.table, FOLDER)
        assert doc["stopped_by"] == (case.runner.stopped_by if case.runner else None)
        assert f"model calls cost ${doc['cost_usd']:.4f} of " in text
        assert doc["exit"] == int((GOLDEN / f"{name}.txt").read_text().split()[1])
    # J1: the example's rules and its fault row, end to end.
    _, doc = e2e(project, fake_openai(ACME))
    assert doc["exit"] == 0 and len(cast(list[object], doc["rows"])) == 2
    # J2: a fault on one model name, which fires on the first request only.
    project.toml.write_text(
        project.toml.read_text() + '\n[[test.fault]]\ncall = "model"\ndo = "status 429"\n'
        'model = "gpt-4o-mini"\nwhen = "first 1"\nforbid = ["never said"]\n'
    )
    _, doc = e2e(project, fake_openai(ACME))
    fault = cast(list[dict[str, object]], doc["rows"])[2]["fault"]
    assert fault == {
        "call": "model",
        "do": "status 429",
        "when": "first 1",
        "model": "gpt-4o-mini",
        "method": None,
        "path": None,
        "raises": None,
    }
    # T1: tools that are methods on a class.
    project.edit('function = "acme.agent:answer"', 'function = "acme.toolkit:answer"')
    project.edit(
        'tools    = ["acme.tools:get_order", "acme.tools:check_inventory", '
        '"acme.tools:refund_order", "acme.tools:get_stock"]',
        'tools    = ["acme.toolkit:StoreTools.get_order", '
        '"acme.toolkit:StoreTools.check_inventory", "acme.toolkit:StoreTools.refund_order", '
        '"acme.toolkit:StoreTools.get_stock"]',
    )
    _, doc = e2e(project, fake_openai(ACME))
    assert doc["exit"] in (0, 1), doc


def test_a_row_carries_its_runs_and_the_rules_they_broke() -> None:
    case = CASES["failing"]()
    _, doc, _ = render(case.config, case.results, FOLDER, case.runner)
    base = cast(list[dict[str, object]], doc["rows"])[0]
    assert base["rate"] == 0.3333 and base["range"] == [0.0615, 0.7923]
    assert base["pass_rate"] == 1.0 and base["result"] == "fail"
    broken = cast(list[dict[str, object]], base["broken"])
    assert broken[0] == {
        "kind": "expect",
        "text": 'expect "shipped" not found',
        "runs": ["run-baseline-1"],
    }
    details = cast(list[dict[str, object]], base["run_details"])
    assert details[0] == {
        "run": "run-baseline-0",
        "outcome": "answered",
        "fault_fired": None,
        "judged": "pass",
        "left_out": None,
        "cost_usd": 0.00012,
        "answer_preview": "It shipped, and arrives on Oct 30.",
        "mock_miss_count": 0,
    }
    assert doc["records"] == str(FOLDER) and doc["stopped_by"] is None
    assert doc["budget_usd"] == 5.0 and doc["tests"] == 1


def test_an_answer_preview_has_no_secret_and_is_cut() -> None:
    case = CASES["passing"]()
    secret = "sk-" + "a1b2c3d4" * 3
    case.results[0].answer = f"key {secret} " + "x" * 400
    _, doc, _ = render(case.config, case.results, FOLDER, case.runner)
    detail = cast(
        list[dict[str, object]], cast(list[dict[str, object]], doc["rows"])[0]["run_details"]
    )[0]
    preview = str(detail["answer_preview"])
    assert secret not in preview and len(preview) == 200


def test_the_most_broken_rule_comes_first() -> None:
    case = CASES["most_broken"]()
    text, doc, _ = render(case.config, case.results, FOLDER, case.runner)
    broken = cast(list[dict[str, object]], cast(list[dict[str, object]], doc["rows"])[0]["broken"])
    assert [(b["text"], b["runs"]) for b in broken] == [
        ('expect "shipped" not found', ["run-baseline-1", "run-baseline-2"]),
        ('expect "Oct 30" or "October 30" not found', ["run-baseline-0"]),
    ]
    assert text.index('2 runs: expect "shipped"') < text.index("1 run: expect")


def test_a_fault_row_names_its_method_path_and_raises() -> None:
    case = CASES["never_fired"]()
    _, doc, _ = render(case.config, case.results, FOLDER, case.runner)
    fault = cast(list[dict[str, object]], doc["rows"])[1]["fault"]
    assert fault == {
        "call": "http:orders.acme.internal",
        "do": "status 503",
        "when": "always",
        "model": None,
        "method": None,
        "path": None,
        "raises": None,
    }


@pytest.mark.design("json.open_schema")
def test_the_shipped_schema_takes_what_a_later_release_adds() -> None:
    case = CASES["passing"]()
    _, doc, _ = render(case.config, case.results, FOLDER, case.runner)
    later = json.loads(json.dumps(doc))
    later["trace"] = "a field a later release adds"
    later["status"] = "flaky"
    later["notes"] = [{"message": "a note", "code": "N001"}]
    cast(list[dict[str, object]], later["rows"])[0]["outcome_counts"] = {"answered": 3}
    jsonschema.Draft202012Validator(CLI_SCHEMA).validate(later)
    record = {**doc, "command": "later", "out": "scrimmage.recorded.toml"}
    jsonschema.Draft202012Validator(CLI_SCHEMA).validate(record)  # a command it does not know
    closed = jsonschema.Draft202012Validator(closed_schema())
    for changed in (later, record, {**doc, "status": "flaky"}, {**doc, "extra": 1}):
        assert list(closed.iter_errors(changed)), "the closed schema must catch what we add"


@pytest.mark.design("json.version")
def test_the_version_comes_from_the_package_or_is_unknown(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: f"sentinel-{name}")
    assert cli.main(["check", "--json", str(project.toml)]) == 0
    assert json.loads(capsys.readouterr().out)["scrimmage"] == "sentinel-scrimmage-core"

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert cli.main(["check", "--json", str(project.toml)]) == 0
    doc = json.loads(capsys.readouterr().out)
    validate(doc)
    assert doc["scrimmage"] == "unknown"


def test_a_document_that_cannot_be_written_still_gives_one(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Runner, "run_all", lambda self: [])
    monkeypatch.setattr(runner, "report_json", lambda report: {"rows": object()})
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    validate(doc)
    assert captured.err == ""
    assert "could not write its document: TypeError" in messages(doc, "infra")[0]


@pytest.mark.design("json.sigterm")
def test_sigterm_while_the_file_is_read_still_gives_one_document(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_load = config.load
    handlers: list[object] = []

    def terminated(path: Path) -> config.Config:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.1)  # the handler has run: it noted the signal and raised nothing
        handlers.append(signal.getsignal(signal.SIGTERM))
        return real_load(path)

    # Handlers no test could have left: so a handler a command leaves set cannot pass.
    marker_int, marker_term = signal.default_int_handler, signal.SIG_DFL
    monkeypatch.setattr(config, "load", terminated)
    for command in ("test", "check"):
        signal.signal(signal.SIGINT, marker_int)
        signal.signal(signal.SIGTERM, marker_term)
        assert cli.main([command, "--json", str(project.toml)]) == 3
        doc = json.loads(capsys.readouterr().out)
        validate(doc)
        assert messages(doc, "infra") == ["stopped by SIGTERM"], command
        assert signal.getsignal(signal.SIGINT) is marker_int, command  # put back after it
        assert signal.getsignal(signal.SIGTERM) is marker_term, command
    # After the first signal the handler was still set, not SIG_IGN, so a second would count.
    assert handlers == [cli.stop, cli.stop]


# Sends SIGTERM from inside a __del__ and spins there, so the handler runs inside it, where
# Python prints and drops any exception it raises. In cli: as the file is read.
SIGNAL_IN_DEL = """
import gc, os, signal

class Bomb:
    def __del__(self):
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(200_000):  # the handler runs here, inside __del__
            pass

def go_off():
    Bomb()
    gc.collect()
"""
IN_READ = (
    SIGNAL_IN_DEL
    + """
from scrimmage_core import config
real_load = config.load
def load(path):
    go_off()
    return real_load(path)
config.load = load
"""
)


def wait_for(path: Path) -> None:
    """Wait for the agent's marker; the ceiling only ends a test whose agent never got there."""
    ceiling = time.monotonic() + 120
    while not path.exists():
        assert time.monotonic() < ceiling, f"{path.name} never appeared"
        time.sleep(0.01)


def hang(project: Project, timeout: int = 600) -> None:
    """The example's agent swapped for one that sleeps, one baseline run, no fault.

    The time limit is long, so a signal that was lost shows as a run that went on: its
    process outlives the test's own 120 s wait for it.
    """
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "acme.slow:hang"\nruns = 1\nrun_timeout = {timeout}\nmax_cost_usd = "none"',
    )
    project.edit(project.toml.read_text().split("[[test.fault]]")[1], "")
    project.edit("[[test.fault]]", "")


@pytest.mark.design("json.signal_in_cleanup")
@pytest.mark.parametrize("command", ["check", "test"])
@pytest.mark.slow
def test_a_signal_inside_a_del_while_the_file_is_read_still_stops(
    project: Project, command: str, leftovers: list[subprocess.Popen[str]]
) -> None:
    hang(project)
    done = run_entry(IN_READ, command, "--json", str(project.toml))
    assert done.code == 3, done.out + done.err
    doc = one_document(done, command)  # standard error is empty: nothing was dropped
    assert messages(doc, "infra") == ["stopped by SIGTERM"]
    assert not (project.root / "scrimmage-runs").exists()  # it stopped before a runner was made


IN_RUNNER = (
    SIGNAL_IN_DEL
    + """
from scrimmage_core import runner
real_init = runner.Runner.__init__
def init(self, *args, **kwargs):
    real_init(self, *args, **kwargs)
    go_off()  # the runner exists and its folder is made; its runs have not started
runner.Runner.__init__ = init
"""
)


@pytest.mark.design("json.signal_in_cleanup")
@pytest.mark.slow
def test_a_signal_as_the_runner_is_made_stops_before_any_run(
    project: Project, leftovers: list[subprocess.Popen[str]]
) -> None:
    hang(project)
    done = run_entry(IN_RUNNER, "test", "--json", str(project.toml))
    assert done.code == 3, done.out + done.err
    doc = one_document(done, "test")
    assert messages(doc, "infra") == ["stopped by SIGTERM"]  # not "every run was stopped"
    assert doc["records"] is not None
    assert not list((project.root / "scrimmage-runs").glob("*/*.log")), "a run was started"


# Blocks the stop signals in the main thread once the pool exists, so the kernel must give
# a signal to another thread, as it may at any time under load.
MAIN_THREAD_DEAF = """
import signal
from scrimmage_core import runner
real_pool = runner.ThreadPoolExecutor
def pool(*args, **kwargs):
    made = real_pool(*args, **kwargs)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    return made
runner.ThreadPoolExecutor = pool
"""


@pytest.mark.design("json.stops_fast")
@pytest.mark.slow
def test_a_signal_on_another_thread_still_stops_the_runs(
    project: Project, leftovers: list[subprocess.Popen[str]]
) -> None:
    hang(project)
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    code = f"import sys\nsys.argv = ['scrimmage', 'test', '--json', {str(project.toml)!r}]\n"
    code += MAIN_THREAD_DEAF + "from scrimmage_core import entry\nentry.main()\n"
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        env=clean, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    leftovers.append(proc)
    wait_for(project.root / "hanging")  # the agent is in its sleep, inside the runner's wait
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=120)  # had the runs waited, they would run 600 s
    done = Done(proc.returncode, out, err)
    assert done.code == 3, out + err
    assert messages(one_document(done, "test"), "infra") == [
        "stopped by SIGTERM; every run was stopped"
    ]


RUNNER_DEL = """
import gc, os, signal, threading, time

class Bomb:
    def __del__(self):
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(200_000):  # the runner's handler runs here, inside __del__
            pass

from scrimmage_core import runner
real_pool = runner.ThreadPoolExecutor
def pool(*args, **kwargs):
    Bomb()  # made and dropped on the main thread, with the runner's handlers set
    gc.collect()
    return real_pool(*args, **kwargs)
runner.ThreadPoolExecutor = pool
"""


@pytest.mark.design("json.signal_in_cleanup")
@pytest.mark.slow
def test_a_signal_inside_a_del_while_runs_go_still_stops_them(
    project: Project, leftovers: list[subprocess.Popen[str]]
) -> None:
    hang(project)
    done = run_entry(RUNNER_DEL, "test", "--json", str(project.toml))
    assert done.code == 3, done.out + done.err
    doc = one_document(done, "test")
    assert messages(doc, "infra") == ["stopped by SIGTERM; every run was stopped"]


@pytest.mark.design("json.stops_fast")
@pytest.mark.slow
def test_a_long_run_stops_at_either_of_two_signals(
    project: Project, leftovers: list[subprocess.Popen[str]]
) -> None:
    hang(project)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    proc = subprocess.Popen(
        [SCRIMMAGE, "test", "--json", str(project.toml)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    leftovers.append(proc)
    wait_for(project.root / "hanging")  # the agent is in its sleep, inside the runner's wait
    proc.send_signal(signal.SIGTERM)
    proc.send_signal(signal.SIGINT)  # a second signal changes nothing and is not lost
    out, err = proc.communicate(timeout=120)  # had both been lost, the run would go 600 s
    done = Done(proc.returncode, out, err)
    assert done.code == 3, out + err
    doc = one_document(done, "test")
    # Two signals pending at once come lowest number first, so either can be the first.
    assert messages(doc, "infra") in (
        ["stopped by SIGTERM; every run was stopped"],
        ["stopped by SIGINT; every run was stopped"],
    )


@pytest.mark.design("cli.no_abbreviations")
@pytest.mark.parametrize("flag", ["--js", "--jso"])
def test_a_shortened_json_flag_is_a_usage_error(
    project: Project, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in ("check", "test"):
        with pytest.raises(SystemExit) as stop:
            cli.main([command, flag, str(project.toml)])
        assert stop.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == "" and f"unrecognized arguments: {flag}" in captured.err


def run_entry(script: str, *argv: str) -> Done:
    """scrimmage's entry point in its own process, after a script that sets up a hook."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    code = f"import sys\nsys.argv = ['scrimmage', *{list(argv)!r}]\n{script}\n"
    code += "from scrimmage_core import entry\nentry.main()\n"
    proc = subprocess.run(
        [sys.executable, "-c", code], env=clean, capture_output=True, text=True, timeout=120
    )
    return Done(proc.returncode, proc.stdout, proc.stderr)


# Sends the signal as a Scrimmage module is imported, so it lands while Scrimmage loads. The
# cli module is the window before cli can set its own handlers. The runner is imported late,
# when a test starts, with the handlers set.
SIGNAL_WHILE_LOADING = """
import importlib.abc, os, signal, time

class Signal(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "{module}":
            sys.meta_path.remove(self)
            os.kill(os.getpid(), signal.{sig})
            time.sleep(0.2)
        return None

sys.meta_path.insert(0, Signal())
"""
# Sends both signals just after the document is written, before the process exits.
SIGNAL_AFTER_PRINT = """
import io, os, signal, time

class Out(io.TextIOWrapper):
    def write(self, text):
        written = super().write(text)
        if text.rstrip().endswith("}"):
            self.flush()
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)
        return written

sys.stdout = Out(sys.stdout.detach(), encoding="utf-8", line_buffering=False)
"""


@pytest.mark.design("json.sigterm")
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
@pytest.mark.parametrize(
    ("command", "module"),
    [
        ("check", "scrimmage_core.cli"),
        ("test", "scrimmage_core.cli"),
        ("test", "scrimmage_core.runner"),
    ],
)
@pytest.mark.slow
def test_a_signal_while_scrimmage_loads_still_gives_one_document(
    project: Project, sig: str, command: str, module: str
) -> None:
    script = SIGNAL_WHILE_LOADING.format(sig=sig, module=module)
    done = run_entry(script, command, "--json", str(project.toml))
    assert done.code == 3, done.out + done.err
    doc = one_document(done, command)
    assert messages(doc, "infra") == [f"stopped by {sig}"]
    assert not (project.root / "scrimmage-runs").exists()  # it stopped before any run


@pytest.mark.design("json.exit_matches")
@pytest.mark.slow
def test_a_signal_after_the_document_does_not_change_the_exit(project: Project) -> None:
    good = run_entry(SIGNAL_AFTER_PRINT, "check", "--json", str(project.toml))
    assert (good.code, one_document(good, "check")["exit"]) == (0, 0), good.err
    project.edit("[agent]\n", "[agent]\nbogus = 1\n")
    bad = run_entry(SIGNAL_AFTER_PRINT, "check", "--json", str(project.toml))
    assert (bad.code, one_document(bad, "check")["exit"]) == (2, 2), bad.err
    # The scrimmage process itself keeps ignoring signals after main returns, until it exits.
    late = subprocess.run(
        [sys.executable, "-c", SIGNAL_AFTER_MAIN, "check", "--json", str(project.toml)],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert (late.returncode, json.loads(late.stdout)["exit"]) == (2, 2), late.stderr


# Sends SIGTERM after cli.main has returned, while the process lingers before it exits.
SIGNAL_AFTER_MAIN = """
import os, signal, sys, time
from scrimmage_core import cli
code = cli.main(sys.argv[1:], process=True)
os.kill(os.getpid(), signal.SIGTERM)
time.sleep(0.3)
sys.exit(code)
"""


def test_the_entry_point_without_json_sets_no_handler(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    monkeypatch.setattr(sys, "argv", ["scrimmage", "check", str(project.toml)])
    with pytest.raises(SystemExit) as end:
        entry.main()
    assert end.value.code == 0 and "is valid" in capsys.readouterr().out
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before


@pytest.mark.design("json.tool_star_warning")
@pytest.mark.slow
def test_the_tool_star_line_is_the_first_warning_end_to_end(
    project: Project, fake_openai: Fake
) -> None:
    project.edit('real     = ["tool:check_inventory"]', 'real     = ["tool:*"]')
    done = scrimmage("test", "--json", str(project.toml), env=model_env(fake_openai(ACME)))
    doc = one_document(done, "test")
    assert messages(doc, "warnings")[0] == runner.TOOL_STAR_LINE


NESTED = "[agent]\nfunction = 'acme.agent:answer'\nx = " + "[" * 5000 + "]" * 5000 + "\n"


@pytest.mark.design("cli.read_crash")
def test_a_crash_while_reading_the_file_is_exit_3_with_or_without_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "scrimmage.toml"
    path.write_text(NESTED)
    codes: dict[str, int] = {}
    for command in ("test", "check"):
        codes[command] = cli.main([command, str(path)])
        captured = capsys.readouterr()
        if codes[command] == 3:  # a parser that recurses runs out of stack: Scrimmage's failure
            assert f"scrimmage: infrastructure error in scrimmage {command}: RecursionError" in (
                captured.err
            )
            assert captured.out.endswith("; nothing was run\n")
        else:  # a parser with a depth limit calls it a mistake in the file
            assert codes[command] == 2 and "scrimmage: scrimmage.toml:" in captured.err
        assert cli.main([command, "--json", str(path)]) == codes[command]
        validate(json.loads(capsys.readouterr().out))
    assert codes["test"] == codes["check"]


# Calls that had a mock on their label and matched none.


HAND_ARGS = (
    '[[test.mock]]\ncall    = "tool:get_order"\nargs    = {order_id = "5678"}\n'
    'returns = "shipped, Oct 30"\n'
)
ORDERS = '[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n'


@contextlib.contextmanager
def orders_system(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A real orders system on localhost, which the example's get_order asks."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.recv(1024)
                conn.sendall(b"shipped, Oct 30")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv("ACME_ORDERS", f"127.0.0.1:{server.getsockname()[1]}")
    try:
        yield
    finally:
        stop.set()
        server.close()
        thread.join(timeout=10)


def missing(project: Project, real: str, fake: FakeOpenAI) -> tuple[str, dict[str, object]]:
    project.edit(
        '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'
        'expect = [["unable", "sorry", "try again"]]\nforbid = ["Oct 30", "October 30"]\n',
        "",
    )
    project.edit(ORDERS, HAND_ARGS)
    project.edit('real     = ["tool:check_inventory"]', f"real     = {real}")
    return e2e(project, fake)


@pytest.mark.design("json.mock_misses")
@pytest.mark.slow
def test_a_miss_on_a_listed_tool_warns_and_the_test_still_passes(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    with orders_system(monkeypatch):
        text, doc = missing(project, '["tool:get_order"]', fake_openai(ACME))
    assert (doc["exit"], doc["status"]) == (0, "passed")
    [miss] = cast(list[dict[str, Any]], doc["mock_misses"])
    assert (miss["call"], miss["reason"], miss["action"]) == ("tool:get_order", "arguments", "real")
    assert miss["seen"] == '{"order_id": "1234"}' and miss["runs"] == 3
    assert miss["nearest"]["args"] == '{"order_id": "5678"}' and miss["nearest"]["mock"] == 1
    assert miss["nearest"]["differs"] == ["order_id"]
    assert doc["mock_misses_left_out"] == 0
    assert 'warning: tool:get_order with {"order_id": "1234"} matched no mock and ran for real' in (
        text
    )
    assert "heuristic: fewest differing arguments" in text


@pytest.mark.design("json.mock_misses")
@pytest.mark.slow
def test_a_miss_on_an_unlisted_tool_is_refused_with_its_evidence(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    with orders_system(monkeypatch):
        _, doc = missing(project, '["tool:check_inventory"]', fake_openai(ACME[:1]))
    assert doc["exit"] == 2
    [miss] = cast(list[dict[str, Any]], doc["mock_misses"])
    assert (miss["reason"], miss["action"], miss["nearest"]["differs"]) == (
        "arguments",
        "refused",
        ["order_id"],
    )
    mistakes = " ".join(m["message"] for m in cast(list[dict[str, str]], doc["mistakes"]))
    assert "none matches this call's arguments" in mistakes and "1234" not in mistakes


@pytest.mark.design("json.mock_misses")
@pytest.mark.slow
def test_the_miss_in_the_text_and_in_json_say_the_same(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    with orders_system(monkeypatch):
        text, doc = missing(project, '["tool:get_order"]', fake_openai(ACME))
    [miss] = cast(list[dict[str, Any]], doc["mock_misses"])
    [line] = [
        w for w in cast(list[dict[str, str]], doc["warnings"]) if "matched no mock" in w["message"]
    ]
    for shown in (miss["seen"], miss["nearest"]["args"], *miss["nearest"]["differs"]):
        assert shown in line["message"]
    assert f"({miss['test']} {miss['row']}, {miss['runs']} of 3 runs)" in line["message"]
    assert f"warning: {line['message']}" in text


def a_miss(
    call: str = "tool:get_order", seen: str = '{"order_id": "1"}', action: str = "real"
) -> dict[str, object]:
    return {
        "call": call,
        "action": action,
        "reason": "arguments",
        "seen": seen,
        "method": None,
        "path": None,
        "nearest": {
            "mock": 0,
            "args": '{"order_id": "2"}',
            "method": None,
            "path": None,
            "differs": ["order_id"],
            "differs_left_out": 0,
        },
    }


def passing_case() -> tuple[config.Config, list[runner.RunResult]]:
    case = CASES["passing"]()
    return case.config, case.results


@pytest.mark.design("json.mock_misses")
def test_misses_group_over_runs() -> None:
    cfg, results = passing_case()
    base = [r for r in results if r.row.fault is None]
    base[0].misses = [a_miss(), a_miss(seen='{"order_id": "3"}')]
    base[1].misses = [a_miss(), a_miss()]  # twice in one run counts one run
    base[2].misses = [a_miss(call="tool:get_stock")]
    _, doc, _ = render(cfg, results, FOLDER, None)
    groups = {
        (m["call"], m["seen"]): m["runs"] for m in cast(list[dict[str, Any]], doc["mock_misses"])
    }
    assert groups == {
        ("tool:get_order", '{"order_id": "1"}'): 2,
        ("tool:get_order", '{"order_id": "3"}'): 1,
        ("tool:get_stock", '{"order_id": "1"}'): 1,
    }
    first = cast(list[dict[str, Any]], doc["mock_misses"])[0]
    assert first["run"] == base[0].run and first["nearest"]["mock"] == 1


@pytest.mark.design("json.mock_misses")
def test_the_miss_list_is_cut_real_first_and_says_by_how_many() -> None:
    cfg, results = passing_case()
    run = next(r for r in results if r.row.fault is None)
    run.misses = [a_miss(seen=f'{{"order_id": "{i:04}"}}', action="refused") for i in range(150)]
    run.misses += [a_miss(seen=f'{{"order_id": "r{i:04}"}}') for i in range(60)]
    text, doc, _ = render(cfg, results, FOLDER, None)
    listed = cast(list[dict[str, Any]], doc["mock_misses"])
    assert len(listed) == 200 and doc["mock_misses_left_out"] == 10
    assert [m["action"] for m in listed[:60]] == ["real"] * 60  # real calls are kept first
    assert sum(m["action"] == "refused" for m in listed) == 140
    assert text.count("matched no mock") == 20 and "and 190 more; see --json" in text


@pytest.mark.design("json.mock_misses")
@pytest.mark.slow
def test_a_recording_run_has_no_mock_misses_field(project: Project, fake_openai: Fake) -> None:
    project.monkeypatch.setenv("OPENAI_BASE_URL", fake_openai(ACME).base_url)
    project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    done = scrimmage("record", str(project.toml), "--json", env=os.environ.copy(), cwd=project.root)
    doc = done.doc
    assert doc["command"] == "record" and "mock_misses" not in doc
    assert "mock_misses_left_out" not in doc


@pytest.mark.design("json.mock_misses")
def test_the_miss_fits_the_cli_schema() -> None:
    cfg, results = passing_case()
    results[0].misses = [a_miss(), {**a_miss(call="http:x.acme"), "reason": "request"}]
    _, doc, _ = render(cfg, results, FOLDER, None)  # render validates against both schemas
    assert len(cast(list[object], doc["mock_misses"])) == 2
    bad = {**doc, "mock_misses": [{"call": "tool:get_order"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(CLI_SCHEMA).validate(bad)


@pytest.mark.design("json.mock_misses")
def test_a_miss_counts_on_its_row_and_run_with_no_change_to_status_or_exit() -> None:
    cfg, results = passing_case()
    _, before, _ = render(cfg, results, FOLDER, None)
    results[0].misses = [a_miss(), a_miss(seen="{}")]
    _, after, _ = render(cfg, results, FOLDER, None)
    rows_before = cast(list[dict[str, Any]], before["rows"])
    rows_after = cast(list[dict[str, Any]], after["rows"])
    assert (rows_after[0]["mock_miss_count"], rows_before[0]["mock_miss_count"]) == (2, 0)
    assert rows_after[0]["run_details"][0]["mock_miss_count"] == 2
    assert (after["exit"], after["status"]) == (before["exit"], before["status"])
    for old, new in zip(rows_before, rows_after, strict=True):
        assert {k: v for k, v in new.items() if k not in ("mock_miss_count", "run_details")} == {
            k: v for k, v in old.items() if k not in ("mock_miss_count", "run_details")
        }
