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
    custom_notes: str = Field(
        default="",
        max_length=1500,
        description=(
            "Free-text traveler preferences, desired trip style, pace, special requirements, "
            "or specific requests (e.g. 'romantic anniversary with relaxed mornings and fine dining', "
            "'spiritual yoga retreat', 'traveling with elderly parents so minimal walking/stairs')."
        ),
        examples=["Romantic honeymoon with slow-paced mornings, scenic sunset dining, and no crowded tourist traps."],
    )

    @field_validator("custom_notes")
    @classmethod
    def _clean_custom_notes(cls, v: str) -> str:
        return (v or "").strip()

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
    custom_notes: str = ""
    research: dict[str, Any] | None = None
    draft_itinerary: dict[str, Any] | None = None
    travel_options: dict[str, Any] | None = None
    lodging_anchor: dict[str, Any] | None = None
    journey_corridor: dict[str, Any] | None = None
    route_map: dict[str, Any] | None = None
    rag_sources: list[str] = Field(default_factory=list)
    revision_count: int = 0
    revision_notes: list[str] = Field(default_factory=list)
    travel_advisories: dict[str, Any] | None = None
    calendar_ics: str | None = None
    mcp_tools_used: list[str] = Field(default_factory=list)


class FinalPlanResponse(BaseModel):
    plan_id: str
    status: str
    final_plan: dict[str, Any]


class ActivityImageLookupRequest(BaseModel):
    destination: str = Field(..., min_length=2)
    title: str = ""
    location_name: str = ""


class ActivityImageLookupResponse(BaseModel):
    image_url: str | None = None
    image_confidence: float | None = None
    image_source: str | None = None


# ─── Structured LLM Output Schemas ────────────────────────────────────

class LodgingAnchor(BaseModel):
    """Explicit persistent accommodation anchor for the entire trip duration.

    Solves the 'Stay at Hotel A Again' flaw by separating the persistent lodging
    base from hourly daily activities.
    """
    name: str = Field(..., description="Name of the accommodation or hotel base.")
    neighborhood: str = Field(default="", description="Neighborhood or district where lodging is situated.")
    address: str = Field(default="", description="Street address or location reference.")
    check_in_time: str = Field(default="15:00", description="Standard check-in time.")
    check_out_time: str = Field(default="11:00", description="Standard check-out time.")
    estimated_nightly_rate: float | None = Field(default=None, description="Estimated price per night.")
    currency: str = Field(default="USD", description="Currency code.")
    why_recommended: str = Field(
        default="",
        description="Why this lodging serves as a convenient geographic base for the itinerary."
    )


class NeighborhoodCluster(BaseModel):
    """Geographic cluster or district identified for spatial grouping."""
    name: str = Field(..., description="District or neighborhood name.")
    vibe: str = Field(default="", description="Atmosphere and character of this area.")
    key_attractions: list[str] = Field(
        default_factory=list, description="Primary landmarks in this cluster."
    )


class JourneyCorridorSchema(BaseModel):
    """Inter-city travel corridor from Origin to Destination."""
    departure_city: str = Field(..., description="Origin city where journey begins.")
    arrival_city: str = Field(..., description="Destination city.")
    recommended_transit_mode: str = Field(
        ...,
        description="Recommended transit mode between cities (e.g. 'Express Rail (Vande Bharat, ~4.5 hrs)', 'Non-stop Flight (~1h 15m)', 'Express Highway Drive (~5.5 hrs)')."
    )
    travel_time_estimate: str = Field(
        ...,
        description="Estimated door-to-door transit time."
    )
    arrival_transfer_guidance: str = Field(
        ...,
        description="Practical instructions on reaching the Lodging Anchor from arrival airport/railway station."
    )
    climate_and_cultural_contrast: str = Field(
        default="",
        description="Customized contrast notes between origin and destination (weather difference, clothing advice, local etiquette)."
    )
    return_departure_tip: str = Field(
        default="",
        description="Logistical advice for final-day check-out and return transit back to origin."
    )
    grounded_route_facts: list[str] = Field(
        default_factory=list,
        description="Grounded route and transit facts retrieved from RAG and live web search."
    )


