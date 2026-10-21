"""Tool registry: a plain function becomes a model tool with one decorator."""

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass

from openai.types.chat import ChatCompletionFunctionToolParam
from pydantic import BaseModel, ValidationError, create_model

# Long tool output is cut to its last part. Recent log lines matter most.
MAX_TOOL_OUTPUT_CHARS = 20_000


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable[..., object]
    arguments: type[BaseModel]

    def schema(self) -> ChatCompletionFunctionToolParam:
        """The tool in the OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments.model_json_schema(),
            },
        }


class Registry:
    """The one place the loop finds tools: their schemas and how to call them."""

    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def tool[F: Callable[..., object]](self, function: F) -> F:
        """Register a function. Its type hints become the argument schema."""
        fields = {}
        for name, param in inspect.signature(function).parameters.items():
            if param.annotation is inspect.Parameter.empty:
                raise TypeError(f"{function.__name__}({name}) needs a type hint")
            default = ... if param.default is inspect.Parameter.empty else param.default
            fields[name] = (param.annotation, default)
        arguments = create_model(f"{function.__name__}_arguments", **fields)
        self.tools[function.__name__] = Tool(
            name=function.__name__,
            description=inspect.getdoc(function) or "",
            function=function,
            arguments=arguments,
        )
        return function

    def schemas(self) -> list[ChatCompletionFunctionToolParam]:
        return [tool.schema() for tool in self.tools.values()]

    async def call(self, name: str, arguments: str) -> tuple[str, bool]:
        """Run the tool the model asked for. Return its output and whether it worked."""
        tool = self.tools.get(name)
        if tool is None:
            available = ", ".join(self.tools)
            return f"Error: unknown tool {name!r}. Available tools: {available}.", False
        try:
            args = tool.arguments.model_validate_json(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}"
                for e in exc.errors()
            )
            return f"Error: bad arguments {arguments} ({problems}).", False
        try:
            if inspect.iscoroutinefunction(tool.function):
                output = await tool.function(**dict(args))
            else:
                # A thread cannot be cancelled, so a sync tool can run past the deadline.
                output = await asyncio.to_thread(tool.function, **dict(args))
        except Exception as exc:  # noqa: BLE001 - any tool failure goes back to the model
            return f"Error: {name} raised {type(exc).__name__}: {exc}", False
        return clean_output(output), True


def clean_output(output: object) -> str:
    """Turn whatever a tool returned into bounded, printable text."""
    text = output if isinstance(output, str) else repr(output)
    text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\t")
    if not text.strip():
        return "The tool returned an empty result."
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        cut = len(text) - MAX_TOOL_OUTPUT_CHARS
        text = f"[first {cut} characters cut]\n" + text[-MAX_TOOL_OUTPUT_CHARS:]
    return text
