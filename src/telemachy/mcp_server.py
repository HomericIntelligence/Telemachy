"""Read-only MCP server exposing Agamemnon state to AI agents.

Exposes two tools (both backed by existing AgamemnonClient GET methods):
- ``agamemnon_list_agents`` — wraps ``AgamemnonClient.list_agents``
- ``agamemnon_list_team_tasks`` — wraps ``AgamemnonClient.get_tasks``

No write endpoints are exposed. See issue #173.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from telemachy.agamemnon_client import AgamemnonClient
from telemachy.config import settings


@dataclass
class _ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]


_TOOLS: list[_ToolDescriptor] = [
    _ToolDescriptor(
        name="agamemnon_list_agents",
        description="List all agents currently known to Agamemnon (read-only).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    _ToolDescriptor(
        name="agamemnon_list_team_tasks",
        description="List all tasks for a given Agamemnon team (read-only).",
        input_schema={
            "type": "object",
            "properties": {"team_id": {"type": "string"}},
            "required": ["team_id"],
            "additionalProperties": False,
        },
    ),
]


class UnknownToolError(ValueError):
    """Raised when call_tool() receives a name not in _TOOLS."""


class MissingArgumentError(ValueError):
    """Raised when call_tool() is missing a required tool argument."""


class Dispatcher:
    """Pure read-only routing layer over AgamemnonClient.

    Has NO dependency on the `mcp` SDK so it is unit-testable on a clean
    checkout without any spike. ``build_server()`` adapts it to the SDK.
    """

    def __init__(self, client: AgamemnonClient) -> None:
        self._client = client

    def list_tools(self) -> list[_ToolDescriptor]:
        return list(_TOOLS)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Return the tool's JSON-serialised result text."""
        if name == "agamemnon_list_agents":
            agents = await self._client.list_agents()
            return json.dumps(agents, indent=2)
        if name == "agamemnon_list_team_tasks":
            try:
                team_id = str(arguments["team_id"])
            except KeyError as exc:
                raise MissingArgumentError(
                    "agamemnon_list_team_tasks requires a 'team_id' argument"
                ) from exc
            tasks = await self._client.get_tasks(team_id)
            return json.dumps(tasks, indent=2)
        raise UnknownToolError(f"unknown tool: {name}")


def build_server(dispatcher: Dispatcher) -> Any:
    """Wire the Dispatcher into the mcp SDK. Thin adapter only."""
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    server = Server("telemachy-agamemnon")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
            for t in dispatcher.list_tools()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        text = await dispatcher.call_tool(name, arguments)
        return [TextContent(type="text", text=text)]

    return server


async def _run() -> None:
    from mcp.server.stdio import stdio_server

    async with AgamemnonClient(**settings.client_kwargs()) as client:
        dispatcher = Dispatcher(client)
        server = build_server(dispatcher)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
