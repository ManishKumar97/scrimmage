"""The user docs keep up with the code: every setting, fault and outcome has its place."""

import json
import re
from pathlib import Path
from typing import cast

import pytest

import scrimmage_core
from conftest import doc_pages
from scrimmage_core import cli, config, judge, mcp_gate, outcomes, record, render

DOCS = Path(__file__).parents[1] / "docs"
CLI_SCHEMA = json.loads(
    (Path(scrimmage_core.__file__).parent / "schemas" / "cli.v1.json").read_text()
)


def page(name: str) -> str:
    return (DOCS / name).read_text()


def mentioned(text: str, word: str) -> bool:
    return f"`{word}`" in text or f'`"{word}"`' in text


@pytest.mark.design("docs.every_setting")
def test_every_setting_is_documented() -> None:
    text = page("configuration.md")
    keys = (config.AGENT_KEYS | config.TEST_KEYS | config.MOCK_KEYS | config.MODEL_FAULT_KEYS) - {
        "mock",
        "fault",
    }
    missing = sorted(k for k in keys if not mentioned(text, k))
    assert not missing, f"settings with no entry in docs/configuration.md: {missing}"


@pytest.mark.design("docs.web_settings")
def test_every_web_setting_and_real_form_is_documented() -> None:
    text = page("configuration.md")
    keys = config.WEB_MOCK_KEYS | config.WEB_FAULT_KEYS
    missing = sorted(k for k in keys if not mentioned(text, k))
    assert not missing, f"web settings with no entry in docs/configuration.md: {missing}"
    web = page("web-requests.md")
    for form in ("`http:orders.acme.internal`", " POST`", " POST /refunds`"):
        assert form in web


@pytest.mark.design("docs.mcp")
def test_mcp_settings_faults_and_labels_are_documented() -> None:
    text = page("mcp.md")
    for word in ("timeout", "error", "empty", "mcp:docs/search", "mcp:search"):
        assert mentioned(text, word), word
    assert all(f"`{method}`" in text for method in mcp_gate.PASSES)
    assert "`mcp:<server>/<tool>`" in page("concepts.md")
    assert "`mcp:" not in page("configuration.md").split("## Coming in later versions")[1]


@pytest.mark.design("docs.scripts")
def test_the_script_shape_its_children_and_its_stopping_are_documented() -> None:
    text = page("scripts.md")
    for word in ("{question}", "children", "SCRIMMAGE_ANSWER_FILE", "process", "other_python"):
        assert f"`{word}`" in text or f"`{word} = true`" in text, word
    assert "`command`" in page("configuration.md") and "`scrimmage-run" in page("cli.md")
    assert "`command`" not in page("configuration.md").split("## Coming in later versions")[1]
    assert "mcp 1.x, started with your environment" not in page("limits.md")


@pytest.mark.design("docs.judging")
def test_the_rules_the_table_and_the_endings_are_documented() -> None:
    text = page("judging.md")
    for word in sorted(config.RULE_KEYS) + render.HEAD[3:-1]:
        assert mentioned(text, word), word
    for result in (judge.PASS, judge.FAIL, judge.NO_RESULT):
        assert f"`{result}`" in text
    assert all(f"| {outcome}" in text for outcome in outcomes.OUTCOMES)
    assert judge.EMPTY in text
    assert "not a promise about the next one" in text
    assert "No judging" not in page("limits.md")
    assert "later version" not in text


def test_every_fault_and_outcome_is_documented() -> None:
    concepts = page("concepts.md")
    assert all(mentioned(concepts, f) for f in config.FAULTS)
    missing = [o for o in outcomes.OUTCOMES if f"| {o} |" not in concepts]
    assert not missing, f"outcomes with no row in docs/concepts.md: {missing}"


def test_exit_codes_match_the_runner() -> None:
    cli = page("cli.md")
    for code in (outcomes.OK, outcomes.ROW_FAILED, outcomes.MISTAKE, outcomes.INFRA):
        assert f"| {code} |" in cli


