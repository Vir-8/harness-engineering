"""Tools: a name, a description, a Pydantic model for arguments, and a function."""

from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from refund_harness.agent.types import ToolSpec


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        fn: Callable[[Any], Awaitable[str]],
    ):
        self.name = name
        self.description = description
        self.args_model = args_model
        self.fn = fn

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )

    async def run(self, raw_args: dict[str, Any]) -> str:
        args = self.args_model.model_validate(raw_args)  # raises ValidationError if bad
        return await self.fn(args)


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def get(self, name: str) -> Tool:
        return self._tools[name]  # raises KeyError if unknown

    def names(self) -> list[str]:
        return [t.name for t in self._tools.values()]
