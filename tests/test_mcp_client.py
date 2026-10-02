"""Tests for Model Context Protocol (MCP) server, client manager, and export tools."""
import json
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.mcp.server import server as mcp_server
from app.mcp.pdf_generator import build_itinerary_pdf
from app.services.mcp_client import get_mcp_client, MCPClientManager


@pytest.mark.asyncio
async def test_mcp_server_tools_registered():
    """Verify all 5 travel concierge tools are registered on the FastMCP server."""
    tools = await mcp_server.list_tools()
    tool_names = [t.name for t in tools]
    expected = [
        "generate_itinerary_pdf",
        "generate_calendar_ics",
        "fetch_travel_safety_and_etiquette",
        "generate_smart_packing_list",
        "convert_and_budget_currency",
    ]
    for exp in expected:
        assert exp in tool_names, f"Missing MCP tool: {exp}"


@pytest.mark.asyncio
async def test_mcp_safety_and_etiquette_japan():
    """Verify safety bulletin and etiquette tool for Japan."""
    res = await mcp_server.call_tool("fetch_travel_safety_and_etiquette", {"destination": "Kyoto, Japan"})
    content = res.structured_content if hasattr(res, "structured_content") else json.loads(res.content[0].text)
    assert content.get("country") == "Japan"
    emergency = content.get("emergency_numbers", {})
    assert "110" in emergency.values()
    assert "tipping_culture" in content
    assert len(content.get("cultural_etiquette", [])) >= 2


@pytest.mark.asyncio
async def test_mcp_safety_and_etiquette_india():
    """Verify safety bulletin and etiquette tool for India."""
    res = await mcp_server.call_tool("fetch_travel_safety_and_etiquette", {"destination": "Rishikesh, India"})
    content = res.structured_content if hasattr(res, "structured_content") else json.loads(res.content[0].text)
    assert content.get("country") == "India"
    assert "112" in str(content.get("emergency_numbers"))
    assert "transit_card_recommendation" in content


@pytest.mark.asyncio
async def test_mcp_calendar_ics_generation():
    """Verify RFC 5545 calendar .ics file generation via MCP tool."""
    days = [
        {
            "day_number": 1,
            "title": "Historic Exploration",
            "neighborhood_cluster": "Higashiyama",
            "activities": [
                {
                    "time": "10:00",
                    "title": "Kiyomizu-dera Temple",
                    "description": "Hilltop UNESCO landmark",
                    "location_name": "Kiyomizu-dera",
                }
            ],
        }
    ]
    res = await mcp_server.call_tool(
        "generate_calendar_ics",
        {
            "trip_title": "Kyoto Journey",
            "start_date": "2026-10-10",
            "days_json": json.dumps(days),
            "lodging_name": "Gion Boutique Ryokan",
        },
    )
    ics_text = res.structured_content["result"] if hasattr(res, "structured_content") else res.content[0].text
    assert "BEGIN:VCALENDAR" in ics_text
    assert "END:VCALENDAR" in ics_text
    assert "SUMMARY:Depart Base" in ics_text
    assert "SUMMARY:Kiyomizu-dera Temple" in ics_text
    assert "SUMMARY:Return to Base" in ics_text


@pytest.mark.asyncio
async def test_mcp_weather_aware_packing():
    """Verify weather-informed packing items."""
    # Rainy conditions
    res_rain = await mcp_server.call_tool(
        "generate_smart_packing_list",
        {
            "destination": "Kyoto, Japan",
            "weather_condition": "Rainy showers",
            "duration_days": 4,
            "interests_json": json.dumps(["temples", "walking"]),
        },
    )
    items_rain = res_rain.structured_content["result"] if hasattr(res_rain, "structured_content") else res_rain.content[0].text
    rain_str = " ".join(items_rain).lower()
    assert "umbrella" in rain_str or "rain" in rain_str
    assert "type a/b" in rain_str  # Japan plug adapter

    # Sunny conditions
    res_sun = await mcp_server.call_tool(
        "generate_smart_packing_list",
        {
            "destination": "Goa, India",
            "weather_condition": "Sunny and clear",
            "duration_days": 3,
            "interests_json": json.dumps(["beach"]),
        },
    )
    items_sun = res_sun.structured_content["result"] if hasattr(res_sun, "structured_content") else res_sun.content[0].text
    sun_str = " ".join(items_sun).lower()
    assert "sunscreen" in sun_str or "sunglasses" in sun_str


