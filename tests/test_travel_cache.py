"""Tests for SerpAPI travel result cache."""
import time

from app.services.travel_cache import clear_cache, get_cached, set_cached


def test_travel_cache_roundtrip():
    clear_cache()
    payload = {"origin": "delhi", "destination": "rishikesh", "outbound": "2026-07-19"}
    assert get_cached("flights", payload) is None
    set_cached("flights", payload, {"available": True, "offers": [{"price": 100}]})
    hit = get_cached("flights", payload)
    assert hit is not None
    assert hit["available"] is True
    assert len(hit["offers"]) == 1


def test_travel_cache_key_differs_by_route():
    clear_cache()
    set_cached("flights", {"origin": "delhi", "destination": "goa"}, {"route": "a"})
    assert get_cached("flights", {"origin": "delhi", "destination": "rishikesh"}) is None
