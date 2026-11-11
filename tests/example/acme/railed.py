"""Acme's shop bot behind NeMo Guardrails: an input rail, the bot's own tool loop, an output rail.

NeMo checks the question and the answer. The tool loop between them is the bot's own: the
openai library on gpt-4o-mini, with get_order and refund_order. So a tool result reaches the
output rail only through what the bot writes in its answer.
"""

import os
from pathlib import Path

# NeMo sends usage statistics to NVIDIA unless told not to; nothing leaves a test run.
os.environ.setdefault("NEMO_GUARDRAILS_NO_USAGE_STATS", "1")

import openai
from nemoguardrails import LLMRails, RailsConfig
from nemoguardrails.rails.llm.options import RailStatus, RailType

from acme import agent

CONFIG = Path(__file__).parent / "rails"
UNAVAILABLE = "Sorry, the safety check is unavailable"
SPECS = [s for s in agent.SPECS if s["function"]["name"] in ("get_order", "refund_order")]


def rails() -> LLMRails:
    config = RailsConfig.from_path(str(CONFIG))
    base = os.environ.get("OPENAI_BASE_URL")
    if base:  # the same variable the openai library reads, so one setting moves every model
        for model in config.models:
            model.parameters["base_url"] = base
    return LLMRails(config)


def answer(question: str) -> str:
    guard = rails()
    user = {"role": "user", "content": question}
    try:
        checked = guard.check([user], rail_types=[RailType.INPUT])
    except Exception:  # the rail's own call failed: this bot then refuses
        return UNAVAILABLE
    if checked.status == RailStatus.BLOCKED:
        return checked.content
    draft = agent.loop(openai.OpenAI(max_retries=0), question, specs=SPECS, turns=4)
    try:
        checked = guard.check(
            [user, {"role": "assistant", "content": draft}], rail_types=[RailType.OUTPUT]
        )
    except Exception:
        return UNAVAILABLE
    return checked.content
