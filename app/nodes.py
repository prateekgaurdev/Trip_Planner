"""The five graph nodes.

    orchestrator → research_agent → planner_agent → human_gate ─┐
                          ▲                ▲                     │
                          │ reject         │ modify             │ approve
                          └────────────────┴──────── route ─────┴──▶ finalize → END

Each node is a plain async function: (state) -> partial state update.
LangGraph merges the returned dict into TripState. The HITL pause lives in
`human_gate`, which calls langgraph.types.interrupt() — the modern (0.2+)
human-in-the-loop primitive — and resumes via Command(resume=...).
"""
from __future__ import annotations

import asyncio
from typing import Any

from langgraph.types import interrupt

from app.pipeline_log import hitl_pause
from app.services.llm import get_llm
from app.state import TripState
from app.tools import allocate_budget, build_day_schedule, build_route_map, get_weather, web_search, get_exchange_rate, generate_packing_list
from app.services.serp import fetch_travel_options


# ─── Node 1: orchestrator ─────────────────────────────────────────────
async def orchestrator(state: TripState) -> dict[str, Any]:
    """Initialise run-level state. Validation already happened at the API
    boundary (Pydantic), so this node just sets the starting status."""
    dest = state.get("preferences", {}).get("destination", "your destination")
    return {
        "status": "researching",
        "progress_message": f"Starting research on {dest}…",
        "revision_notes": state.get("revision_notes", []),
        "revision_count": state.get("revision_count", 0),
    }


# ─── Node 2: research agent ───────────────────────────────────────────
async def research_fetch(state: TripState) -> dict[str, Any]:
    """Gather web, weather, and currency data in parallel."""
    prefs = state["preferences"]
    notes = state.get("revision_notes", [])
    feedback_hint = f" Focus on: {notes[-1]}" if notes else ""
    dest = prefs["destination"]
    notes = state.get("revision_notes", [])
    if notes:
        progress = f"Re-researching {dest} with your feedback…"
    else:
        progress = f"Searching web, weather & currency for {dest}…"

    query = (
        f"Best things to do in {dest} for travelers interested "
        f"in {', '.join(prefs['interests']) or 'general sightseeing'}.{feedback_hint}"
    )

    findings, weather, exchange_rates = await asyncio.gather(
        web_search(query),
        get_weather(dest, prefs["start_date"], prefs["end_date"]),
        get_exchange_rate(prefs["currency"]),
    )

    return {
        "status": "researching",
        "progress_message": progress,
        "_research_raw": {
            "findings": findings,
            "weather": weather,
            "exchange_rates": exchange_rates,
            "query": query,
        },
    }


async def research_agent(state: TripState) -> dict[str, Any]:
    """Distil fetched context into highlights + tips via LLM."""
    prefs = state["preferences"]
    notes = state.get("revision_notes", [])
    raw = state.get("_research_raw") or {}
    findings = raw.get("findings", [])
    weather = raw.get("weather", {})
    exchange_rates = raw.get("exchange_rates", {})

    llm = get_llm()
    distilled = await llm.complete_json(
        system="You are a travel research analyst. Summarise findings as JSON.",
        user=(
            f"Destination: {prefs['destination']}\n"
            f"Interests: {prefs['interests']}\n"
            f"Raw search snippets: {findings}\n"
            f"Current Exchange Rates (Base {prefs['currency']}): {exchange_rates}\n"
            f"Human feedback (if any): {notes[-1] if notes else 'none'}\n"
            "Return JSON with keys: highlights (list of strings), tips (list of strings), and local_currency_hint (a string like 'Local currency is EUR. $1 USD ≈ 0.93 EUR' based on the destination and rates)."
        ),
    )

    return {
        "status": "planning",
        "progress_message": f"Research done — building your day-by-day plan for {prefs['destination']}…",
        "research": {
            "highlights": distilled.get("highlights", []),
            "tips": distilled.get("tips", []),
            "local_currency_hint": distilled.get("local_currency_hint", "Unknown currency."),
            "weather": weather,
            "sources": [f.get("url") for f in findings if f.get("url")],
        },
        "_research_raw": {},
    }


