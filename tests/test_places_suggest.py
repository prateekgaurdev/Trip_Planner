"""Tests for global location suggestions and nearest airport distance calculation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.places_suggest import find_closest_airport, haversine, suggest_places


def test_haversine_distance():
    # Delhi center (28.6139, 77.2090) to IGI Airport (28.5665, 77.1031)
    dist = haversine(28.6139, 77.2090, 28.5665, 77.1031)
    assert 10.0 <= dist <= 16.0


def test_suggest_places_empty():
    results = suggest_places("", limit=5)
    assert len(results) > 0
    assert any("Delhi" in r["name"] or "Rishikesh" in r["name"] for r in results)


def test_suggest_places_rishikesh():
    results = suggest_places("rishi", limit=3)
    assert len(results) > 0
    top = results[0]
    assert "Rishikesh" in top["name"]
    assert top["city"] == "Rishikesh"
    assert top["closest_airport_iata"] == "DED"
    assert 10 <= top["distance_km"] <= 25
    assert "Dehradun" in top["closest_airport_name"] or "DED" in top["sub"]


def test_suggest_places_amalfi():
    results = suggest_places("amalfi", limit=3)
    assert len(results) > 0
    top = results[0]
    assert "Amalfi" in top["name"]
    assert top["distance_km"] > 0


def test_suggest_places_global_city():
    results = suggest_places("innsbruck", limit=3)
    assert len(results) > 0
    top = results[0]
    assert "Innsbruck" in top["name"]
    assert top["closest_airport_iata"] == "INN"


def test_api_endpoint_places_suggest():
    client = TestClient(app)
    res = client.get("/places/suggest?q=delhi&limit=3")
    assert res.status_code == 200
    data = res.json()
    assert len(data) > 0
    assert "Delhi" in data[0]["name"]
    assert "DEL" in data[0]["closest_airport_iata"] or "DEL" in data[0]["sub"]
