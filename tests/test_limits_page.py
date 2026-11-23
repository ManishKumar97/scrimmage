"""The limits page answers "will it work for my agent" first, then groups the rest by use case."""

import re
from pathlib import Path

DOCS = Path(__file__).parents[1] / "docs"
FIT = "Will Scrimmage work for your agent?"
SIX = [
    "OpenAI only.",
    "LangChain only.",
    "httpx only.",
    "No streaming replies.",
    "Rules match text, not meaning.",
    "Install from git.",
]

ALL_TITLES = [
    "A 400 after a reply fault on Responses is an infrastructure error.",
    "A Responses agent that chains to a faulted reply is not supported.",
    "A Vault secret read at a nested mount.",
    "A blind child not started from Python.",
    "A blind child on another Python.",
    "A body cut by a dropped connection.",
    "A built Responses reply's other fields are fixed.",
    "A built reply's counts are an estimate.",
    "A burst of tool calls with no pause while recording.",
    "A call `background_real` lets through is decided once, with what it runs inside.",
    "A call can cost more than its hold.",
    "A call served on a dearer tier than its hold.",
    "A call with no plan on no server.",
    "A change over text the record removes is not made.",
    "A changed reply keeps the real counts.",
    "A child on another venv's SDK is not version-checked.",
    "A child's gates wait for the control server.",
    "A client without the token can hold the control server's connections.",
    "A clock jump during a test can show as a lost connection.",
    "A command that clears Scrimmage's path.",
    "A connection fault's cause chain, in detail.",
    "A control path cut mid-test does not stop the test.",
    "A copy's bind error that names no address can blame the wrong port.",
    "A credential request from a thread is not seen as a tool's.",
    "A daemon that starts its own session.",
    "A fault with `model` needs a name it can read.",
    "A few faults.",
    "A form field is cut at the first space, quote or `&`.",
    "A form the model never sent while recording still misses.",
    "A gateway's stated cost can only raise a call's cost.",
    "A hold that cannot end while a call waits.",
    "A hook that posts to the MCP endpoint.",
    "A hung call under your own SIGTERM handler.",
    "A killed run's holds stay.",
    "A killed runner leaves its token folder.",
    "A killed runner leaves the copy's other programs.",
    "A killed runner leaves the copy's workers on macOS.",
    "A killed runner takes its runs with it, but not their children.",
    "A last line of output shared with a child.",
    "A lazy worker started inside a run.",
    "A library that changes its tool functions",
    "A library that loses what mocks are built from.",
    "A live session cookie under `session_id` is kept.",
    "A long plain request keeps the threads it started plain until it ends.",
    "A long prompt costed from an estimate.",
    "A mock on a closed MCP session still answers.",
    "A mock's value is capped at 64 KiB of TOML.",
    "A model call from a thread is frozen inside a tool mock, with exit 0.",
    "A model name is compared whole.",
    "A model's path on a host under `real`.",
    "A module reached through a `__path__` its parent changes.",
    "A name lookup can carry data out.",
    "A new certificate needs the agent's pods restarted.",
    "A plumbing type imported under another name.",
    "A process pool's `close()`, `terminate()` or `shutdown()` inside a run is refused.",
    "A process that leaves the copy's group with `setsid` is not stopped.",
    "A provider that is not OpenAI.",
    "A queue's private parts are not gated.",
    "A rate describes the runs that happened.",
    "A recorded `${` is written as `$${`.",
    "A reply that changes with time is not written at N above 1.",
    "A request to a credential or secret-store endpoint runs for real in a test.",
    "A root that execs another program.",
    "A root under `uv run` that execs itself.",
    "A rule cannot tell where a word came from.",
    "A run's changes to its process carry over.",
    "A secret with no pattern under an innocent key is written.",
    "A server that pages its tool list.",
    "A service row's runs go one after another.",
    "A service run is stricter than a script about processes.",
    "A service run refuses every process start, a library's own too.",
    "A service run that queues 10,000 record lines it cannot send waits.",
    "A start made below multiprocessing's own calls is not seen before Python 3.14.",
    "A sync caller that gives up leaves its thread behind.",
    "A tool changed after the last check.",
    "A tool in a file another program runs.",
    "A tool its server answers but does not list.",
    "A tool made with `model_construct`.",
    "A tool with no readable source on Python 3.14.",
    "A tool's function called directly.",
    "A transport's own retries.",
    "A value that changes between runs is frozen by its mock.",
    "A value under 8 characters is not removed from the records.",
    "A waiter keeps its place until it stops waiting.",
    "A web mock cannot match a query or a body.",
    "A wrapper that is a Python program with no Scrimmage.",
    "A wrapper whose `__wrapped__` was set by hand to another function.",
    "An MCP label that reads two ways.",
    "An MCP result with several text items.",
    "An `_arun` that starts `_run` on the default executor itself",
    "An alias at the top of a module.",
    "An injected `reset` never reaches the server.",
    "An outage from a thread is frozen into the mock, with exit 0.",
    "Another process can take the port before the copy binds it.",
    "Cache writes on Chat Completions are unconfirmed.",
    "Cached input on OpenAI only.",
    "Code that rewrites private internals is out of scope.",
    "Code written to get around the gate.",
    "Connection faults on a tool or an MCP tool.",
    "Each control request is a new connection.",
    "Guardrails.",
    "Install from git.",
    "LangChain only.",
    "LangChain versions.",
    "MCP through the official client or a client built on it, such as FastMCP's.",
    "MCP tools under another name.",
    "Methods stored as the class is made.",
    "Mocks with `args` match arguments exactly.",
    "NeMo Guardrails' usage statistics.",
    "No encryption between nodes.",
    'No model judges an answer, and there is no built-in list for "admits the failure".',
    "No streaming replies.",
    "Not part of this version.",
    "Nothing between the runner and the agent.",
    "On Python 3.11, a task built outside `create_task` is not seen at a run's close.",
    "One Job at a time against one `scrimmage` Service.",
    "One `runs` for every row.",
    "One framework.",
    "One node.",
    "One provider.",
    "One runner per service at a time.",
    "One unsafe tool blocks its whole name.",
    "One web mock answers any query, at any N.",
    "Only MCP tool calls are gated.",
    "Only text and plain data take `append` and `replace`.",
    "OpenAI only.",
    "Other ways to another process are not gated.",
    "POSTs a library sends by itself.",
    "Prices need checking.",
    "Programs on another Python.",
    "Python is told by name or shebang.",
    "Python only.",
    "Ray and Dask are not covered.",
    "Refusals that can surprise you.",
    "Regional and FedRAMP endpoints cost more.",
    "Request bodies that are not in memory.",
    "Rules are plain text.",
    "Rules match text, not meaning.",
    "SIGTERM taken by a thread that is not Scrimmage's.",
    "Signals are not gated.",
    "Stopping needs Python to run.",
    "Stopping the service mid-run.",
    "Subinterpreters are refused in a run.",
    "The Starlette gate alone lets an outer middleware see the run header.",
    "The Structured Outputs refusal is the guide's example.",
    "The `dns` message is Linux's.",
    "The cross-run web rule leaves out `outside_tool` and `credential` calls.",
    "The default certificate lasts one day.",
    "The editor schema checks shape only.",
    "The faults in a model's reply are not for a streamed request.",
    "The first Python with the SDK owns the run.",
    "The first in line waits for room.",
    "The mock type check reads the outer type and the first item.",
    "The old SSE transport for MCP.",
    "The plan holds `find` as written.",
    "The probe reaches one replica.",
    "The runner runs inside the cluster.",
    "The setup request lists only what it saw.",
    "The startup file needs Python 3.8 or later.",
    "Three agent shapes.",
    "Tool names with no `tools` list.",
    "Tools reached by import path.",
    "Tools, web requests and OpenAI model calls.",
    "Two HTTP libraries.",
    "Two MCP servers with one name.",
    "Two `model` keys in one body.",
    "Two cache-write rates nothing uses yet.",
    "Two calls to one model.",
    "Two toolkits with the same method names.",
    "Web bodies that are not text or JSON.",
    "What a stdio server does by itself.",
    "With cert-manager, a certificate renewed during a test is seen at the next test.",
    "With cert-manager, the control authority's name constraint covers every namespace.",
    "Without `/proc`, a copy that answers with no gate is named only after 30 seconds.",
    "Work outside any request has a cost while a test runs.",
    "`${` in an injected text.",
    "`--json` prints at the end.",
    "`--loop asyncio` on Python 3.11.",
    "`bad body` on a streamed model request.",
    "`bad tool args` breaks the JSON only.",
    "`context too long` is OpenAI's Chat Completions reply.",
    "`empty` needs a declared type with an empty value.",
    "`first N` in children.",
    "`garble` is one form of corrupted output.",
    "`mcp:search` before the second server is known.",
    "`os.fork` warns on Python 3.12 and later.",
    "`passphrase`, `pw` and other short names are not key-name secrets.",
    "`process:sh` lets any shell command through.",
    "`replace` on a model reply is for Chat Completions text.",
    "`requests N` needs a library that marks its retries.",
    "`scrimmage check` does not import your code.",
    "`scrimmage init --probe` can run calls a test baseline would refuse.",
    "`scrimmage init` reads the code, so it only guesses.",
    "`scrimmage prices update` trusts this repository's `main`.",
    "`scrimmage record` runs one baseline.",
    "`slow` is not a late reply after the work.",
    "`slow` outside asyncio.",
    "`truncate` on Responses keeps the real counts.",
    "`truncate` on a very short text.",
    "`unknown tool` is for Chat Completions.",
    "`unknown tool` sends one form.",
    "asyncio only for async agents.",
    "gunicorn is not covered, with `UvicornWorker` either.",
    "httpx only.",
    "hypercorn, `--http zttp` and `--http2` are not covered.",
    "mcp 2.x only, tested with 2.3.",
    "multiprocessing after an edit of the environment.",
]


