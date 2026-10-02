"""Tests for CARTO Cloud Platform Spatial Intelligence & MCP Integration."""
import pytest
from app.services.carto_mcp import CartoMCPClient, get_carto_mcp_client
from app.tools import _route_leg, geocode_place


def test_carto_mcp_client_singleton():
    """Verify singleton factory returns consistent CartoMCPClient."""
    client1 = get_carto_mcp_client()
    client2 = get_carto_mcp_client()
    assert client1 is client2
    assert isinstance(client1, CartoMCPClient)


def test_carto_mcp_stubs_safety(monkeypatch):
    """When USE_STUBS=true, CARTO MCP client reports not configured for safety in tests."""
    monkeypatch.setenv("USE_STUBS", "true")
    client = CartoMCPClient()
    assert client.is_configured is False


@pytest.mark.asyncio
async def test_carto_mcp_graceful_routing_fallback():
    """Verify _route_leg executes safely with CARTO integration."""
    origin = {"lat": 30.106, "lon": 78.293, "name": "Triveni Ghat"}
    dest = {"lat": 30.122, "lon": 78.312, "name": "Ram Jhula"}
    leg = await _route_leg(origin, dest, profile="foot")
    assert leg is not None
    assert "distance_km" in leg
    assert "duration_minutes" in leg
    assert "geometry" in leg
    assert leg["distance_km"] > 0
    assert leg["duration_minutes"] > 0


@pytest.mark.asyncio
async def test_carto_mcp_graceful_geocoding(monkeypatch):
    """Verify geocode_place runs cleanly with CARTO geocoder primary pipeline."""
    # Test with monkeypatched CARTO geocoder coroutine
    client = get_carto_mcp_client()
    monkeypatch.setattr(type(client), "is_configured", property(lambda self: True))
    async def mock_geo(*args, **kwargs):
        return (30.122, 78.312)
    monkeypatch.setattr(client, "geocode", mock_geo)
    hit = await geocode_place("Beatles Ashram Landmark", "Rishikesh, India")
    assert hit is not None
    assert "lat" in hit
    assert "lon" in hit
    assert hit["lat"] == 30.122
    assert hit["lon"] == 78.312
