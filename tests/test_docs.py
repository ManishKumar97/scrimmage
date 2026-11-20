"""The user docs keep up with the code: every setting, fault and outcome has its place."""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

import scrimmage_core
from conftest import EXAMPLE, doc_pages
from k8s import cluster as kubernetes
from scrimmage_core import cli, config, judge, mcp_gate, outcomes, record, render, suggest

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
    # A row of its own, so a setting named only in prose, or in a later section, is missed.
    missing = sorted(k for k in keys if f"| `{k}` |" not in text)
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


@pytest.mark.design("docs.scripts")
def test_the_script_shape_its_children_and_its_stopping_are_documented() -> None:
    text = page("scripts.md")
    for word in ("{question}", "children", "SCRIMMAGE_ANSWER_FILE", "process", "other_python"):
        assert f"`{word}`" in text or f"`{word} = true`" in text, word
    assert "`command`" in page("configuration.md") and "`scrimmage-run" in page("cli.md")
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


@pytest.mark.design("docs.cached_input")
def test_cached_input_prices_and_their_default_are_documented() -> None:
    text = page("configuration.md")
    assert all(mentioned(text, key) for key in config.PRICE_KEYS)
    assert "cached tokens cost the full `input` price" in text
    guide = page("model-faults.md")
    for word in ("prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens"):
        assert word in guide, word
    assert "`tokens.cached`" in guide and "the safe overestimate" in guide


@pytest.mark.design("docs.cache_write")
def test_cache_write_prices_and_the_current_table_are_documented() -> None:
    guide = page("model-faults.md")
    assert "### Cache writes" in guide
    for word in ("input_tokens_details.cache_write_tokens", "ttl_12h_tokens", "ttl_30m_tokens"):
        assert word in guide, word
    assert "checked against OpenAI's pricing page on 19 November 2026" in guide
    for model in ("gpt-5", "gpt-5-mini", "gpt-5-nano"):
        assert f"`{model}`" in guide.split("### Prices")[1].split("### ")[0], model
    assert "`cache_write_30m`" in page("json-output.md")
    assert "On Chat Completions, where writes last 30 minutes, it leaves out" in guide


@pytest.mark.design("docs.hold_can_be_passed")
def test_a_call_that_costs_more_than_its_hold_is_documented() -> None:
    guide = page("model-faults.md")
    assert "cannot together pass the limit while each costs no more than it reserved" in guide
    assert "](limits.md#the-budget)" in guide
    limits = page("limits.md").split("### The budget")[1].split("### ")[0]
    assert "**A call can cost more than its hold.**" in limits
    assert "the test spends $0.1967" in limits
    assert "times `n` on Chat Completions" in limits and "times `n` on Chat Completions" in guide
    assert "when Scrimmage cannot read the request body" in limits
    security = page("security.md")
    assert "so one call can take the total over the limit" not in security
    assert "can together pass the cap" in security and "](limits.md#the-budget)" in security


@pytest.mark.design("docs.method_tools")
def test_tools_that_are_methods_and_their_limits_are_documented() -> None:
    text = page("configuration.md")
    assert "`module:Class.method`" in text and "## Tools that are methods" in text
    for case in ("`staticmethod`", "`classmethod`", "`__init__`", "`Outer.Inner.method`"):
        assert case in text, case
    assert "never holds `self` or `cls`" in text
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
    assert "`2.0`" in page("configuration.md") and "`$${` is a literal `${`" in page(
        "configuration.md"
    )
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


