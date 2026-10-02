"""Async Model Context Protocol (MCP) Client Manager for Wayfarer.

Follows the official Anthropic Model Context Protocol specification:
- Spawns and manages MCP server child processes via stdio transport (JSON-RPC 2.0)
- Dynamically discovers tools via session.list_tools() (tools/list)
- Invokes tools via session.call_tool() (tools/call)
- Provides resilient fallback to in-process execution and deterministic stubs
  for offline development, CI/CD, and serverless environments.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from app.config import get_settings, stubs_enabled
from app.pipeline_log import api_end, api_start

logger = logging.getLogger("wayfarer.mcp_client")


class MCPClientManager:
    """Enterprise MCP Client handling connection lifecycle, tool discovery, and tool execution."""

    def __init__(self, server_command: str | None = None, server_args: list[str] | None = None) -> None:
        self.server_command = server_command or sys.executable
        self.server_args = server_args or ["-m", "app.mcp.server"]
        self._cached_tools: list[dict[str, Any]] | None = None

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover available tools from the MCP server using tools/list."""
        if self._cached_tools is not None:
            return self._cached_tools

        if stubs_enabled():
            self._cached_tools = [
                {"name": "generate_itinerary_pdf", "description": "Generate visual PDF dossier"},
                {"name": "generate_calendar_ics", "description": "Generate RFC 5545 calendar"},
                {"name": "fetch_travel_safety_and_etiquette", "description": "Fetch safety & etiquette"},
                {"name": "generate_smart_packing_list", "description": "Generate smart packing checklist"},
                {"name": "convert_and_budget_currency", "description": "Convert currency & budget guidance"},
            ]
            return self._cached_tools

        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=self.server_command,
                args=self.server_args,
                env=None,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.list_tools()
                    self._cached_tools = [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "input_schema": t.inputSchema if hasattr(t, "inputSchema") else {},
                        }
                        for t in res.tools
                    ]
                    return self._cached_tools
        except Exception as exc:
            logger.warning("MCP stdio tool discovery failed, falling back to in-process server: %s", exc)
            from app.mcp.server import server as in_process_server

            raw_tools = await in_process_server.list_tools()
            self._cached_tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                }
                for t in raw_tools
            ]
            return self._cached_tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke an MCP tool over stdio transport, with fallback to in-process execution."""
        t0 = api_start("MCP", tool_name, **{k: str(v)[:30] for k, v in arguments.items() if k != "plan_json"})

        # If stubs enabled, or running under tight test mock, invoke directly
        if stubs_enabled():
            from app.mcp.server import server as in_process_server

            try:
                res = await in_process_server.call_tool(tool_name, arguments)
                api_end("MCP", tool_name, t0, ok=True, stub=True)
                return self._extract_result(res)
            except Exception as exc:
                api_end("MCP", tool_name, t0, ok=False, error=str(exc)[:80])
                raise

        # Primary: Execute via official MCP ClientSession over stdio
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=self.server_command,
                args=self.server_args,
                env=None,
            )
            async with asyncio.timeout(15.0):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        res = await session.call_tool(tool_name, arguments)
                        api_end("MCP", tool_name, t0, ok=True, transport="stdio")
                        return self._extract_result(res)
        except Exception as exc:
            logger.warning("MCP stdio call failed for '%s', using resilient in-process fallback: %s", tool_name, exc)
            # Resilient fallback: direct in-process server execution (e.g. for Vercel serverless / CI)
            from app.mcp.server import server as in_process_server

            try:
                if hasattr(in_process_server, "call_tool"):
                    res = await in_process_server.call_tool(tool_name, arguments)
                else:
                    import app.mcp.server as srv_mod
                    fn = getattr(srv_mod, tool_name, None)
                    if fn:
                        import inspect
                        if inspect.iscoroutinefunction(fn):
                            res = await fn(**arguments)
                        else:
                            res = fn(**arguments)
                    else:
                        raise RuntimeError(f"Tool {tool_name} not found")
                api_end("MCP", tool_name, t0, ok=True, transport="in-process-fallback")
                return self._extract_result(res)
            except Exception as inner_exc:
                api_end("MCP", tool_name, t0, ok=False, error=str(inner_exc)[:80])
                logger.warning("In-process MCP fallback for '%s' failed (%s), returning safe fallback.", tool_name, inner_exc)
                return self._safe_fallback(tool_name, arguments)

    def _safe_fallback(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        dest = arguments.get("destination", "Your Destination")
        if tool_name == "fetch_travel_safety_and_etiquette":
            return {
                "country": dest,
                "emergency_numbers": {
                    "Emergency Hotline": "112 / 911",
                    "Local Police": "100 or 112",
                    "Medical Helpline": "102 or 112",
                },
                "tipping_culture": "5% to 10% is customary in sit-down dining unless a service charge is included.",
                "cultural_etiquette": [
                    "Respect dress codes at religious sanctuaries and temples.",
                    "Keep digital copies of photo identification and visas handy.",
                    "Be mindful of personal belongings in crowded transit terminals.",
                ],
                "transit_card_recommendation": "Use contactless tap-to-pay or official transit apps.",
            }
        if tool_name == "generate_smart_packing_list":
            return [
                "Passport & digital travel documentation",
                "Comfortable, broken-in walking shoes",
                "Compact portable power bank",
                "Versatile smart-casual layers",
                "Universal travel power adapter",
            ]
        if tool_name == "convert_and_budget_currency":
            amount = arguments.get("amount", 1000)
            curr = arguments.get("base_currency", "USD")
            return {
                "base_currency": curr,
                "base_amount": amount,
                "target_currency": "USD",
                "estimated_local_amount": amount,
                "exchange_rate": 1.0,
                "cash_vs_card_advice": "Contactless cards accepted in major centers; carry minor cash for tips.",
                "cash_preferred": False,
            }
        if tool_name == "generate_calendar_ics":
            return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Wayfarer//EN\r\nEND:VCALENDAR\r\n"
        return {}

    def _extract_result(self, res: Any) -> Any:
        """Parse text/JSON output from an MCP CallToolResult."""
        if hasattr(res, "structured_content") and res.structured_content:
            val = res.structured_content
            if isinstance(val, dict) and "result" in val:
                return val["result"]
            return val

        if hasattr(res, "content") and res.content:
            first = res.content[0]
            text = getattr(first, "text", "")
            if text:
                try:
                    return json.loads(text)
                except Exception:
                    return text
        return res

    # ── Convenience Typed Helpers for LangGraph Nodes ─────────────────────────

    async def fetch_safety_and_etiquette(self, destination: str) -> dict[str, Any]:
        """Fetch local emergency numbers, tipping customs, and cultural guidelines via MCP."""
        try:
            res = await self.call_tool("fetch_travel_safety_and_etiquette", {"destination": destination})
            if isinstance(res, dict):
                return res
            try:
                return json.loads(res)
            except Exception:
                return {"country": destination, "raw": str(res)}
        except Exception as err:
            logger.warning("fetch_safety_and_etiquette fallback: %s", err)
            return self._safe_fallback("fetch_travel_safety_and_etiquette", {"destination": destination})

    async def generate_smart_packing(
        self,
        destination: str,
        weather_condition: str = "Fair",
        duration_days: int = 4,
        interests: list[str] | None = None,
    ) -> list[str]:
        """Generate weather-informed packing items via MCP."""
        res = await self.call_tool(
            "generate_smart_packing_list",
            {
                "destination": destination,
                "weather_condition": weather_condition,
                "duration_days": duration_days,
                "interests_json": json.dumps(interests or []),
            },
        )
        if isinstance(res, list):
            return [str(x) for x in res]
        try:
            parsed = json.loads(res)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except Exception:
            return ["Passport & Travel Documents", "Comfortable walking shoes"]

    async def generate_calendar(
        self,
        trip_title: str,
        start_date: str,
        days: list[dict[str, Any]],
        lodging_name: str = "Curated Lodging",
    ) -> str:
        """Generate RFC 5545 .ics calendar content via MCP."""
        res = await self.call_tool(
            "generate_calendar_ics",
            {
                "trip_title": trip_title,
                "start_date": start_date,
                "days_json": json.dumps(days),
                "lodging_name": lodging_name,
            },
        )
        return str(res)

    async def generate_pdf(self, plan_data: dict[str, Any]) -> dict[str, Any]:
        """Generate high-fidelity visual PDF bytes & metadata via MCP."""
        res = await self.call_tool(
            "generate_itinerary_pdf",
            {"plan_json": json.dumps(plan_data)},
        )
        if isinstance(res, dict):
            return res
        try:
            return json.loads(res)
        except Exception:
            return {"error": "Failed to parse PDF response", "success": False}


_mcp_client: MCPClientManager | None = None


def get_mcp_client() -> MCPClientManager:
    """Return a singleton instance of MCPClientManager."""
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = MCPClientManager()
    return _mcp_client