@pytest.mark.asyncio
async def test_mcp_currency_conversion():
    """Verify local currency guidelines and cash/card guidance."""
    res = await mcp_server.call_tool(
        "convert_and_budget_currency",
        {
            "amount": 2000.0,
            "base_currency": "USD",
            "destination": "Tokyo, Japan",
        },
    )
    data = res.structured_content if hasattr(res, "structured_content") else json.loads(res.content[0].text)
    assert data["target_currency"] == "JPY"
    assert data["estimated_local_amount"] > 100000
    assert data["cash_preferred"] is True
    assert "cash_vs_card_advice" in data


def test_build_itinerary_pdf_structure():
    """Verify visual PDF document generator produces valid PDF bytes with all sections."""
    plan_mock = {
        "destination": "Kyoto, Japan",
        "preferences": {
            "destination": "Kyoto, Japan",
            "start_date": "2026-10-10",
            "end_date": "2026-10-14",
            "travelers": 2,
            "budget": 2500,
            "currency": "USD",
            "origin": "Tokyo, Japan",
            "custom_notes": "Romantic autumn strolls, tea houses, relaxed mornings",
        },
        "summary": "A balanced 4-day autumn journey through Kyoto's historic shrines and dining alleys.",
        "lodging_anchor": {
            "name": "Boutique Gion Ryokan",
            "neighborhood": "Gion & Higashiyama",
            "reason": "Central historic base with walking access to temples.",
        },
        "journey_corridor": {
            "recommended_mode": "Tokaido Shinkansen (2h 15m)",
        },
        "budget": {
            "total": 2500,
            "allocation": {"lodging": 1100, "food": 650, "activities": 400, "transport": 200, "buffer": 150},
        },
        "weather": {
            "summary": "Crisp autumn weather, 14-22C.",
            "days": [{"date": "2026-10-10", "condition": "Sunny & Clear", "high_c": 22, "low_c": 14, "tip": "Walking"}],
        },
        "days": [
            {
                "day_number": 1,
                "date": "2026-10-10",
                "title": "Higashiyama Lantern Walk",
                "neighborhood_cluster": "Historic Higashiyama",
                "activities": [
                    {
                        "time": "14:30",
                        "title": "Kiyomizu-dera Wooden Stage",
                        "description": "Hilltop temple with panorama.",
                        "duration": "2 hours",
                        "cost": "400 JPY",
                        "travel_from_prev": {"mode": "walking", "duration_mins": 15, "distance_km": 0.8},
                    }
                ],
            }
        ],
        "packing_list": ["Walking shoes", "Power adapter", "Light jacket"],
        "travel_advisories": {
            "emergency_numbers": {"Police": "110", "Ambulance": "119"},
            "etiquette_tips": ["No tipping required."],
        },
    }

    pdf_bytes = build_itinerary_pdf(plan_mock)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 2000
    assert pdf_bytes.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_mcp_client_manager():
    """Verify MCPClientManager discovers tools and calls them."""
    client = get_mcp_client()
    tools = await client.list_tools()
    assert len(tools) >= 5
    safety = await client.fetch_safety_and_etiquette("Kyoto, Japan")
    assert safety.get("country") == "Japan"
    packing = await client.generate_smart_packing("Kyoto, Japan", "Sunny", 4, ["temples"])
    assert len(packing) >= 3


@pytest.mark.asyncio
async def test_api_mcp_tools_endpoint():
    """Verify GET /mcp/tools returns the MCP tool schema list."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/mcp/tools")
        assert res.status_code == 200
        data = res.json()
        assert data["mcp_version"] == "2024-11-05"
        assert data["server_name"] == "wayfarer-travel-concierge"
        assert data["tools_count"] >= 5


@pytest.mark.asyncio
async def test_api_health_includes_mcp():
    """Verify GET /health reports mcp_enabled."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["mcp_enabled"] is True