@pytest.mark.design("docs.changing_results")
def test_the_changing_results_page_covers_the_faults_the_rules_and_the_lines() -> None:
    text = page("changing-results.md")
    for fault in ("append", "replace", "slow"):
        assert mentioned(text, fault), fault
    for key in ("text", "find", "under", "unchanged", "slowed_ms", "cut_by", "under_real"):
        assert f"`{key}`" in text or f"`{key}" in text, key
    for words in (
        "ZXCANARY41",
        "not shaped like a key",
        "leave that tool unmocked and off `real`",
        'uses up its place in `when = "first N"`',
        "promptfoo, garak and PyRIT",
        "future.result(timeout=...)",
        "(heuristic)",
        "A mistake at the row",
    ):
        assert words in text, words
    for cut in ("client timeout", "run stopped", "time limit", "cancelled"):
        assert f"`{cut}`" in text, cut
    assert "invented" not in text.lower()
    assert "model-faults.md" in text
    nav = (DOCS.parent / "mkdocs.yml").read_text()
    assert "Changing a result: changing-results.md" in nav


@pytest.mark.design("docs.changing_results")
def test_the_limits_page_names_each_gap_of_the_carried_faults() -> None:
    limits = page("limits.md")
    assert "runs a listed real write twice" in limits
    faults = page("model-faults.md")
    assert "`append` does not work on a model call" in faults
    assert "`replace` needs `model`" in faults
    later = page("configuration.md").split("## Coming in later versions")[1]
    assert "`append` on a model call" in later


def test_the_guardrails_page_covers_the_two_questions_and_what_is_not_covered() -> None:
    text = page("guardrails.md")
    for words in (
        "leave that tool unmocked and off `real`",
        "ZXCANARY41",
        "Request blocked by policy RB7",
        "`baseline failed`",
        "passes the lenient row and fails the strict one",
        "Fail open or fail closed",
        '"llama-guard-4" = {input = 0, output = 0}',
        "I'm sorry, I can't respond to that.",
        "NEMO_GUARDRAILS_NO_USAGE_STATS=1",
        "promptfoo, garak and PyRIT",
        "## What is not covered",
    ):
        assert words in text, words
    assert "invented" not in text.lower()
    nav = (DOCS.parent / "mkdocs.yml").read_text()
    assert "Testing your guardrails: guardrails.md" in nav
    assert "## A guard model" in page("model-faults.md")


@pytest.mark.design("docs.suggestions")
def test_the_show_page_lists_every_suggestion_code_and_calls_it_a_heuristic() -> None:
    text = page("show.md").split("### Suggestions")[1].split("### What show opens")[0]
    for code in suggest.CODES:
        assert f"| `{code}` |" in text, code
    assert "Every suggestion is a heuristic" in text and "`suggestion (a heuristic):`" in text
    assert "reads `code` and not `text`" in text
    assert 'never "because"' in text
    assert "never edits `scrimmage.toml`" in text


@pytest.mark.design("docs.mcp_server")
def test_the_mcp_server_page_says_what_it_guards_and_what_it_does_not() -> None:
    text = page("mcp-server.md")
    for sentence in (
        "they are not a boundary against a coding agent with a shell",
        "Allowlist `check` and `show` in your client, and never `test` or `record`",
        "A result handed to a coding agent goes to that agent's model provider.",
        "Approving a call to `test` approves whatever the config's entry point does now",
        "carries the recorded responses of real tools word for word",
        "`redact` removes known secret shapes, such as API keys; it does not remove customer data.",
        "On a system with no `/proc`, such as macOS",
        "It speaks MCP over stdio only",
        "No client has been checked against this server yet except the SDK's own scripted client.",
    ):
        assert sentence in text, sentence
    assert "[Driving Scrimmage from a coding agent](mcp-server.md)" in page("mcp.md")
    for name in ("install.md", "cli.md", "safety.md"):
        assert "mcp-server.md" in page(name), name
    assert "## scrimmage-mcp" in page("cli.md")