def test_every_page_is_in_the_site_navigation_and_every_link_works() -> None:
    """mkdocs build --strict checks the same, but needs the docs group; this runs everywhere."""
    nav = (DOCS.parent / "mkdocs.yml").read_text().split("\nnav:\n")[1]
    listed = set(re.findall(r"([a-z/-]+\.md)$", nav, re.M))
    pages = {str(p.relative_to(DOCS)) for p in doc_pages(DOCS)}
    assert listed == pages, f"nav lists {sorted(listed)}, pages {sorted(pages)}"
    for name in pages:
        for target in re.findall(r"\]\(((?:\.\./)?[a-z/-]+\.md)(?:#[a-z0-9-]+)?\)", page(name)):
            path = (DOCS / name).parent / target
            assert path.resolve().is_file(), f"{name} links to missing {target}"
    assert "[Install](install.md)" in page("index.md")
    assert "[Quickstart](quickstart.md)" in page("index.md")


@pytest.mark.design("docs.model_name")
def test_a_fault_on_one_model_and_its_limits_are_documented() -> None:
    assert "| `model` | On a model call" in page("configuration.md")
    guide = page("model-faults.md")
    for word in ("gpt-4.1", "gpt-4o-mini-2024-07-18", "unreadable_model"):
        assert word in guide, word
    limits = page("limits.md")
    assert "**Two calls to one model.**" in limits
    assert "**A model name is compared whole.**" in limits
    assert "**A fault with `model` needs a name it can read.**" in limits
    assert "**Two `model` keys in one body.**" in limits
    for text in (guide, limits):
        assert "the row can pass, and nothing warns" in text
        assert "not the name a reply or a dashboard shows" in text
        assert "sent from a generator" in text
        assert "the openai library takes" in text.lower()
        assert '`""` under `[agent.prices]`, or set `max_cost_usd = "none"`' in text
        assert "any model call with no name, such as a Responses call on a stored prompt" in text
        assert "streamed, multipart" not in text
    assert "a character that does not print" in guide
    assert '"" = {input = 2.0, output = 8.0}' in guide
    assert 'The key `""` prices a call that sends no model name.' in page("configuration.md")
    assert "with the reply streamed or not" in guide
    safety = page("safety.md")
    assert "`unreadable_model`" in safety and "- A chat request body" in safety
    assert "- A request body Scrimmage cannot read" not in safety
    assert "`model`" not in page("configuration.md").split("## Coming in later versions")[1]


@pytest.mark.design("docs.cached_input")
def test_cached_input_prices_and_their_default_are_documented() -> None:
    text = page("configuration.md")
    assert all(mentioned(text, key) for key in config.PRICE_KEYS)
    assert "cached tokens cost the full `input` price" in text
    guide = page("model-faults.md")
    for word in ("prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens"):
        assert word in guide, word
    assert "`tokens.cached`" in guide and "the safe overestimate" in guide
    assert "**Cached input on OpenAI only.**" in page("limits.md")


@pytest.mark.design("docs.method_tools")
def test_tools_that_are_methods_and_their_limits_are_documented() -> None:
    text = page("configuration.md")
    assert "`module:Class.method`" in text and "## Tools that are methods" in text
    for case in ("`staticmethod`", "`classmethod`", "`__init__`", "`Outer.Inner.method`"):
        assert case in text, case
    assert "never holds `self` or `cls`" in text
    assert "**Methods stored as the class is made.**" in page("limits.md")
    assert "A method is gated on its class" in page("how-it-works.md")


