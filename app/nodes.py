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

from typing import Any

from langgraph.types import interrupt

from app.services.llm import get_llm
from app.state import TripState
from app.tools import allocate_budget, build_day_schedule, get_weather, web_search, get_exchange_rate, generate_packing_list


# ─── Node 1: orchestrator ─────────────────────────────────────────────
async def orchestrator(state: TripState) -> dict[str, Any]:
    """Initialise run-level state. Validation already happened at the API
    boundary (Pydantic), so this node just sets the starting status."""
    return {
        "status": "researching",
        "revision_notes": state.get("revision_notes", []),
        "revision_count": state.get("revision_count", 0),
    }


# ─── Node 2: research agent ───────────────────────────────────────────
async def research_agent(state: TripState) -> dict[str, Any]:
    """Gather destination context (web_search) and weather (get_weather),
    then have the LLM distil it into highlights + tips.

    On a reject loop, the latest human feedback is folded into the search
    query so the re-run actually changes behaviour.
    """
    prefs = state["preferences"]
    notes = state.get("revision_notes", [])
    feedback_hint = f" Focus on: {notes[-1]}" if notes else ""

    query = (
        f"Best things to do in {prefs['destination']} for travelers interested "
        f"in {', '.join(prefs['interests']) or 'general sightseeing'}.{feedback_hint}"
    )

    findings = await web_search(query)
    weather = await get_weather(
        prefs["destination"], prefs["start_date"], prefs["end_date"]
    )
    exchange_rates = await get_exchange_rate(prefs["currency"])

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
        "research": {
            "highlights": distilled.get("highlights", []),
            "tips": distilled.get("tips", []),
            "local_currency_hint": distilled.get("local_currency_hint", "Unknown currency."),
            "weather": weather,
            "sources": [f.get("url") for f in findings if f.get("url")],
        },
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
        prefs["interests"]
    )
    
    # Check if there are human notes to adjust the schedule
    llm = get_llm()
    
    # Actually use the LLM to build a realistic daily schedule based on research
    # rather than just round-robin allocating the highlights.
    expanded_schedule = await llm.complete_json(
        system="You are an expert itinerary planner. Build a logical day-by-day skeletal schedule.",
        user=(
            f"Destination: {prefs['destination']}\n"
            f"Start Date: {prefs['start_date']}\n"
            f"End Date: {prefs['end_date']}\n"
            f"Highlights to include: {research.get('highlights', [])}\n"
            f"Weather Data: {research.get('weather', {})}\n"
            f"Human feedback to incorporate (if any): {notes[-1] if notes else 'none'}\n"
            "Return JSON with a single key 'days', which is a list of days. "
            "Each day must have 'day_number', 'date', 'focus' (e.g. indoor/outdoor), "
            "'activities' (list of 2-3 brief strings of what to do), and 'weather_note'."
        ),
    )
    
    schedule = expanded_schedule.get("days", [])
    
    # Fallback to the pure function if the LLM failed to return a schedule
    if not schedule:
        schedule = build_day_schedule(
            prefs["start_date"],
            prefs["end_date"],
            research.get("highlights", []),
            research.get("weather", {}),
        )

    # Apply human modify-feedback as a lightweight note on the draft so the
    # downstream consumer (and reviewer) can see it was honoured.
    narrative = await llm.complete_json(
        system="You are an itinerary planner. Produce a concise JSON summary.",
        user=(
            f"Budget breakdown: {budget}\n"
            f"Daily schedule: {schedule}\n"
            f"Tips: {research.get('tips', [])}\n"
            f"Latest human change request: {notes[-1] if notes else 'none'}\n"
            "Return JSON with keys: summary (string), notes (list of strings)."
        ),
    )

    return {
        "status": "awaiting_review",
        "draft_itinerary": {
            "summary": narrative.get("summary", ""),
            "budget": budget,
            "days": schedule,
            "packing_list": packing_list,
            "tips": research.get("tips", []),
            "planner_notes": narrative.get("notes", []),
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
    decision = interrupt(
        {
            "message": "Review the draft itinerary and approve / reject / modify.",
            "draft_itinerary": state.get("draft_itinerary"),
        }
    )

    action = decision.get("action")
    feedback = (decision.get("feedback") or "").strip()

    update: dict[str, Any] = {"review": decision}
    if action in ("reject", "modify") and feedback:
        notes = list(state.get("revision_notes", []))
        notes.append(f"[{action}] {feedback}")
        update["revision_notes"] = notes
        update["revision_count"] = state.get("revision_count", 0) + 1
    return update


# ─── Node 5: finalize ─────────────────────────────────────────────────
async def finalize(state: TripState) -> dict[str, Any]:
    """Format the approved draft into the final, immutable plan.
    
    Upon human approval, we take the skeletal draft and use the LLM to expand
    it into a rich, detailed day-by-day itinerary complete with times,
    descriptions, and image keywords for the frontend to render.
    """
    prefs = state["preferences"]
    draft = state.get("draft_itinerary", {})
    
    from app.services.llm import get_llm
    llm = get_llm()
    
    # We ask the LLM to expand the barebones draft into a detailed schedule.
    expanded = await llm.complete_json(
        system=(
            "You are an expert travel planner finalizing an approved itinerary. "
            "Take the skeletal day plan and expand it into a detailed day-by-day schedule. "
            "For each day, provide a list of activities with 'time', 'title', 'description', "
            "and an 'image_keyword' (1-2 words describing the location or activity for fetching an image)."
        ),
        user=(
            f"Destination: {prefs['destination']}\n"
            f"Draft schedule: {draft.get('days', [])}\n"
            f"Tips: {draft.get('tips', [])}\n"
            "Return JSON with a single key 'days', which is a list of days. "
            "Each day must have 'day_number', 'date', 'title', 'description' (a brief overview of the day), "
            "and 'activities' (list of dicts with 'time', 'title', 'description' (a detailed 1-2 sentence paragraph), and 'image_keyword' (1-2 words like 'colosseum', 'sushi', etc)). "
            "Make sure the descriptions are rich and engaging."
        ),
    )
    
    expanded_days = expanded.get("days", [])
    
    # Merge the expanded days with the existing schedule structure or just replace it.
    if not expanded_days:
        expanded_days = draft.get("days", [])
        
    return {
        "status": "completed",
        "final_plan": {
            "destination": prefs["destination"],
            "dates": {"start": prefs["start_date"], "end": prefs["end_date"]},
            "travelers": prefs["travelers"],
            "summary": draft.get("summary", ""),
            "budget": draft.get("budget", {}),
            "itinerary": expanded_days,
            "packing_list": draft.get("packing_list", []),
            "local_currency_hint": draft.get("local_currency_hint", ""),
            "tips": draft.get("tips", []),
            "revisions": state.get("revision_count", 0),
            "approved": True,
        },
    }


# ─── Conditional routing after the gate ───────────────────────────────
def route_after_review(state: TripState) -> str:
    """Decide the next node from the human's action.

    approve → finalize | reject → research_agent (full redo)
    modify  → planner_agent (partial redo, research untouched)
    """
    action = (state.get("review") or {}).get("action")
    if action == "approve":
        return "finalize"
    if action == "reject":
        return "research_agent"
    if action == "modify":
        return "planner_agent"
    # Defensive default: unknown action keeps the human in control.
    return "human_gate"