@pytest.mark.design("docs.services")
def test_the_service_shape_its_placeholders_and_its_limits_are_documented() -> None:
    from scrimmage_core import asgi_gate

    guide = page("services.md")
    assert "An agent inside a web service: services.md" in (DOCS.parent / "mkdocs.yml").read_text()
    for word in ("SCRIMMAGE_SERVICE", "SCRIMMAGE_TOKEN_FILE", "SCRIMMAGE_CONTROL_URL"):
        assert f"| `{word}` |" in guide, word
    for word in ("SCRIMMAGE_TOOLS", "SCRIMMAGE_ALLOW_IMPORTS", "scrimmage-run uvicorn"):
        assert word in guide, word
    for key in ("service", *config.SERVICE_KEYS, "answer", "parallel"):
        assert f"| `{key}` |" in guide, key
    for form in ("`{question}`", "`{session}`", "`${TEST_TOKEN}`", "exit 2"):
        assert form in guide or form.strip("`") in guide, form
    # Every code the gate can answer 503 with has its row, and nothing else has one.
    codes = set(re.findall(r'Unavailable\("([a-z-]+)"\)', Path(asgi_gate.__file__).read_text()))
    codes |= {*asgi_gate.CLAIM_REFUSALS.values(), asgi_gate.NO_SUCH_RUN}
    rows = set(
        re.findall(r"^\| `([a-z-]+)` \| ", guide.split("## When the gate answers 503")[1], re.M)
    )
    assert rows == codes, rows ^ codes
    for words in (
        "## Before the first run",
        "**The probe.**",
        "**The setup request.**",
        "## Workers",
        "## What is not covered",
        "rising toward 80 ms",
        "record_wait_s",
    ):
        assert words in guide, words
    from scrimmage_core import runner as running

    assert f"it asks again for up to {running.READY_S:g} seconds" in guide
    assert "--loop asyncio" in guide and "process:<command>" in guide
    limits = page("limits.md")
    safety = page("safety.md")
    assert '"process:kubectl"' in safety and "`kubectl delete`" in safety
    assert "There is no matching on arguments" in safety
    for point in (
        "`JoinableQueue`",
        "`put_nowait`",
        "`send_bytes`",
        "`InterpreterPoolExecutor.submit`",
    ):
        assert point in safety, point
    assert "every multiprocessing send" not in safety + limits
    assert "resource tracker" in safety and "never serves starts no tracker" in safety
    security = page("security.md")
    assert "## A web service's token and run header" in security and "`0600`" in security
    # The threat model's scope: public APIs in, rewrites of private internals out.
    assert "## Who Scrimmage defends against" in security
    assert "rewrites the private parts of Python's objects or of Scrimmage's own" in security
    assert "out of scope" in page("limits.md")
    assert "replayed once to each other worker within 60 seconds" in security
    assert "[An agent inside a web service](services.md)" in page("configuration.md")
    assert "service-agent.md" in page("examples/index.md")


@pytest.mark.design("docs.container_shape")
def test_the_container_shape_tls_and_check_service_are_documented() -> None:
    guide = page("docker.md")
    compose = (Path(__file__).parent / "compose" / "service" / "compose.yaml").read_text()
    assert compose.strip() in guide  # the page shows the file the Docker tests run
    for words in (
        "### With Docker Compose",
        "SCRIMMAGE_UID",
        "`user:`",
        "create_host_path: false",
        "host.docker.internal:host-gateway",
        "docker build --network none",
        "docker compose port agent 8765",
        "scrimmage check scrimmage.toml --service",
        "docker compose down -v",
        'control = "0.0.0.0:8766"',
        "CAP_NET_RAW",
        "SCRIMMAGE_CONTROL_CA",
    ):
        assert words in guide, words
    assert "## A service in a container" in guide
    assert "[A service in a container](../docker.md#a-service-in-a-container)" in page(
        "examples/service-agent.md"
    )
    assert "`control_tls`" in page("configuration.md") and "scrimmage cert" in page("cli.md")