@pytest.mark.design("docs.budget_wait")
def test_the_wait_for_calls_in_flight_and_its_report_are_documented() -> None:
    guide = page("model-faults.md")
    assert "### When calls in flight hold the budget" in guide
    assert (
        "even if every other run finished | 30 seconds, or half the time the run has left" in guide
    )
    assert "| a run that has ended | no wait: refused at once |" in guide
    assert "An async call waits on its event loop, not on a thread" in guide
    assert "Tickets are served in the order they came" in guide
    assert "a stream it stopped reading without closing" in guide
    assert "still holding" in guide and '| "not reserved" |' in guide
    for line in ("was reached: $0.1003 was spent", "a model call could not be reserved"):
        assert line in guide, line
    assert "after a wait of 30s for calls in flight" in guide
    assert "#when-calls-in-flight-hold-the-budget" in page("configuration.md")
    assert "**A hold that cannot end while a call waits.**" in page("limits.md")
    assert "| not reserved | left out |" in page("judging.md")
    limits = page("limits.md")
    for item in (
        "A killed run's holds stay",
        "The first in line waits",
        "Two toolkits",
        "alias at the top",
    ):
        assert item in limits, item


@pytest.mark.design("docs.recorded_mocks")
def test_recorded_mocks_and_their_exact_arguments_are_documented() -> None:
    text = page("configuration.md").split("### Mocks that match arguments")[1]
    for words in ("exactly", "next mock", "normal rule", "note", "still a mistake", "exit code 2"):
        assert words in text, words
    assert "Mocks with `args` match arguments exactly" in page("limits.md")
    assert "without `recorded = true` is a mistake" not in text


@pytest.mark.design("docs.check")
def test_check_is_documented_as_reading_the_file_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = page("cli.md")
    assert "`scrimmage check` reads the file only" in text
    assert "## scrimmage check" in text and "## scrimmage schema" in text
    assert "**`scrimmage check` does not import your code.**" in page("limits.md")
    # The quickstart's check line is what scrimmage check prints on the quickstart's file.
    guide = page("quickstart.md")
    (tmp_path / "scrimmage.toml").write_text(re.findall(r"```toml\n(.*?)```", guide, re.S)[0])
    capsys.readouterr()
    assert cli.main(["check", str(tmp_path / "scrimmage.toml")]) == outcomes.OK
    printed = capsys.readouterr().out.strip()
    printed = printed.replace(str(tmp_path / "scrimmage.toml"), "scrimmage.toml")
    assert printed in guide


def property_names(node: object) -> set[str]:
    """Every property name in a JSON Schema, at every depth."""
    names: set[str] = set()
    if isinstance(node, dict):
        table = cast(dict[str, object], node)
        properties = table.get("properties")
        if isinstance(properties, dict):
            names |= set(cast(dict[str, object], properties))
        for value in table.values():
            names |= property_names(value)
    elif isinstance(node, list):
        for item in cast(list[object], node):
            names |= property_names(item)
    return names


@pytest.mark.design("docs.json")
def test_every_command_flag_exit_code_and_json_field_is_documented() -> None:
    cli_page, output = page("cli.md"), page("json-output.md")
    for word in ("scrimmage check [path/to/scrimmage.toml] [--json]", "scrimmage schema", "--json"):
        assert word in cli_page, word
    check = cli_page.split("## scrimmage check")[1].split("## scrimmage record")[0]
    assert all(f"| {code} |" in check for code in cli.STATUS["check"])
    assert "| 1 |" not in check
    missing = sorted(name for name in property_names(CLI_SCHEMA) if f"`{name}`" not in output)
    assert not missing, f"JSON fields with no entry in docs/json-output.md: {missing}"
    lines = {re.sub(r"\s+", " ", line) for line in output.splitlines()}
    for command, words in cli.STATUS.items():
        cells = [f"`{words[code]}`" if code in words else "" for code in range(4)]
        row = re.sub(r"\s+", " ", f"| `{command}` | {' | '.join(cells)} |")
        assert row in lines, row
    assert "a reader may check a document against any version 1 copy of the schema" in output
    assert "the schema of the Scrimmage that wrote it" not in output
    for text in (cli_page, output):  # the window the code leaves, said, not "at any point"
        assert "Ctrl-C or SIGTERM at any point" not in text
        assert "a signal before that" in text and "always equals the document's `exit`" in text
    assert "once scrimmage has started" in output.lower()
    assert "Once Scrimmage has started" in cli_page
    assert "`2.0`" in page("configuration.md") and "rejects `${`" in page("configuration.md")
    assert "[JSON output](json-output.md)" in page("index.md")
    assert "#:schema ./scrimmage.schema.json" in page("configuration.md")
    assert "## Checking the file in your editor" in page("configuration.md")