# ─── Node 3: planner agent ────────────────────────────────────────────
async def planner_agent(state: TripState) -> dict[str, Any]:
    """Turn research + budget into a concrete draft itinerary.

    Uses two pure tools (allocate_budget, build_day_schedule) for the
    deterministic structure, then asks the LLM only for a short narrative
    summary — keeping the LLM's role small and auditable.

    On a `modify` loop, the human's patch is included so the planner can
    adjust without re-doing research.
    """
    prefs = state["preferences"]
    research = state.get("research", {})
    notes = state.get("revision_notes", [])

    import datetime as dt

    nights = max(
        (dt.date.fromisoformat(prefs["end_date"])
         - dt.date.fromisoformat(prefs["start_date"])).days,
        1,
    )

    budget = allocate_budget(prefs["budget"], prefs["currency"], nights, prefs["travelers"])
    packing_list = generate_packing_list(
        prefs["destination"],
        research.get("weather", {}),
        nights,
        prefs["interests"],
    )

    llm = get_llm()
    planner_result, travel_options = await asyncio.gather(
        llm.complete_json(
            system=(
                "You are an expert itinerary planner producing a precise, review-ready draft. "
                "Build a logical day-by-day schedule using REAL venue and landmark names a traveler "
                "can look up (e.g. 'Parmarth Niketan', 'Laxman Jhula' — not vague 'local temple'). "
                "Do NOT include airport arrivals, departures, hotel check-ins, or transit into the city. "
                "Assume the traveler is already at the destination and start with sightseeing. "
                "Respect the exact trip dates and number of days."
            ),
            user=(
                f"Destination: {prefs['destination']}\n"
                f"Start Date: {prefs['start_date']}\n"
                f"End Date: {prefs['end_date']}\n"
                f"Travelers: {prefs['travelers']}\n"
                f"Budget breakdown: {budget}\n"
                f"Highlights to include: {research.get('highlights', [])}\n"
                f"Research tips: {research.get('tips', [])}\n"
                f"Weather Data: {research.get('weather', {})}\n"
                f"Human feedback to incorporate (if any): {notes[-1] if notes else 'none'}\n"
                "Return JSON with exactly these keys:\n"
                "- days: list of days, each with day_number, date, focus (indoor/outdoor/flexible), "
                "weather_note, and activities (2-4 strings — each a specific place or experience with a real name)\n"
                "- summary: 2-3 sentences describing trip theme, pacing, and who it suits\n"
                "- notes: 3-5 bullet strings with practical planner advice (timing, budget, local customs)"
            ),
        ),
        fetch_travel_options(prefs),
    )

    schedule = planner_result.get("days", [])

    if not schedule:
        schedule = build_day_schedule(
            prefs["start_date"],
            prefs["end_date"],
            research.get("highlights", []),
            research.get("weather", {}),
        )

    return {
        "status": "awaiting_review",
        "progress_message": "Draft ready — pick optional flight/hotel, then approve.",
        "travel_options": travel_options,
        "travel_selections": {},
        "draft_itinerary": {
            "summary": planner_result.get("summary", ""),
            "budget": budget,
            "days": schedule,
            "packing_list": packing_list,
            "tips": research.get("tips", []),
            "planner_notes": planner_result.get("notes", []),
            "local_currency_hint": research.get("local_currency_hint", ""),
            "weather_available": research.get("weather", {}).get("available", False),
        },
    }


# ─── Node 4: human gate (HITL pause) ──────────────────────────────────
async def human_gate(state: TripState) -> dict[str, Any]:
    """Pause the graph and surface the draft for human review.

    interrupt() suspends execution and persists the checkpoint. The graph
    resumes only when the API calls Command(resume={"action","feedback"}),
    whose payload becomes the return value of interrupt() here.
    """
    if state.get("status") == "awaiting_review" and not state.get("review"):
        hitl_pause()
    decision = interrupt(
        {
            "message": "Review the draft itinerary and approve / reject / modify.",
            "draft_itinerary": state.get("draft_itinerary"),
        }
    )

    action = decision.get("action")
    feedback = (decision.get("feedback") or "").strip()

    update: dict[str, Any] = {"review": decision}
    if action == "approve":
        selections = decision.get("travel_selections")
        if isinstance(selections, dict):
            update["travel_selections"] = {
                "flight": selections.get("flight"),
                "hotel": selections.get("hotel"),
            }
    if action in ("reject", "modify") and feedback:
        notes = list(state.get("revision_notes", []))
        notes.append(f"[{action}] {feedback}")
        update["revision_notes"] = notes
        update["revision_count"] = state.get("revision_count", 0) + 1
    return update


