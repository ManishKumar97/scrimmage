"""Recording only observes a call: nothing in it may fail the agent's call, and a forked
child's calls name no parent from the process that forked it."""

import asyncio
import dataclasses
import hashlib
import json
from typing import cast

import pytest

from scrimmage_core import capture, mcp_gate, state
from scrimmage_core.decide import Call
from test_record_lines import (
    Sink,
    expect_scrimmage_error,
    gated,
    get_dict,
    no_scrimmage_error,
    recorded_of,
    recording,
    sink,
)

__all__ = ["expect_scrimmage_error", "no_scrimmage_error", "sink"]  # fixtures, used by name


# Recording only observes a call, so nothing in it may fail the agent's call.


def holds_itself() -> list[object]:
    value: list[object] = []
    value.append(value)
    return value


def nested_deep() -> list[object]:
    value: list[object] = []
    inner = value
    for _ in range(3000):
        inner.append([])
        inner = cast(list[object], inner[0])
    return value


def loop_dict() -> dict[str, object]:
    value: dict[str, object] = {}
    value["self"] = value
    return value


SURROGATE = "report-\udcff.pdf"  # a file name with a byte that is not UTF-8


@pytest.mark.design("record.never_fails_the_call")
@pytest.mark.parametrize("record", [True, False], ids=["recording", "test"])
@pytest.mark.parametrize(
    ("returns", "argument"),
    [
        (SURROGATE, "a"),
        ("x", SURROGATE),
        (holds_itself(), "a"),
        (nested_deep(), "a"),
        ("x", loop_dict()),
    ],
    ids=["surrogate-value", "surrogate-arg", "holds-itself", "3000-deep", "arg-holds-itself"],
)
def test_a_value_scrimmage_cannot_write_still_reaches_the_agent_and_leaves_its_line(
    sink: Sink, record: bool, returns: object, argument: object
) -> None:
    run = recording(sink, record=record)

    def lookup(name: object) -> object:
        return returns

    assert gated(lookup)(name=argument) is returns
    (line,) = sink.calls()
    assert line["action"] == "real" and run.live == {}
    json.dumps(line)  # the line can be sent
    if record:
        assert line["recorded"]["code"] == "unsavable"


@pytest.mark.design("record.never_fails_the_call")
@pytest.mark.usefixtures("expect_scrimmage_error")
def test_a_failure_inside_recording_is_listed_never_raised(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = recording(sink)

    def broken(*_: object) -> dict[str, object]:
        raise KeyError("a bug in Scrimmage")

    monkeypatch.setattr(capture, "tool_value", broken)
    assert gated(get_dict)(order_id="1") == get_dict("1")
    assert recorded_of(sink, "tool:get_dict") == {
        "code": "scrimmage_error",
        "why": "Scrimmage failed while recording it: KeyError",
    }
    assert run.live == {}


class Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no message")


@pytest.mark.design("record.never_fails_the_call")
@pytest.mark.parametrize(
    ("error", "shown"),
    [(Unprintable(), "Unprintable: its message could not be read"),
     (ValueError(SURROGATE), "ValueError: report-\\udcff.pdf")],
)  # fmt: skip
def test_an_error_scrimmage_cannot_print_is_still_the_agents_error(
    sink: Sink, error: Exception, shown: str
) -> None:
    run = recording(sink, record=False)

    def lookup(name: str) -> str:
        raise error

    with pytest.raises(type(error)) as caught:
        gated(lookup)(name="a")
    assert caught.value is error
    assert sink.calls()[0]["error"] == shown and run.live == {}


# MCP: the gate's own recording step, with the session and the library stood in for.


class Reply:
    def __init__(self, doc: dict[str, object]) -> None:
        self.doc = doc

    def model_dump(self, **_: object) -> dict[str, object]:
        return self.doc


class Reader:
    """Stands in for the library's TypeAdapter: it reads a reply as it is."""

    @staticmethod
    def validate_python(doc: dict[str, object]) -> Reply:
        return Reply(doc)


class Session:
    def __init__(self, fails: BaseException | None = None) -> None:
        self.fails = fails
        self.listed = 0

    async def list_tools(self) -> None:
        self.listed += 1
        if self.fails is not None:
            raise self.fails


def text_result(text: str, **more: object) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}], "resultType": "complete", **more}


def mcp_call(
    sink: Sink, reply: dict[str, object], arguments: object, session: Session
) -> tuple[object, state.RunState]:
    run = recording(sink, real=("mcp:docs/search",))
    gate = mcp_gate.Gate(run, sink, pytest.fail)
    step = run.decide(Call("mcp", "mcp:docs/search", "search", short="mcp:search"), {})

    async def sent() -> Reply:
        return Reply(reply)

    keep = mcp_gate.Kept(session, arguments, Reader)
    got = asyncio.run(mcp_gate.carried(gate, run, step, sent(), keep))
    return got, run


@pytest.mark.design("record.never_fails_the_call")
@pytest.mark.parametrize(
    ("reply", "arguments"),
    [(text_result(SURROGATE), {"q": "a"}), (text_result("found"), {"q": SURROGATE}),
     (text_result("found"), loop_dict())],
    ids=["surrogate-value", "surrogate-arg", "arg-holds-itself"],
)  # fmt: skip
def test_an_mcp_value_scrimmage_cannot_write_still_reaches_the_agent(
    sink: Sink, reply: dict[str, object], arguments: object
) -> None:
    got, run = mcp_call(sink, reply, arguments, Session())
    assert cast(Reply, got).doc == reply
    (line,) = sink.calls()
    assert line["recorded"]["code"] == "unsavable" and run.live == {}
    json.dumps(line)