def enum_values(node: object, name: str = "") -> dict[str, set[str]]:
    """Every enum in the schema, by the name of the field that holds it."""
    found: dict[str, set[str]] = {}
    if isinstance(node, dict):
        table = cast(dict[str, object], node)
        if "enum" in table:
            found.setdefault(name, set()).update(map(str, cast(list[object], table["enum"])))
        for key, value in table.items():
            inner = name if key in ("enum", "items", "anyOf", "oneOf") else key
            for field, values in enum_values(value, inner).items():
                found.setdefault(field, set()).update(values)
    elif isinstance(node, list):
        for item in cast(list[object], node):
            for field, values in enum_values(item, name).items():
                found.setdefault(field, set()).update(values)
    return found


@pytest.mark.design("json.unknown_value_read")
def test_the_docs_say_beside_each_enum_that_an_unknown_value_reads_as_other() -> None:
    text = page("json-output.md").split("## Values that can grow")[1]
    enums = enum_values(CLI_SCHEMA)
    assert {"command", "status", "result", "outcome", "judged", "kind", "stopped_by"} <= set(enums)
    for field, values in enums.items():
        row = next((line for line in text.splitlines() if line.startswith(f"| `{field}` | `")), "")
        assert row.endswith("| read as other |"), field
        listed = set(re.findall(r"`([^`]+)`", row.split("|")[2]))
        assert listed == values, (field, listed ^ values)
    assert "A reader that gets a value it does not know reads it as other." in text


@pytest.mark.design("docs.recording")
def test_recording_its_codes_and_its_limits_are_documented() -> None:
    guide, cli_page, output, limits = (
        page("recording.md"),
        page("cli.md"),
        page("json-output.md"),
        page("limits.md"),
    )
    assert "Recording replies: recording.md" in (DOCS.parent / "mkdocs.yml").read_text()
    assert (
        "scrimmage record [path/to/scrimmage.toml] [--test NAME]... [--refresh LABEL]..."
        in cli_page
    )
    section = cli_page.split("## scrimmage record")[1].split("## scrimmage schema")[0]
    assert all(f"| {code} |" in section for code in cli.STATUS["record"])
    for flag in ("--test NAME", "--refresh LABEL", "--json"):
        assert flag in guide, flag
    for word in ("`recorded = true`", "outside_tool", "credential", "`kept`", "64 KiB"):
        assert word in guide or word in limits, word
    for code in (*record.MANUAL_CODES, *record.SKIPPED_CODES):
        assert f"`{code}`" in output, code
    for limit in (
        "**A live session cookie under `session_id` is kept.**",
        "**A Vault secret read at a nested mount.**",
        "**A web mock cannot match a query or a body.**",
        "**A secret with no pattern under an innocent key is written.**",
        "**A mock's value is capped at 64 KiB of TOML.**",
    ):
        assert limit in limits, limit
    assert "No rule can promise there is none" in guide
    assert "Recording replies" in page("configuration.md") and "## Recording" in page("safety.md")


@pytest.mark.design("docs.init")
def test_init_its_probe_and_the_probe_s_limits_are_documented() -> None:
    guide, commands, limits = page("quickstart.md"), page("cli.md"), page("limits.md")
    assert guide.index("scrimmage init") < guide.index("## 1."), "the quickstart starts with init"
    assert "scrimmage init [folder] [--out PATH] [--probe MODULE:FUNCTION] [--json]" in commands
    section = commands.split("## scrimmage init")[1].split("## --json")[0]
    assert all(f"| {code} |" in section for code in cli.STATUS["init"])
    assert "| 1 |" not in section
    text = page("init.md")
    for words in (
        "`--json` never turns it on",
        "refused before it is sent",
        "imports nothing",
        "A plain tool function called before the first model request runs for real",
        "never runs its own guess",
        "`requests`, `urllib` or a `curl` subprocess",
        "A script entry point is never probed",
        "such as `requests`, runs, and so does a subprocess",
        "on any host",
        "labelled as a heuristic",
        "costs $0",
    ):
        assert words in text, words
    assert "can run calls a test baseline would refuse" in limits
    assert "[Drafting a test file](init.md" in page("safety.md")


