"""Tests verifying the Lodging Anchor pattern and Spatial Geographic Clustering."""
from __future__ import annotations

import pytest
from app.tools import build_route_map, _stub_route_map
from app.schemas import LodgingAnchor, PlannerDaySchema, PlannerResultSchema, FinalizeResultSchema


def test_lodging_anchor_schema_validation():
    anchor = LodgingAnchor(
        name="Hotel Santa Justa",
        neighborhood="Baixa",
        address="Rua dos Correeiros 204, Lisbon",
        check_in_time="15:00",
        check_out_time="11:00",
        estimated_nightly_rate=145.0,
        currency="EUR",
        why_recommended="Central base enabling walking departures and returns.",
    )
    assert anchor.name == "Hotel Santa Justa"
    assert anchor.neighborhood == "Baixa"
    assert anchor.estimated_nightly_rate == 145.0


def test_planner_result_schema_enforces_clustering_and_anchor():
    payload = {
        "summary": "Geographically clustered 2-day cultural itinerary in Lisbon.",
        "lodging_anchor": {
            "name": "Boutique Chiado Hotel",
            "neighborhood": "Chiado",
            "address": "Rua Garrett, Lisbon",
            "check_in_time": "15:00",
            "check_out_time": "11:00",
            "estimated_nightly_rate": 130.0,
            "currency": "EUR",
            "why_recommended": "Transit hub next to Baixa-Chiado metro station.",
        },
        "days": [
            {
                "day_number": 1,
                "date": "2026-09-10",
                "focus": "flexible",
                "neighborhood_cluster": "Historic Alfama & Baixa",
                "weather_note": "Dry and sunny",
                "activities": ["São Jorge Castle", "Lisbon Cathedral"],
            },
            {
                "day_number": 2,
                "date": "2026-09-11",
                "focus": "outdoor",
                "neighborhood_cluster": "Belém Waterfront",
                "weather_note": "Pleasant breeze",
                "activities": ["Jerónimos Monastery", "Belém Tower"],
            },
        ],
        "notes": ["Consolidated district pacing."],
        "transit_strategy": "Dedicated single-district clusters to minimize transit friction.",
    }
    validated = PlannerResultSchema.model_validate(payload)
    assert validated.lodging_anchor.neighborhood == "Chiado"
    assert validated.days[0].neighborhood_cluster == "Historic Alfama & Baixa"
    assert validated.days[1].neighborhood_cluster == "Belém Waterfront"


@pytest.mark.asyncio
async def test_route_map_anchors_circuit_at_lodging_base():
    days = [
        {
            "day_number": 1,
            "date": "2026-09-10",
            "title": "Historic Baixa",
            "activities": [
                {"title": "São Jorge Castle", "location_name": "São Jorge Castle", "time": "10:00"},
                {"title": "Praça do Comércio", "location_name": "Praça do Comércio", "time": "14:00"},
            ],
        }
    ]
    lodging = {
        "name": "Heritage Baixa Hotel",
        "neighborhood": "Baixa",
        "address": "Rua Augusta 45, Lisbon",
    }
    route = await build_route_map("Lisbon, Portugal", days, lodging_anchor=lodging)
    assert route["available"] is True
    day1 = route["days"][0]
    assert day1["lodging_base"] == "Heritage Baixa Hotel"
    assert "transit_friction" in day1
    assert day1["transit_friction"]["avg_leg_km"] > 0
    # Markers include start from hotel and return to hotel
    markers = day1["markers"]
    assert len(markers) >= 3
    assert "Heritage Baixa Hotel" in markers[0]["title"]
    assert "Heritage Baixa Hotel" in markers[-1]["title"]
