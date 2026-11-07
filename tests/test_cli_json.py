"""--json: one document on standard output, one envelope, and the same report as the text."""

import ast
import importlib.metadata
import io
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import jsonschema
import pytest

import derail_sdk
from conftest import ACME, Project
from derail_sdk import cli, config, judge, runner
from derail_sdk.runner import Runner, build_report, print_report, report_json
from fakes.openai_server import FakeOpenAI
from report_cases import CASES, FOLDER, GOLDEN

Fake = Callable[..., FakeOpenAI]
SCHEMAS = Path(derail_sdk.__file__).parent / "schemas"
CLI_SCHEMA = json.loads((SCHEMAS / "cli.v1.json").read_text())
DERAIL = str(Path(sys.executable).parent / "derail")
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


def validator() -> jsonschema.Draft202012Validator:
    jsonschema.Draft202012Validator.check_schema(CLI_SCHEMA)
    return jsonschema.Draft202012Validator(CLI_SCHEMA)


def derail(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> Done:
    """The derail command, as a team runs it: its own process, its own streams."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DERAIL_")}
    proc = subprocess.run(
        [DERAIL, *args],
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
    validator().validate(doc)
    assert doc["command"] == command and doc["exit"] == done.code
    return doc


def messages(doc: dict[str, object], key: str) -> list[str]:
    items = cast(list[dict[str, object]], doc[key])
    return [str(item["message"]) for item in items]


def runs_of_test(project: Project, fake_openai: Fake) -> dict[int, Done]:
    """One `derail test --json` per exit code."""
    toml = str(project.toml)
    done = {0: derail("test", "--json", toml, env=model_env(fake_openai(ACME)))}
    project.edit('expect   = ["shipped", ', 'expect   = ["refunded", ')
    done[1] = derail("test", "--json", toml, env=model_env(fake_openai(ACME)))
    project.edit('expect   = ["refunded", ', 'expect   = ["shipped", ')
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    failing = fake_openai(["500:error_500"])
    done[3] = derail("test", "--json", toml, env=model_env(failing))
    project.edit("[agent]\n", "[agent]\nbogus = 1\n")
    done[2] = derail("test", "--json", toml)
    return done


def check_runs(project: Project) -> dict[int, Done]:
    """One `derail check --json` per exit code; 3 by making the file's reader raise."""
    toml = str(project.toml)
    done = {0: derail("check", "--json", toml)}
    broken = (
        "import sys; from derail_sdk import cli, config\n"
        "def boom(path): raise RuntimeError('the reader broke')\n"
        "config.load = boom\n"
        f"sys.exit(cli.main(['check', '--json', {toml!r}]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", broken], capture_output=True, text=True, check=False
    )
    done[3] = Done(proc.returncode, proc.stdout, proc.stderr)
    project.edit('name     = "order-status"', 'name     = ""')
    done[2] = derail("check", "--json", toml)
    return done


@pytest.mark.design("json.one_document")
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
def test_the_envelope_names_the_installed_version(project: Project) -> None:
    doc = one_document(derail("check", "--json", str(project.toml)), "check")
    assert doc["derail"] == importlib.metadata.version("derail")
    assert doc["v"] == 1 and doc["config"] == str(project.toml)


@pytest.mark.design("json.message_objects")
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


@pytest.mark.design("json.interrupted")
def test_sigint_during_a_test_still_prints_one_document(project: Project) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:hang"\nruns = 1\nrun_timeout = 120\nmax_cost_usd = "none"',
    )
    project.edit(project.toml.read_text().split("[[test.fault]]")[1], "")
    project.edit("[[test.fault]]", "")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DERAIL_")}
    proc = subprocess.Popen(
        [DERAIL, "test", "--json", str(project.toml)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    for _ in range(200):
        time.sleep(0.1)
        if list((project.root / "derail-runs").glob("*/*.log")):
            break
    time.sleep(0.5)  # the agent is in its sleep, inside the runner's wait
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=60)
    done = Done(proc.returncode, out, err)
    assert done.code == 3, out + err
    doc = one_document(done, "test")
    assert doc["status"] == "infra"
    assert messages(doc, "infra") == ["stopped by SIGINT; every run was stopped"]
    assert doc["records"] is not None and doc["rows"] == []


def test_ctrl_c_outside_the_runs_is_still_one_document(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(path: Path) -> config.Config:
        raise KeyboardInterrupt

    monkeypatch.setattr(config, "load", interrupted)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    validator().validate(doc)
    assert captured.err == "" and messages(doc, "infra") == ["stopped by SIGINT"]


def test_a_failure_of_derail_itself_is_still_one_document(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(self: Runner) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(Runner, "run_all", broken)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    doc = json.loads(capsys.readouterr().out)
    validator().validate(doc)
    assert "no space left on device" in messages(doc, "infra")[0]
    assert doc["records"] is not None and doc["cost_usd"] == 0.0

    def report_broke(*args: object) -> None:
        raise ValueError("the report broke")

    monkeypatch.setattr(Runner, "run_all", lambda self: [])
    monkeypatch.setattr(runner, "build_report", report_broke)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    doc = json.loads(capsys.readouterr().out)
    validator().validate(doc)
    assert "the report broke" in messages(doc, "infra")[0]


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
        status = cast(dict[str, dict[str, object]], defs[command]["properties"])["status"]
        assert status["enum"] == list(words.values()), command
    assert found["command"] == list(cli.STATUS)
    assert found["result"] == list(judge.VERDICTS)
    assert found["judged"] == list(judge.RUN_VERDICTS)
    assert found["outcome"] == list(runner.OUTCOMES)
    assert found["kind"] == list(judge.KINDS)
    assert found["stopped_by"] == list(runner.STOPPED_BY)
    source = Path(runner.__file__)
    assert constants_assigned(source, "one", "outcome") <= set(runner.OUTCOMES)
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
    return f"  {runner.plural(len(runs), 'run')}: {broken['text']}: {', '.join(names)}{more}"


def assert_same(text: str, doc: dict[str, object], table: list[list[str]], folder: Path) -> None:
    """The JSON rows and lists say what the text says."""
    rows = cast(list[dict[str, object]], doc["rows"])
    assert [cells(row) for row in rows] == table[1:]
    lines = text.splitlines()
    for key, prefix in PREFIXES.items():
        printed = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
        assert messages(doc, key) == printed, key
    indented = [line for line in lines if line.startswith("  ") and " run" in line]
    from_json = [
        broken_line(b, folder) for row in rows for b in cast(list[dict[str, object]], row["broken"])
    ]
    assert from_json == indented
    for row in rows:
        assert len(cast(list[object], row["run_details"])) == row["runs"]


def render(case_config: config.Config, results: list[runner.RunResult], folder: Path, run: object):
    rep = build_report(case_config, results, folder, run)  # type: ignore[arg-type]
    out = io.StringIO()
    code = print_report(rep, out)
    doc = cli.envelope("test", code, "derail.toml", rep.messages, **report_json(rep))
    validator().validate(doc)
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
    assert fault == {"call": "model", "do": "status 429", "when": "first 1", "model": "gpt-4o-mini"}
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
