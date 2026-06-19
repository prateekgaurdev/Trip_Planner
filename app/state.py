"""The single source of truth for what flows through the graph.

TripState is intentionally flat and JSON-serialisable so it checkpoints
cleanly into SQLite and is trivial to inspect via GET /plan/{id}.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

# Lifecycle of a plan, surfaced verbatim to the API client.
PlanStatus = Literal[
    "researching",      # research agent is gathering context
    "planning",         # planner agent is drafting the itinerary
    "awaiting_review",   # graph paused at the HITL gate, waiting for /review
    "finalizing",       # approved — expanding itinerary, media, travel picks
    "completed",         # plan approved and finalised
]

ReviewAction = Literal["approve", "reject", "modify"]


class TripPreferences(TypedDict):
    destination: str
    start_date: str          # ISO yyyy-mm-dd
    end_date: str            # ISO yyyy-mm-dd
    budget_usd: float
    travelers: int
    interests: list[str]     # e.g. ["food", "museums", "hiking"]


class ReviewDecision(TypedDict, total=False):
    action: ReviewAction
    feedback: str            # free-text guidance for reject / modify


class TripState(TypedDict, total=False):
    """Everything the graph reads from and writes to.

    `total=False` because nodes populate fields incrementally as the run
    progresses; early in the graph many keys simply don't exist yet.
    """

    plan_id: str
    status: PlanStatus
    preferences: TripPreferences

    # Filled by the research agent.
    research: dict[str, Any]          # {"findings": [...], "weather": {...}}

    # Filled by the planner agent.
    draft_itinerary: dict[str, Any]   # {"budget": {...}, "days": [...]}

    # The user's last review decision (set on resume).
    review: ReviewDecision

    # Accumulates each round of human feedback so agents have full history
    # when they re-run after a reject/modify.
    revision_notes: list[str]
    revision_count: int

    # Human-readable status for the UI while agents work.
    progress_message: str

    # Optional picks from the HITL review (included in final plan when set).
    travel_selections: dict[str, Any]

    # Live flight/hotel search results surfaced at review time.
    travel_options: dict[str, Any]

    # Internal research/finalize pipeline buffers.
    _research_raw: dict[str, Any]
    _expanded_days: list[dict[str, Any]]
    _travel_options: dict[str, Any]
    _route_map: dict[str, Any]

    # The approved, formatted output.
    final_plan: dict[str, Any]