def lines() -> list[str]:
    return (DOCS / "limits.md").read_text().split("\n")


def fit_section() -> list[str]:
    text = lines()
    start = text.index(f"## {FIT}")
    end = next(i for i in range(start + 1, len(text)) if text[i].startswith("## "))
    return text[start:end]


def test_the_page_opens_with_will_scrimmage_work_for_your_agent() -> None:
    headings = [line for line in lines() if line.startswith("#")]
    assert headings[0] == "# Limits of this version"
    assert headings[1] == f"## {FIT}"


def test_the_fit_section_holds_the_six_in_order() -> None:
    titles = re.findall(r"^- \*\*([^*]*)\*\*", "\n".join(fit_section()), re.M)
    assert titles == SIX


def test_every_limit_is_under_a_use_case_heading() -> None:
    heading = ""
    outside = []
    for line in lines():
        if line.startswith("#"):
            heading = line
        elif line.startswith("- **"):
            title = re.match(r"- \*\*([^*]*)\*\*", line)
            assert title
            above = heading in ("# Limits of this version", f"## {FIT}")
            if above and title.group(1) not in SIX:
                outside.append(line[:60])
    assert not outside, f"a limit is above the first use case heading: {outside}"
    wanted = [
        "## A function or a script agent",
        "## An agent inside a web service",
        "## In Docker Compose",
        "## In a Kubernetes cluster",
        "## General",
    ]
    present = [line for line in lines() if line in wanted]
    assert present == wanted


def test_each_of_the_six_links_to_its_detail() -> None:
    bullets = "\n".join(fit_section()).split("\n- ")[1:]
    assert len(bullets) == len(SIX)
    for bullet in bullets:
        link = re.search(r"\]\(([a-z-]+\.md)(#[a-z0-9-]+)?\)", bullet)
        assert link, f"no link in: {bullet[:40]}"
        target = DOCS / link.group(1)
        assert target.exists(), link.group(1)
        if link.group(2):
            anchor = link.group(2)[1:]
            heads = [
                re.sub(r"[^a-z0-9 -]", "", h.lstrip("#").strip().lower()).replace(" ", "-")
                for h in target.read_text().split("\n")
                if h.startswith("#")
            ]
            assert anchor in heads, f"{link.group(0)} has no heading"


def test_no_limit_is_dropped() -> None:
    text = "\n".join(lines())
    titles = re.findall(r"^- \*\*([^*]*)\*\*", text, re.M)
    assert sorted(titles) == ALL_TITLES