class ResearchResultSchema(BaseModel):
    highlights: list[str] = Field(description="List of key highlights and attractions of the destination.")
    tips: list[str] = Field(description="Practical tips, customs, and logistics for the destination.")
    local_currency_hint: str = Field(description="Exchange rate/currency summary statement.")
    neighborhoods: list[NeighborhoodCluster] = Field(
        default_factory=list,
        description="Geographic neighborhoods identified for spatial clustering."
    )
    recommended_lodging_area: str = Field(
        default="",
        description="Best neighborhood to stay in for convenient transit and sightseeing."
    )
    grounded_facts: list[str] = Field(
        default_factory=list,
        description="Factual destination knowledge retrieved via RAG."
    )


class PlannerDaySchema(BaseModel):
    day_number: int = Field(description="1-based day index of the trip.")
    date: str = Field(description="ISO-formatted date string.")
    focus: Literal["indoor", "outdoor", "flexible"] = Field(description="The primary focus of this day.")
    neighborhood_cluster: str = Field(
        default="",
        description="Primary geographic district/neighborhood for this day's activities."
    )
    weather_note: str = Field(default="", description="Weather context or notes for this day.")
    activities: list[str] = Field(
        description="List of 2-4 specific activity titles/experiences clustered in the day's neighborhood."
    )


class PlannerResultSchema(BaseModel):
    summary: str = Field(description="A brief narrative summary of the trip.")
    lodging_anchor: LodgingAnchor = Field(
        description="The persistent accommodation anchor serving as the daily departure/return base."
    )
    journey_corridor: JourneyCorridorSchema | None = Field(
        default=None,
        description="End-to-end corridor intelligence connecting Origin and Destination.",
    )
    days: list[PlannerDaySchema] = Field(description="Itinerary days clustered by geographic district.")
    notes: list[str] = Field(
        default_factory=list,
        description="Practical planning advice (timing, budget, transit)."
    )
    transit_strategy: str = Field(
        default="",
        description="Explanation of how geographic clustering minimizes travel friction."
    )


class FinalizeActivitySchema(BaseModel):
    time: str = Field(description="Time of activity in HH:MM format.")
    title: str = Field(description="Short descriptive title of the activity.")
    description: str = Field(description="Detailed 1-2 sentence description of what the activity entails.")
    location_name: str = Field(description="Official name of the venue/landmark for mapping.")
    neighborhood: str = Field(default="", description="District or neighborhood.")
    activity_type: Literal["lodging", "sightseeing", "dining", "culture", "relaxation", "transit"] = Field(
        default="sightseeing", description="Category of activity."
    )


class FinalizeDaySchema(BaseModel):
    day_number: int = Field(description="1-based day index.")
    date: str = Field(description="ISO-formatted date string.")
    title: str = Field(description="Title of this day's theme/focus.")
    neighborhood_cluster: str = Field(
        default="",
        description="Primary geographic district for this day."
    )
    description: str = Field(description="Overall summary/description of the day.")
    activities: list[FinalizeActivitySchema] = Field(description="List of detailed activities.")


class FinalizeResultSchema(BaseModel):
    days: list[FinalizeDaySchema] = Field(description="Fully expanded day plans.")
    lodging_anchor: LodgingAnchor | None = Field(
        default=None,
        description="The persistent accommodation base for the trip."
    )
    journey_corridor: JourneyCorridorSchema | None = Field(
        default=None,
        description="End-to-end corridor intelligence connecting Origin and Destination.",
    )


# ─── Place Vibe Chat ──────────────────────────────────────────────────
class PlaceChatRequest(BaseModel):
    place_name: str = Field(..., examples=["Mingles"])
    destination: str = Field(..., examples=["Seoul, South Korea"])
    query: str = Field(..., examples=["Is it good for a date night?"])
    description: str = Field("", description="Current itinerary snippet")
    category: str = Field("", examples=["Restaurant", "Landmark"])
    chat_history: list[dict[str, str]] = Field(default_factory=list, description="Previous Q&A")