@pytest.mark.design("docs.faults_judged")
def test_no_documented_fault_fires_on_every_call_with_nothing_to_judge_it() -> None:
    """A fault that always fires needs its own expect or forbid, or a when; as the reader of
    the file refuses one without, no page may show one, run or not."""
    found = 0
    for name in (str(p.relative_to(DOCS)) for p in doc_pages(DOCS)):
        for block in re.findall(r"```(?:toml|diff)[^\n]*\n(.*?)```", page(name), re.S):
            parts = re.split(r"(?m)^[+ ]?(\[\[?[^\]\n]+\]\]?)\s*$", block)
            for header, body in zip(parts[1::2], parts[2::2], strict=True):
                if header.strip("+ ") != "[[test.fault]]":
                    continue
                found += 1
                keys = set(re.findall(r"(?m)^[+ ]?(\w+)\s*=", body))
                do = re.search(r'(?m)^[+ ]?do\s*=\s*"(\w+)', body)
                changes = do is not None and do.group(1) in ("append", "replace")
                assert changes or keys & {"expect", "forbid", "when"}, (name, body)
    assert found >= 10


K8S = Path(__file__).parent / "k8s"
# The files the Kubernetes page's "In Kubernetes" section shows, each as the cluster tests
# apply it. The fake model's Deployment is left out: it stands in for the model provider in
# the tests only. So is
# the agent from the egress test image, which adds a hook only the tests use.
K8S_TEST_ONLY = {"fake-model.yaml", "agent-egress.yaml"}
K8S_SHOWN = {
    **{p.name: p for p in (K8S / "manifests").glob("*.yaml") if p.name not in K8S_TEST_ONLY},
    "pki-authority.yaml": K8S / "pki-authority.yaml",
    "pki-control.yaml": K8S / "pki-control.yaml",
    "scrimmage.toml": K8S / "scrimmage.toml",
}


def kubernetes_docs_problems() -> list[str]:
    """Each file the Kubernetes page's "In Kubernetes" section shows, compared with the committed
    one the cluster tests apply, and each of them shown once. An empty list is in step."""
    parts = page("kubernetes.md").split("\n## In Kubernetes\n", 1)
    if len(parts) != 2:
        return ["kubernetes.md has no In Kubernetes section"]
    section = parts[1].split("\n## ", 1)[0]
    blocks = re.findall(r'(?ms)^```(?:yaml|toml) title="([^"]+)"\n(.*?)^```$', section)
    counts = Counter(title for title, _ in blocks)
    problems = [f"{title} is shown {n} times" for title, n in counts.items() if n > 1]
    shown = dict(blocks)
    problems += [f"{name} is not shown" for name in sorted(K8S_SHOWN.keys() - shown.keys())]
    problems += [f"{name} is not a committed file" for name in sorted(shown.keys() - K8S_SHOWN)]
    problems += [
        f"{name} differs from tests/k8s/{K8S_SHOWN[name].relative_to(K8S)}"
        for name in sorted(shown.keys() & K8S_SHOWN.keys())
        if shown[name] != K8S_SHOWN[name].read_text()
    ]
    return problems


@pytest.mark.design("docs.kubernetes_manifests")
def test_the_docs_manifests_are_the_tested_ones() -> None:
    assert kubernetes_docs_problems() == []
    assert "[In Kubernetes](../kubernetes.md#in-kubernetes)" in page("examples/service-agent.md")


def certificate_text() -> str:
    """The Kubernetes page's certificate block, as written."""
    section = page("kubernetes.md").split("\n### The certificate\n", 1)[1].split("\n#", 1)[0]
    (block,) = re.findall(r"(?ms)^```bash\n(NS=.*?)^```$", section)
    return block


def certificate_block() -> list[list[list[str]]]:
    """Each line of the certificate block, continued lines joined, as the commands the shell
    runs: one list for a plain line, several for a line chained with &&."""
    lines: list[list[list[str]]] = []
    for line in certificate_text().replace("\\\n", " ").splitlines():
        chain: list[list[str]] = [[]]
        for word in shlex.split(line):
            if word == "&&":
                chain.append([])
            else:
                chain[-1].append(word)
        lines.append(chain)
    return lines


