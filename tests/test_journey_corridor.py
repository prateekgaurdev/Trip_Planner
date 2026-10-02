"""Tests for end-to-end Inter-City Journey Corridor (From Origin to Destination)."""
from __future__ import annotations

import os
import uuid
import pytest

os.environ["USE_STUBS"] = "true"
os.environ["GRAPH_BACKGROUND"] = "false"
os.environ.pop("VERCEL", None)

from langgraph.types import Command
from app.graph import _build_uncompiled
from app.schemas import (
    JourneyCorridorSchema,
    PlannerResultSchema,
    LodgingAnchor,
    PlannerDaySchema,
    PlanRequest,
    PlanStateResponse,
)
from app.services.rag import get_rag_engine


def test_plan_request_pydantic_custom_notes():
    req = PlanRequest(
        destination="Kyoto, Japan",
        start_date="2026-09-10",
        end_date="2026-09-14",
        budget=3000,
        custom_notes="  Romantic anniversary trip with relaxed mornings and fine dining   ",
    )
    assert req.custom_notes == "Romantic anniversary trip with relaxed mornings and fine dining"

    resp = PlanStateResponse(
        plan_id="p123",
        status="planning",
        custom_notes="Spiritual yoga retreat",
    )
    assert resp.custom_notes == "Spiritual yoga retreat"


def test_journey_corridor_schema_validation():
    corridor = JourneyCorridorSchema(
        departure_city="Delhi, India",
        arrival_city="Rishikesh, India",
        recommended_transit_mode="Express Rail (Vande Bharat Express, ~4.5 hrs)",
        travel_time_estimate="4.5–5 hours door-to-door",
        arrival_transfer_guidance="Take an autorickshaw or taxi from Haridwar/Rishikesh station directly across Ram Jhula to Tapovan lodging.",
        climate_and_cultural_contrast="Transitioning from the warm Delhi plains to cooler Himalayan foothills; evenings along the Ganges are breezy.",
        return_departure_tip="Book return train in advance and leave Tapovan 90 minutes before scheduled departure.",
        grounded_route_facts=["Vande Bharat runs daily from New Delhi (NDLS) to Dehradun/Haridwar."],
    )
    assert corridor.departure_city == "Delhi, India"
    assert corridor.arrival_city == "Rishikesh, India"
    assert "Vande Bharat" in corridor.recommended_transit_mode
    assert len(corridor.grounded_route_facts) == 1


def test_corridor_rag_retrieval():
    engine = get_rag_engine()
    corridor = engine.retrieve_corridor_intelligence("Delhi, India", "Rishikesh, India")
    assert corridor["origin"] == "Delhi, India"
    assert corridor["destination"] == "Rishikesh, India"
    assert len(corridor["corridor_chunks"]) > 0
    assert len(corridor["sources"]) > 0


def test_planner_result_schema_incorporates_corridor():
    payload = {
        "summary": "Customized corridor journey from Delhi to Rishikesh.",
        "lodging_anchor": {
            "name": "Aloha on the Ganges",
            "neighborhood": "Tapovan",
            "address": "Tapovan, Rishikesh",
            "check_in_time": "15:00",
            "check_out_time": "11:00",
            "estimated_nightly_rate": 95.0,
            "currency": "USD",
            "why_recommended": "Riverside sanctuary in Tapovan with walking access to Ram Jhula.",
        },
        "journey_corridor": {
            "departure_city": "Delhi, India",
            "arrival_city": "Rishikesh, India",
            "recommended_transit_mode": "Vande Bharat Express Rail (~4.5 hrs)",
            "travel_time_estimate": "5 hours door-to-door",
            "arrival_transfer_guidance": "Taxi from Haridwar to Tapovan hotel.",
            "climate_and_cultural_contrast": "Foothill breezes; conservative clothing for temples.",
            "return_departure_tip": "Check out and depart 2 hours prior to evening train.",
            "grounded_route_facts": ["Direct rail connection from New Delhi."],
        },
        "days": [
            {
                "day_number": 1,
                "date": "2026-09-10",
                "focus": "flexible",
                "neighborhood_cluster": "Tapovan & Ram Jhula",
                "weather_note": "Mild and pleasant",
                "activities": ["Ganga Aarti Parmarth Niketan", "Ram Jhula Walk"],
            }
        ],
        "notes": ["Rail travel recommended over highway traffic."],
        "transit_strategy": "Morning arrival by train; localized walking in Tapovan.",
    }
    validated = PlannerResultSchema.model_validate(payload)
    assert validated.journey_corridor is not None
    assert validated.journey_corridor.departure_city == "Delhi, India"
    assert validated.journey_corridor.arrival_city == "Rishikesh, India"


@pytest.mark.asyncio
async def test_e2e_trip_with_origin_and_destination(tmp_path):
    """End-to-end graph run verifying that origin is carried through to final_plan with journey_corridor."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db_file = str(tmp_path / "corridor_ckpt.sqlite")
    plan_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": plan_id}}

    initial_state = {
        "plan_id": plan_id,
        "preferences": {
            "origin": "London, UK",
            "destination": "Lisbon, Portugal",
            "start_date": "2026-09-10",
            "end_date": "2026-09-12",
            "budget": 2000.0,
            "currency": "USD",
            "travelers": 2,
            "interests": ["food", "history"],
            "custom_notes": "Romantic anniversary trip with relaxed mornings and fine dining",
            "flight_destination": "",
            "include_flights": False,
            "include_hotels": False,
        },
    }

    async with AsyncSqliteSaver.from_conn_string(db_file) as saver:
        graph = _build_uncompiled().compile(checkpointer=saver)

        # 1. Start plan -> reaches human_gate
        await graph.ainvoke(initial_state, config=config)
        state = await graph.aget_state(config)
        assert state.values.get("status") == "awaiting_review"
        draft = state.values.get("draft_itinerary", {})
        assert draft.get("journey_corridor") is not None
        assert draft["journey_corridor"]["departure_city"] == "London, UK"
        assert draft["journey_corridor"]["arrival_city"] == "Lisbon, Portugal"
        assert "Romantic" in (draft.get("custom_notes") or "")

        # 2. Approve draft -> completes plan
        await graph.ainvoke(Command(resume={"action": "approve"}), config=config)
        final_state = await graph.aget_state(config)
        assert final_state.values.get("status") == "completed"
        final_plan = final_state.values.get("final_plan", {})
        assert final_plan.get("journey_corridor") is not None
        assert final_plan["journey_corridor"]["departure_city"] == "London, UK"
        assert final_plan["journey_corridor"]["arrival_city"] == "Lisbon, Portugal"
        assert final_plan["origin"] == "London, UK"
        assert final_plan["destination"] == "Lisbon, Portugal"
        assert "Romantic" in (final_plan.get("custom_notes") or "")