class PlaceChatResponse(BaseModel):
    reply: str = Field(..., description="The conversational, honest vibe review")
    vibe_tags: list[str] = Field(default_factory=list, examples=[["Intimate", "Dressy"]])
    suggested_followups: list[str] = Field(default_factory=list, examples=[["Is there outdoor seating?"]])

# ─── Place Replace ────────────────────────────────────────────────────
class PlaceReplaceRequest(BaseModel):
    current_place: str = Field(..., examples=["Beatles Ashram"])
    destination: str = Field(..., examples=["Rishikesh, India"])
    time_slot: str = Field("", examples=["Morning"])
    day_number: int = Field(1)
    user_preference: str = Field("", description="Custom prompt e.g. 'riverside cafe'")
    existing_places: list[str] = Field(default_factory=list, description="Places already in itinerary to avoid duplicates")

class AlternativeActivity(BaseModel):
    title: str = Field(...)
    location_name: str = Field(...)
    description: str = Field(..., description="2-sentence pitch why it fits")
    time: str = Field(...)
    estimated_cost: float | None = None
    category: str = Field(...)

class PlaceReplaceResponse(BaseModel):
    alternatives: list[AlternativeActivity] = Field(..., min_length=1)


# ─── Supabase Auth & Community Schemas ────────────────────────────────
class UserSignupRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=6, max_length=72)
    full_name: str = Field(..., min_length=2, max_length=100)
    bio: str = Field(default="", max_length=500)
    avatar_url: str = Field(default="")


class UserLoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=1, max_length=72)


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    avatar_url: str
    bio: str = ""
    created_at: str | None = None


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class CommunityTripCreateRequest(BaseModel):
    plan_id: str | None = None
    destination: str = Field(..., min_length=2)
    title: str = Field(..., min_length=3, max_length=300)
    description: str = Field(default="")
    origin: str | None = None
    duration_days: int = Field(default=1, ge=1, le=30)
    budget: float | None = None
    currency: str = Field(default="USD")
    travelers: int = Field(default=1, ge=1, le=20)
    tags: list[str] = Field(default_factory=list)
    itinerary_data: dict[str, Any] = Field(..., description="Full structured itinerary payload")


class CommunityReviewCreateRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5, description="1 to 5 star rating of this itinerary")
    liked_aspects: str = Field(..., min_length=3, description="What you specifically liked about this itinerary")
    suggested_additions: str = Field(..., min_length=3, description="What should be added to make the trip more wonderful")
    comment: str = Field(default="", description="Additional personal notes, tips, or context")


class CommunityReviewResponse(BaseModel):
    id: str
    trip_id: str
    user_id: str | None = None
    user_name: str
    user_avatar: str
    rating: int
    liked_aspects: str
    suggested_additions: str
    comment: str = ""
    created_at: str | None = None


class CommunityTripResponse(BaseModel):
    id: str
    user_id: str | None = None
    author_name: str
    author_avatar: str
    destination: str
    title: str
    description: str = ""
    origin: str | None = None
    duration_days: int
    budget: float | None = None
    currency: str = "USD"
    travelers: int = 1
    tags: list[str] = Field(default_factory=list)
    likes_count: int = 0
    reviews_count: int = 0
    average_rating: float = 5.0
    created_at: str | None = None
    user_has_liked: bool = False


class CommunityTripDetailResponse(CommunityTripResponse):
    itinerary_data: dict[str, Any]
    reviews: list[CommunityReviewResponse] = Field(default_factory=list)


class CommunityRemixRequest(BaseModel):
    custom_instruction: str = Field(default="", description="Optional additional personal tweak from user")


class CommunityRemixResponse(BaseModel):
    new_plan_id: str
    message: str
    remixed_summary: str
    incorporated_suggestions: list[str] = Field(default_factory=list)

