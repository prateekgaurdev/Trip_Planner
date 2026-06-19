"""Pydantic v2 request/response models — the API contract.

Kept separate from TripState (the internal graph state) on purpose: the
wire format and the internal state can evolve independently, and Pydantic
gives us validation + auto-generated Swagger docs for free.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ─── Requests ─────────────────────────────────────────────────────────
class PlanRequest(BaseModel):
    destination: str = Field(..., min_length=2, examples=["Kyoto, Japan"])
    start_date: date = Field(..., examples=["2026-09-10"])
    end_date: date = Field(..., examples=["2026-09-14"])
    budget: float = Field(..., gt=0, examples=[2500])
    currency: str = Field("USD", min_length=3, max_length=3, examples=["USD", "EUR", "GBP"])
    travelers: int = Field(1, ge=1, le=20)
    interests: list[str] = Field(
        default_factory=list, examples=[["food", "temples", "hiking"]]
    )
    # Optional flight departure city; hotels are always auto-picked at finalize
    origin: str = Field(
        "",
        description="Departure city for automatic flight search (optional).",
        examples=["Delhi, India"],
    )
    flight_destination: str = Field(
        "",
        description="Override flight arrival city; auto-detected from trip destination if empty.",
        examples=["Dehradun, India"],
    )
    include_flights: bool = Field(
        False,
        description="Deprecated — flights run automatically when origin is set.",
    )
    include_hotels: bool = Field(
        False,
        description="Deprecated — hotels are always fetched at finalize.",
    )

    @model_validator(mode="after")
    def _check_travel_options(self) -> "PlanRequest":
        return self

    @model_validator(mode="after")
    def _check_dates(self) -> "PlanRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > 30:
            raise ValueError("trip length capped at 30 days")
        return self


class ReviewRequest(BaseModel):
    action: Literal["approve", "reject", "modify"]
    feedback: str = Field(
        "",
        description="Required for reject/modify; ignored for approve.",
        examples=["Swap the day-3 museum for an outdoor hike."],
    )
    travel_selections: dict[str, Any] | None = Field(
        None,
        description="Optional flight/hotel picks on approve: {flight: offer|null, hotel: offer|null}.",
    )

    @model_validator(mode="after")
    def _feedback_required(self) -> "ReviewRequest":
        if self.action in ("reject", "modify") and not self.feedback.strip():
            raise ValueError(f"feedback is required when action='{self.action}'")
        return self


# ─── Responses ────────────────────────────────────────────────────────
class PlanCreatedResponse(BaseModel):
    plan_id: str
    status: str


class PlanStateResponse(BaseModel):
    plan_id: str
    status: str
    progress_message: str = ""
    preferences: dict[str, Any] | None = None
    research: dict[str, Any] | None = None
    draft_itinerary: dict[str, Any] | None = None
    travel_options: dict[str, Any] | None = None
    revision_count: int = 0
    revision_notes: list[str] = Field(default_factory=list)


class FinalPlanResponse(BaseModel):
    plan_id: str
    status: str
    final_plan: dict[str, Any]