# ─── Node 5: finalize (expand + route map, then pack) ───────────────────
async def finalize_expand(state: TripState) -> dict[str, Any]:
    """Expand approved draft + build route map (parallel, then precise remap)."""
    prefs = state["preferences"]
    draft = state.get("draft_itinerary", {})
    draft_days = draft.get("days", [])
    llm = get_llm()
    existing_travel = state.get("travel_options")
    dest = prefs["destination"]

    expanded, route_map_draft = await asyncio.gather(
        llm.complete_json(
            system=(
                "You are an expert travel planner finalizing an approved itinerary. "
                "Expand the skeletal day plan into a detailed, precise schedule. "
                "Use accurate venue/landmark names in location_name for map routing. "
                "Each activity needs: time (HH:MM), title, description (1-2 sentences), "
                "and location_name (official name of the place). "
                "Do not include airport arrivals, departures, or hotel check-ins."
            ),
            user=(
                f"Destination: {dest}\n"
                f"Draft schedule: {draft_days}\n"
                f"Tips: {draft.get('tips', [])}\n"
                f"Planner notes: {draft.get('planner_notes', [])}\n"
                "Return JSON with key 'days' — each day has day_number, date, title, description, "
                "and activities (time, title, description, location_name)."
            ),
        ),
        build_route_map(dest, draft_days),
    )

    expanded_days = expanded.get("days") or draft_days

    travel_options = existing_travel
    if not travel_options:
        travel_options = await fetch_travel_options(prefs)

    route_map = route_map_draft
    has_locations = any(
        isinstance(a, dict) and (a.get("location_name") or a.get("title"))
        for day in expanded_days
        for a in (day.get("activities") or [])
    )
    if has_locations and expanded.get("days"):
        refined = await build_route_map(dest, expanded_days)
        if refined.get("available"):
            route_map = refined

    return {
        "status": "finalizing",
        "progress_message": "Wrapping up your finalized plan…",
        "_expanded_days": expanded_days,
        "_route_map": route_map,
        "_travel_options": travel_options,
    }


async def finalize_pack(state: TripState) -> dict[str, Any]:
    """Assemble the immutable final plan."""
    from app.services.travel_picks import build_selected_travel

    prefs = state["preferences"]
    draft = state.get("draft_itinerary", {})
    travel_options = state.get("_travel_options") or state.get("travel_options") or {}
    selected = build_selected_travel(state.get("travel_selections"), travel_options)

    final_plan: dict[str, Any] = {
        "destination": prefs["destination"],
        "dates": {"start": prefs["start_date"], "end": prefs["end_date"]},
        "travelers": prefs["travelers"],
        "summary": draft.get("summary", ""),
        "budget": draft.get("budget", {}),
        "itinerary": state.get("_expanded_days") or [],
        "route_map": state.get("_route_map") or {},
        "travel_options": travel_options,
        "origin": prefs.get("origin") or "",
        "packing_list": draft.get("packing_list", []),
        "local_currency_hint": draft.get("local_currency_hint", ""),
        "tips": draft.get("tips", []),
        "revisions": state.get("revision_count", 0),
        "approved": True,
    }
    if selected:
        final_plan["selected_travel"] = selected

    return {
        "status": "completed",
        "progress_message": "Your trip is ready!",
        "final_plan": final_plan,
        "_expanded_days": [],
        "_travel_options": {},
        "_route_map": {},
    }


# ─── Conditional routing after the gate ───────────────────────────────
def route_after_review(state: TripState) -> str:
    """Decide the next node from the human's action.

    approve → finalize | reject → research_agent (full redo)
    modify  → planner_agent (partial redo, research untouched)
    """
    action = (state.get("review") or {}).get("action")
    if action == "approve":
        return "finalize_expand"
    if action == "reject":
        return "research_agent"
    if action == "modify":
        return "planner_agent"
    # Defensive default: unknown action keeps the human in control.
    return "human_gate"