@pytest.mark.design("docs.mock_misses")
def test_a_miss_and_its_warning_are_documented() -> None:
    text = page("configuration.md").split("#### Mocks that miss")[1].split("## [[test.fault]]")[0]
    for words in (
        "warning, and the exit code does not change",
        "refusal",
        "nearest mock is a guess",
        "cleaned by a heuristic",
        "fewest differing arguments",
        "Remove the tool from `real`",
    ):
        assert words in text, words
    assert "mock_misses" in page("json-output.md") and "Do not parse it" in page("json-output.md")
    assert "does not cover a call that misses a mock with `args`" in page("safety.md")
    assert "When a replay misses a mock" in page("recording.md")
    for page_name in ("configuration.md", "recording.md", "limits.md", "safety.md"):
        assert "subset" not in page(page_name).replace("no subset", ""), page_name


@pytest.mark.design("docs.held_argument_form")
def test_the_held_argument_form_is_documented() -> None:
    text = page("configuration.md").split("### Mocks that match arguments")[1]
    for words in (
        "Bound by the signature",
        "`{kwargs = {a = 1}}`",
        "Absent. Defaults are not filled in.",
        "`{args = [...]}`",
        "A TOML date, time or datetime is a mistake",
        "mock with no `args` above a mock with `args`",
    ):
        assert words in text, words


@pytest.mark.design("docs.record_runs")
def test_recording_more_than_one_run_is_documented() -> None:
    guide, cli_page, output, limits = (
        page("recording.md"),
        page("cli.md"),
        page("json-output.md"),
        page("limits.md"),
    )
    assert "[--refresh LABEL]... [--runs N] [--json]" in cli_page and "[--runs N]" in guide
    section = guide.split("## Recording more than one run")[1].split("## What gets written")[0]
    for words in (
        "covers every run of every test",
        "file's `runs`",
        "is not read",
        "run N times",
        "`nondeterministic`",
        "p = 0.25",
        "A stop on the budget writes nothing at all",
        "reply that changes with time",
        "`--runs 1`, write its mock by hand, or leave it under `real`",
    ):
        assert words in section, words
    assert "`scrimmage record --runs N`" in guide.split("## When a replay misses a mock")[1]
    for name in ("runs", "runs_recorded", "argument_sets_seen", "argument_sets_written"):
        assert f"`{name}`" in output, name
    assert "`not_started`" in output and "`argument_sets`" in output
    for limit in (
        "**A form the model never sent while recording still misses.**",
        "**One web mock answers any query, at any N.**",
        "**A reply that changes with time is not written at N above 1.**",
    ):
        assert limit in limits, limit
    for text in (guide, cli_page, limits):  # no page says a looser match exists
        assert "looser match" not in text.replace("No looser form", "")


@pytest.mark.design("docs.show")
def test_the_show_page_says_what_redact_removes_and_who_may_see_the_output() -> None:
    text = page("show.md")
    for sentence in (
        "`redact` removes known secret shapes, such as API keys and tokens, not customer data.",
        "`show`'s text and `--json` can hold customer data from the records",
        "A `--json` document handed to a coding agent may be sent to that agent's model provider.",
    ):
        assert sentence in text, sentence
    commands = page("cli.md")
    assert "## scrimmage show" in commands and "--compare" in commands and "--run" in commands
    assert "`scrimmage show --json`" in page("json-output.md")
    assert "signal handling needs the main thread" in commands
