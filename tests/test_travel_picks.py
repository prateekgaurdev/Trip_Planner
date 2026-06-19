"""Tests for budget-aware travel recommendations."""
from app.services.travel_picks import (
    build_selected_travel,
    build_travel_recommendations,
    pick_best_flight,
    pick_top_hotels,
)


def test_pick_best_flight_prefers_cheaper_within_budget():
    flights = {
        "available": True,
        "offers": [
            {"price": 900, "stops": 0, "total_duration": 480, "airlines": "Expensive"},
            {"price": 350, "stops": 1, "total_duration": 540, "airlines": "Value Air"},
        ],
    }
    best = pick_best_flight(flights, total_budget=1000, currency="USD")
    assert best is not None
    assert best["offer"]["airlines"] == "Value Air"


def test_pick_top_hotels_returns_three():
    hotels = {
        "available": True,
        "offers": [
            {"name": "A", "price": 80, "rating": 4.8, "reviews": 500},
            {"name": "B", "price": 60, "rating": 4.2, "reviews": 200},
            {"name": "C", "price": 120, "rating": 4.9, "reviews": 900},
            {"name": "D", "price": 45, "rating": 3.9, "reviews": 50},
        ],
    }
    picks = pick_top_hotels(hotels, total_budget=2000, nights=4, top_n=3)
    assert len(picks) == 3
    assert picks[0]["rank"] == 1


def test_build_selected_travel_summary():
    sel = {
        "flight": {"route_iata": "DEL → DED", "airlines": "Demo Air", "departure": "Delhi", "arrival": "Dehradun"},
        "hotel": {"name": "Central Inn", "price": 89},
    }
    out = build_selected_travel(sel, {"flights": {"airports": {"origin": [{"iata": "DEL"}], "destination": [{"iata": "DED"}]}}})
    assert out is not None
    assert "Central Inn" in out["summary"]
    assert "DEL → DED" in out["summary"]


def test_build_selected_travel_empty():
    assert build_selected_travel(None, {}) is None
    assert build_selected_travel({"flight": None, "hotel": None}, {}) is None
    rec = build_travel_recommendations(
        {
            "flights": {"available": True, "offers": [{"price": 300, "stops": 0, "total_duration": 400, "airlines": "X"}]},
            "hotels": {"available": True, "offers": [{"name": "H", "price": 70, "rating": 4.5, "reviews": 100}]},
        },
        {"budget": 1500, "currency": "USD", "start_date": "2026-07-19", "end_date": "2026-07-23"},
    )
    assert rec["has_picks"] is True
    assert len(rec["flights"]) >= 1
    assert len(rec["hotels"]) >= 1
