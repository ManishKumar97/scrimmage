"""The lab is a customer: its config file maps its attributes, and the product reads it."""

from pathlib import Path

from faultline.config import load_config
from faultline.runs import read_runs

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "packages/faultline/tests/fixtures/normal_run.jsonl"


def test_the_labs_config_finds_what_the_labs_agent_records():
    config = load_config(ROOT / "lab/faultline.toml")

    [run] = read_runs(FIXTURE, config.attributes).runs

    assert (run.question, run.answer, run.stop_reason) == ("why?", "because", "done")
