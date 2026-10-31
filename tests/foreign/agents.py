"""Pydantic AI agents written the way its docs show, standing in for a team's own agent.

They are neither the product nor the lab: Faultline must read their traces without
knowing who wrote them. FunctionModel scripts each model reply, so they need no API key.
"""

from collections.abc import Callable

from pydantic_ai import (
    Agent,
    ModelMessage,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

# One model reply: text, or a tool call as (tool name, arguments).
Reply = list[str | tuple[str, dict[str, str]]]


def scripted(*replies: Reply) -> FunctionModel:
    """A model that gives the replies in order, one per model call."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        reply = replies[sum(isinstance(m, ModelResponse) for m in messages)]
        parts: list[ModelResponsePart] = [
            TextPart(part) if isinstance(part, str) else ToolCallPart(*part)
            for part in reply
        ]
        return ModelResponse(parts=parts)

    return FunctionModel(respond)


def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return {"Paris": "sunny, 21C", "Rome": "cloudy, 18C"}.get(city, "unknown")


def get_time(city: str) -> str:
    """Get the local time in a city."""
    return {"Rome": "14:05"}.get(city, "unknown")


def arithmetic_agent() -> Agent:
    """No tools: one model call answers."""
    return Agent(scripted(["4"]), name="arithmetic")


def weather_agent() -> Agent:
    """One tool call, then the answer."""
    model = scripted([("get_weather", {"city": "Paris"})], ["Sunny and 21C in Paris."])
    return Agent(model, name="weather", tools=[get_weather])


def travel_agent() -> Agent:
    """Two steps that each call a tool, three calls in all, then the answer."""
    model = scripted(
        [("get_weather", {"city": "Paris"}), ("get_weather", {"city": "Rome"})],
        [("get_time", {"city": "Rome"})],
        ["Go to Paris. It is sunny there. Rome is cloudy and it is 14:05."],
    )
    return Agent(model, name="travel", tools=[get_weather, get_time])


TASKS: list[tuple[Callable[[], Agent], str]] = [
    (arithmetic_agent, "What is 2 + 2?"),
    (weather_agent, "What is the weather in Paris?"),
    (travel_agent, "Should I visit Paris or Rome today, and what time is it in Rome?"),
]
