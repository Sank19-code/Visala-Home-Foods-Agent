# How the agent reaches the store: ONLY through the commerce MCP server.
#
#   StdioToolbox     spawns `python -m src.commerce_mcp.server` and talks MCP over stdio,
#                    exactly like Claude Desktop does. Used by the CLI and the demo.
#   InProcessToolbox calls the same FastMCP server object in-process (same schemas, same
#                    audit log, same guardrails) with no subprocess. Used by tests and evals.
#
# Both turn MCP tool definitions into plain {name, description, input_schema} specs (the agent
# converts them to the LLM's format) and return parsed JSON results.
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]


def to_tool_spec(tool: Any) -> dict:
    return {
        "name": tool.name,
        "description": (tool.description or "").strip(),
        "input_schema": tool.inputSchema,
    }


def parse_result(content: Any) -> dict:
    # Our tools return one JSON text block: {"ok": true, ...} or {"ok": false, "error": {...}}
    blocks = content if isinstance(content, list) else getattr(content, "content", [])
    text = "".join(getattr(b, "text", "") for b in blocks)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"ok": True, "result": data}
    except json.JSONDecodeError:
        return {"ok": False, "error": {"code": "bad_tool_output", "message": text[:500]}}


class Toolbox(Protocol):
    async def list_tools(self) -> list[dict]: ...

    async def call(self, name: str, args: dict) -> dict: ...


class StdioToolbox:
    def __init__(self, session_id: str, log_path: Path | None = None) -> None:
        self.session_id = session_id
        self.log_path = log_path or REPO_ROOT / "mcp-error.log"
        self._stack = AsyncExitStack()
        self._session = None

    async def __aenter__(self) -> "StdioToolbox":
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.commerce_mcp.server"],
            env={**os.environ, "AGENT_SESSION_ID": self.session_id},
            cwd=str(REPO_ROOT),
        )
        errlog = self._stack.enter_context(open(self.log_path, "a", encoding="utf-8"))
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def list_tools(self) -> list[dict]:
        result = await self._session.list_tools()
        return [to_tool_spec(t) for t in result.tools]

    async def call(self, name: str, args: dict) -> dict:
        result = await self._session.call_tool(name, args)
        return parse_result(result)


class InProcessToolbox:
    def __init__(self) -> None:
        from src.commerce_mcp.server import mcp

        self._mcp = mcp

    async def __aenter__(self) -> "InProcessToolbox":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def list_tools(self) -> list[dict]:
        return [to_tool_spec(t) for t in await self._mcp.list_tools()]

    async def call(self, name: str, args: dict) -> dict:
        try:
            result = await self._mcp.call_tool(name, args)
        except Exception as exc:  # argument validation errors from FastMCP
            return {"ok": False, "error": {"code": "invalid_input", "message": str(exc)[:500]}}
        content = result[0] if isinstance(result, tuple) else result
        return parse_result(content)