@pytest.mark.design("record.mcp_is_error")
def test_an_error_result_sends_nothing_more_to_the_server(sink: Sink) -> None:
    session = Session()
    mcp_call(sink, text_result("boom", isError=True), {"q": "a"}, session)
    assert session.listed == 0
    assert sink.calls()[0]["recorded"]["code"] == "status"


@pytest.mark.design("record.mcp_round_trip")
def test_a_listing_that_fails_leaves_the_call_succeeded_and_unsaved(sink: Sink) -> None:
    got, run = mcp_call(sink, text_result("found"), {"q": "a"}, Session(RuntimeError("gone")))
    assert cast(Reply, got).doc == text_result("found")
    line = sink.calls()[0]
    assert line["recorded"] == {
        "code": "unsavable",
        "why": "listing the server's tools failed: RuntimeError",
    }
    assert line["error"] is None and run.live == {}


@pytest.mark.design("record.mcp_round_trip")
def test_a_cancel_while_saving_is_raised_after_the_call_is_written_as_succeeded(
    sink: Sink,
) -> None:
    with pytest.raises(asyncio.CancelledError):
        mcp_call(sink, text_result("found"), {"q": "a"}, Session(asyncio.CancelledError()))
    line = sink.calls()[0]
    assert line["error"] is None and line["result"] is not None
    assert line["recorded"] == {"code": "unsavable", "why": "the run was cancelled"}


# The side folder.


class Refusing(Sink):
    def put_blob(self, sha: str, data: bytes) -> bool:
        return False


def test_a_saved_value_over_the_cap_is_cut_and_named_by_what_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "BLOB_CAP", 8)
    sink = Sink()
    entry = state.kept_blob(b'"0123456789"', sink)
    assert entry["cut"] is True and entry["bytes"] == 12
    assert sink.blobs[str(entry["sha256"])] == b'"0123456'
    assert entry["sha256"] == hashlib.sha256(b'"0123456').hexdigest()
    assert state.kept_blob(b"1", Refusing())["unsaved"] is True
    assert state.kept_blob(b"1", None)["unsaved"] is True


@pytest.mark.design("record.never_fails_the_call")
@pytest.mark.usefixtures("expect_scrimmage_error")
def test_a_failure_inside_mcp_recording_is_listed_never_raised(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> dict[str, object]:
        raise KeyError("a bug in Scrimmage")

    monkeypatch.setattr(capture, "mcp_value", broken)
    got, run = mcp_call(sink, text_result("found"), {"q": "a"}, Session())
    assert cast(Reply, got).doc == text_result("found")
    assert sink.calls()[0]["recorded"] == {
        "code": "scrimmage_error",
        "why": "Scrimmage failed while recording it: KeyError",
    }
    assert run.live == {}


@dataclasses.dataclass
class Validated:
    """A team's type whose own check raises an error of its own kind."""

    n: int

    def __post_init__(self) -> None:
        if self.n > 1:
            raise RuntimeError("the team's own check failed")


def get_validated(n: str) -> Validated:
    return Validated(1)


@pytest.mark.design("record.scrimmage_error")
def test_an_error_in_the_teams_type_is_unsavable_and_one_in_scrimmage_is_scrimmage_error(
    sink: Sink, monkeypatch: pytest.MonkeyPatch, expect_scrimmage_error: None
) -> None:
    def same(value: object) -> object:
        return value

    value = Validated(1)
    value.n = 2  # changed after it was made, so the team's check fails as the mock is built
    found = capture.tool_value(value, {}, get_validated, same, [f"{__name__}."])
    assert found == {
        "code": "unsavable",
        "why": "returns: a mock built from it fails with RuntimeError",
    }
    # A bug in Scrimmage's own code: differ called with arguments it does not take.
    monkeypatch.setattr(capture, "differ", capture.text_problem)
    bug = capture.tool_value({"a": 1}, {}, get_dict, same, [])
    assert bug == {"code": "scrimmage_error", "why": "Scrimmage failed while recording it: TypeError"}


class BadRepr:
    def __repr__(self) -> str:
        raise RuntimeError("no repr")


@pytest.mark.design("record.never_fails_the_call")
@pytest.mark.parametrize("record", [True, False], ids=["recording", "test"])
@pytest.mark.parametrize(
    "returns", [{"x": BadRepr()}, BadRepr(), 10**5000], ids=["in-a-dict", "alone", "huge-int"]
)
def test_a_value_whose_repr_fails_still_reaches_the_agent(
    sink: Sink, record: bool, returns: object
) -> None:
    run = recording(sink, record=record)

    def lookup(name: str) -> object:
        return returns

    assert gated(lookup)(name="a") is returns
    (line,) = sink.calls()
    assert line["action"] == "real" and run.live == {}
    assert "BadRepr object at 0x" in line["result"]["preview"] or returns == 10**5000
    if record:
        assert line["recorded"]["code"] == "unsavable"