def test_the_certificate_block_runs_what_the_harness_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The commands the page shows are the ones the kind tests make their Secrets with, with
    $NS for the namespace and .scrimmage-cert for the harness's folder: a cleanup, the three
    commands chained, and a cleanup on a line of its own."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ran: list[list[str]] = []

    def record_only(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        ran.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(kubernetes, "run", record_only)
    cluster = kubernetes.Cluster("2")
    cluster.control_secrets("ns1")
    out = str(cluster.folder / "cert-ns1")
    harness = [
        [arg.replace(out, ".scrimmage-cert").replace("ns1", "$NS") for arg in command]
        for command in ran
    ]
    harness[0][0] = "scrimmage"  # the harness names the command by its full path
    cleanup = [["rm", "-rf", ".scrimmage-cert"]]
    assert certificate_block() == [[["NS=my-test"]], cleanup, harness, cleanup]


@pytest.mark.slow  # it runs the block in bash
def test_the_certificate_block_removes_the_key_when_a_create_fails(tmp_path: Path) -> None:
    """The block pasted into a shell, with a scrimmage that writes the three files and a
    kubectl whose second create fails: the key is gone, and the shell goes on."""
    bin_dir, work = tmp_path / "bin", tmp_path / "work"
    bin_dir.mkdir()
    work.mkdir()
    fakes = {
        "scrimmage": 'out="${@: -1}"; mkdir "$out" && touch "$out/ca.pem" "$out/server.pem" '
        '"$out/server.key"',
        "kubectl": 'echo "$*" >> ../kubectl.log; [ "$3" != generic ]',
    }
    for name, body in fakes.items():
        fake = bin_dir / name
        fake.write_text(f"#!/bin/bash\n{body}\n")
        fake.chmod(0o755)
    (work / ".scrimmage-cert").mkdir()  # an old folder, which the first rm -rf clears
    script = certificate_text() + "echo STILL\n"
    done = subprocess.run(
        ["bash", "-c", script],
        cwd=work,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.stdout.splitlines()[-1:] == ["STILL"], done.stderr
    assert not (work / ".scrimmage-cert").exists(), "the key was left on disk"
    creates = (tmp_path / "kubectl.log").read_text().splitlines()
    assert [line.split()[2] for line in creates] == ["tls", "generic"], creates


@pytest.mark.design("docs.service_pages")
def test_each_service_use_case_has_its_own_page() -> None:
    """Docker Compose and Kubernetes each have a page in the nav, the service guide links to
    both, and neither page's sections is left in the guide."""
    nav = (DOCS.parent / "mkdocs.yml").read_text().split("\nnav:\n")[1]
    guide = page("services.md")
    moved = {
        "docker.md": (
            "A service in Docker Compose",
            "## A service in a container",
            "### With Docker Compose",
        ),
        "kubernetes.md": (
            "A service in Kubernetes",
            "## The token and the key in a Kubernetes pod",
            "## In Kubernetes",
        ),
    }
    for name, (title, *headings) in moved.items():
        assert f"      - {title}: {name}\n" in nav, name
        assert page(name).startswith(f"# {title}\n"), name
        assert f"]({name})" in guide, name
        for heading in headings:
            assert re.search(rf"(?m)^{re.escape(heading)}$", page(name)), heading
            assert not re.search(rf"(?m)^{re.escape(heading)}$", guide), heading


def test_the_readme_on_pypi_is_a_page_for_a_new_visitor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = (Path(scrimmage_core.__file__).parents[2] / "README.md").read_text()
    assert "\N{EM DASH}" not in readme
    old = [n for n in ("scrimmage-core", "derail", "faultline") if n in readme.lower()]
    assert not old, f"the README names an old project: {old}"
    (block,) = re.findall(r"```toml\n(.*?)```", readme, re.S)
    project = tmp_path / "project"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("__pycache__"))
    (project / "scrimmage.toml").write_text(block)
    assert cli.main(["check", str(project / "scrimmage.toml")]) == 0
    assert "2 rows" in capsys.readouterr().out  # the baseline and the timeout fault
