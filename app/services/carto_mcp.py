"""CARTO Cloud Platform Spatial Intelligence & MCP Integration.

Connects Wayfarer's multi-agent planning workflow directly to the official
remote CARTO MCP Server (https://gcp-us-east1.api.carto.com/mcp/ac_5v2ld53m).
Provides enterprise-grade:
- TomTom LDS Routing (exact turn-by-turn road geometry, length in meters, travel duration)
- TomTom LDS Geocoding (high-accuracy landmark, hotel, and street coordinates)
- TravelTime Origin-Destination Matrix & Isochrones
- CARTO Data Observatory Spatial Querying
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

import httpx

from app.config import get_settings, stubs_enabled
from app.pipeline_log import api_end, api_start

logger = logging.getLogger("wayfarer.carto_mcp")


class CartoMCPClient:
    """Async client for CARTO's remote Streamable HTTP / SSE MCP Server."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def is_configured(self) -> bool:
        """Return True if CARTO MCP is configured with a valid token and not in test stub mode."""
        if stubs_enabled():
            return False
        return bool(self.settings.carto_api_key and self.settings.carto_mcp_url)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool on CARTO's remote MCP Server via JSON-RPC 2.0."""
        if not self.is_configured:
            raise RuntimeError("CARTO MCP Server is not configured or stubs are active.")

        url = self.settings.carto_mcp_url
        token = self.settings.carto_api_key
        t0 = api_start("CARTO-MCP", name, args_keys=list(arguments.keys()))

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Wayfarer-Travel-Concierge/1.0",
        }

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                api_end("CARTO-MCP", name, t0, ok=False, status=resp.status_code)
                raise RuntimeError(f"CARTO MCP error ({resp.status_code}): {resp.text[:200]}")

            raw_text = resp.text
            parsed_data: dict[str, Any] = {}
            for line in raw_text.splitlines():
                if line.startswith("data:"):
                    try:
                        parsed_data = json.loads(line[5:].strip())
                        break
                    except Exception:
                        pass

            if not parsed_data:
                try:
                    parsed_data = json.loads(raw_text)
                except Exception as exc:
                    api_end("CARTO-MCP", name, t0, ok=False, error=str(exc))
                    raise RuntimeError(f"Invalid JSON/SSE from CARTO MCP: {raw_text[:200]}")

            api_end("CARTO-MCP", name, t0, ok=True)
            return parsed_data

    async def get_capabilities(self) -> dict[str, Any]:
        """Query account quotas and active spatial providers."""
        try:
            resp = await self.call_tool("geocode", {"operation": "capabilities"})
            content = resp.get("result", {}).get("content", [])
            if content and isinstance(content, list):
                txt = content[0].get("text", "")
                return json.loads(txt)
        except Exception as exc:
            logger.warning("Failed to query CARTO capabilities: %s", exc)
        return {}

    async def geocode(self, address: str, country: str | None = None) -> tuple[float, float] | None:
        """Geocode an address or landmark using CARTO's enterprise TomTom geocoder."""
        if not self.is_configured:
            return None

        clean_addr = address.strip()
        if not clean_addr:
            return None

        arguments: dict[str, Any] = {
            "operation": "geocode",
            "addresses": [clean_addr],
        }
        if country:
            arguments["country"] = country

        try:
            resp = await self.call_tool("geocode", arguments)
            content = resp.get("result", {}).get("content", [])
            if not content:
                return None

            txt = content[0].get("text", "")
            data_wrapper = json.loads(txt) if isinstance(txt, str) else txt
            rows = data_wrapper.get("data", [])
            if rows and isinstance(rows, list):
                first_row = rows[0]
                candidates = first_row.get("value", [])
                if candidates and isinstance(candidates, list):
                    best = candidates[0]
                    lat = float(best["latitude"])
                    lon = float(best["longitude"])
                    logger.info("CARTO geocoded '%s' -> (%.5f, %.5f) [conf=%.2f]", clean_addr, lat, lon, best.get("matchConfidence", 0.0))
                    return (lat, lon)
        except Exception as exc:
            logger.warning("CARTO geocode failed for '%s': %s", clean_addr, exc)

        return None

    async def route(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
        mode: str = "pedestrian",
    ) -> dict[str, Any] | None:
        """Calculate road route between two points using CARTO TomTom LDS Routing.
        
        Args:
            p1: (lat, lon) of origin
            p2: (lat, lon) of destination
            mode: "pedestrian" | "car" | "bicycle"
        
        Returns:
            {
                "distance_km": float,
                "duration_minutes": int,
                "mode": str,
                "geometry": list[[lon, lat]],
                "provider": "CARTO (TomTom LDS)"
            }
        """
        if not self.is_configured:
            return None

        carto_mode = "pedestrian" if mode in ("walk", "walking", "pedestrian") else "car"
        origin_str = f"{p1[1]:.6f},{p1[0]:.6f}"
        dest_str = f"{p2[1]:.6f},{p2[0]:.6f}"

        arguments: dict[str, Any] = {
            "operation": "route",
            "origin": origin_str,
            "destination": dest_str,
            "mode": carto_mode,
            "overview": "simplified",
        }

        try:
            resp = await self.call_tool("route", arguments)
            content = resp.get("result", {}).get("content", [])
            if not content:
                return None

            txt = content[0].get("text", "")
            data_wrapper = json.loads(txt) if isinstance(txt, str) else txt
            val = data_wrapper.get("data", {}).get("value", {})
            if not val:
                return None

            metadata = val.get("metadata", {})
            routes = metadata.get("routes", [])
            if not routes:
                return None

            summary = routes[0].get("summary", {})
            length_meters = summary.get("lengthInMeters", 0)
            travel_time_sec = summary.get("travelTimeInSeconds", 0)

            coords = val.get("route", {}).get("coordinates", [])
            dist_km = round(length_meters / 1000.0, 2)
            dur_min = max(1, round(travel_time_sec / 60.0))

            logger.info("CARTO TomTom routed %s -> %s: %.2f km, %d min (%d coords)", origin_str, dest_str, dist_km, dur_min, len(coords))
            return {
                "distance_km": dist_km,
                "duration_minutes": dur_min,
                "mode": "walking" if carto_mode == "pedestrian" else "driving",
                "geometry": coords,
                "provider": "CARTO (TomTom LDS)",
            }
        except Exception as exc:
            logger.warning("CARTO route failed %s -> %s: %s", origin_str, dest_str, exc)

        return None


@lru_cache
def get_carto_mcp_client() -> CartoMCPClient:
    """Singleton cached CARTO MCP client."""
    return CartoMCPClient()
