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

from app.pipeline_log import hitl_pause, mcp_flow
from app.services.llm import get_llm
from app.services.mcp_client import get_mcp_client
from app.state import TripState
from app.tools import (
    allocate_budget,
    build_day_schedule,
    build_route_map,
    get_weather,
    web_search,
    get_exchange_rate,
    generate_packing_list,
)
from app.services.serp import fetch_travel_options
from app.services.rag import get_rag_engine
from app.schemas import (
    ResearchResultSchema,
    PlannerResultSchema,
    FinalizeResultSchema,
    LodgingAnchor,
)


def _format_snippets(snippets: list[dict[str, Any]], max_items: int = 4, max_chars: int = 260) -> str:
    """Format raw search snippets into clean, concise text for token-efficient prompts."""
    lines: list[str] = []
    for s in snippets[:max_items]:
        if not isinstance(s, dict):
            continue
        title = (s.get("title") or "").strip()
        content = (s.get("content") or s.get("snippet") or "").strip()
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(" ", 1)[0] + "…"
        if content:
            lines.append(f"- {title}: {content}" if title else f"- {content}")
    return "\n".join(lines) if lines else "None"


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
    """Gather web, weather, currency, and RAG domain intelligence for both origin and destination."""
    prefs = state["preferences"]
    notes = state.get("revision_notes", [])
    feedback_hint = f" Focus on: {notes[-1]}" if notes else ""
    dest = prefs["destination"]
    origin = (prefs.get("origin") or "").strip()
    custom_notes = (prefs.get("custom_notes") or state.get("custom_notes") or "").strip()

    if notes:
        progress = f"Re-researching {dest} with your feedback…"
    elif origin:
        progress = f"Researching travel corridor from {origin} to {dest} via web & RAG…"
    else:
        progress = f"Searching web, weather, currency & RAG intelligence for {dest}…"

    custom_notes_hint = f" Specifically tailored for: {custom_notes}." if custom_notes else ""
    dest_query = (
        f"Best things to do in {dest} for travelers interested "
        f"in {', '.join(prefs['interests']) or 'general sightseeing'}.{feedback_hint}{custom_notes_hint}"
    )

    mcp_client = get_mcp_client()
    fetch_tasks = [
        web_search(dest_query),
        get_weather(dest, prefs["start_date"], prefs["end_date"]),
        get_exchange_rate(prefs["currency"]),
        fetch_travel_options(prefs),
        mcp_client.fetch_safety_and_etiquette(dest),
    ]

    if origin:
        corridor_query = f"Best ways to travel from {origin} to {dest} transit options flight train driving travel time"
        fetch_tasks.append(web_search(corridor_query))

    fetch_results = await asyncio.gather(*fetch_tasks)
    findings = fetch_results[0]
    weather = fetch_results[1]
    exchange_rates = fetch_results[2]
    travel_options = fetch_results[3]
    travel_advisories = fetch_results[4]
    corridor_findings = fetch_results[5] if origin and len(fetch_results) > 5 else []

    mcp_flow("Ingested destination advisories via MCP", destination=dest, emergency=bool(travel_advisories.get("emergency_numbers")))

    # Ingest web findings and retrieve grounded domain knowledge via RAG
    rag = get_rag_engine()
    rag.ingest_live_search(dest, findings)
    if corridor_findings:
        rag.ingest_live_search(f"{origin} to {dest}", corridor_findings)

    rag_search_query = f"{dest_query} {custom_notes}".strip()
    rag_chunks = rag.retrieve(dest, rag_search_query, interests=prefs.get("interests"), top_k=5)
    neighborhood_clusters = rag.get_neighborhood_clusters(dest)

    corridor_intel: dict[str, Any] = {}
    if origin:
        corridor_intel = rag.retrieve_corridor_intelligence(origin, dest, top_k=4)

    return {
        "status": "researching",
        "progress_message": progress,
        "travel_options": travel_options,
        "travel_advisories": travel_advisories,
        "_travel_options": travel_options,
        "_travel_advisories": travel_advisories,
        "_corridor_raw": corridor_intel,
        "_research_raw": {
            "findings": findings,
            "corridor_findings": corridor_findings,
            "corridor_intel": corridor_intel,
            "weather": weather,
            "exchange_rates": exchange_rates,
            "travel_advisories": travel_advisories,
            "query": dest_query,
            "custom_notes": custom_notes,
            "rag_chunks": rag_chunks,
            "neighborhood_clusters": neighborhood_clusters,
        },
    }


async def research_agent(state: TripState) -> dict[str, Any]:
    """Distil fetched context + RAG intelligence into highlights + tips via LLM."""
    prefs = state["preferences"]
    notes = state.get("revision_notes", [])
    raw = state.get("_research_raw") or {}
    findings = raw.get("findings", [])
    corridor_findings = raw.get("corridor_findings", [])
    corridor_intel = raw.get("corridor_intel", {})
    weather = raw.get("weather", {})
    exchange_rates = raw.get("exchange_rates", {})
    rag_chunks = raw.get("rag_chunks", [])
    rag_neighborhoods = raw.get("neighborhood_clusters", [])

    origin = (prefs.get("origin") or "").strip()
    dest = prefs["destination"]
    custom_notes = (prefs.get("custom_notes") or state.get("custom_notes") or raw.get("custom_notes") or "").strip()

    rag_text = "\n".join(f"- [{c['source']}] {c['content']}" for c in rag_chunks) or "No pre-indexed RAG context."
    corridor_chunks = corridor_intel.get("corridor_chunks", [])
    corridor_text = "\n".join(f"- [{c['source']}] {c['content']}" for c in corridor_chunks) if corridor_chunks else "None"

    clean_findings = _format_snippets(findings, max_items=4, max_chars=250)
    clean_corridor = _format_snippets(corridor_findings, max_items=3, max_chars=220)

    corridor_prompt_snippet = ""
    if origin:
        corridor_prompt_snippet = (
            f"Origin (Traveling from): {origin}\n"
            f"Travel Corridor RAG Intelligence:\n{corridor_text}\n"
            f"Live Corridor Transit Findings:\n{clean_corridor}\n"
        )

    custom_vision_snippet = f"Traveler's Stated Trip Vision & Style Requests: '{custom_notes}'\n" if custom_notes else ""

    llm = get_llm()
    distilled = await llm.complete_json(
        system=(
            "You are an expert travel research analyst. Summarise findings, grounded RAG domain "
            "intelligence, and neighborhood clusters into a structured JSON schema."
        ),
        user=(
            f"Destination: {dest}\n"
            f"Interests: {prefs['interests']}\n"
            f"{custom_vision_snippet}"
            f"{corridor_prompt_snippet}"
            f"Grounded RAG Context:\n{rag_text}\n"
            f"Key Web Findings:\n{clean_findings}\n"
            f"Current Exchange Rates (Base {prefs['currency']}): {exchange_rates}\n"
            f"Human feedback (if any): {notes[-1] if notes else 'none'}\n"
            "Return JSON matching ResearchResultSchema with highlights, tips, local_currency_hint, "
            "neighborhoods (list of {name, vibe, key_attractions}), recommended_lodging_area, and grounded_facts."
        ),
        schema=ResearchResultSchema,
    )

    sources = (
        [c["source"] for c in rag_chunks]
        + corridor_intel.get("sources", [])
        + [f.get("url") for f in findings + corridor_findings if f.get("url")]
    )
    seen_src: set[str] = set()
    clean_sources = [s for s in sources if s and not (s in seen_src or seen_src.add(s))]

    neighborhoods = distilled.get("neighborhoods") or rag_neighborhoods

    return {
        "status": "planning",
        "progress_message": f"Research done — building your day-by-day plan for {dest}…",
        "custom_notes": custom_notes,
        "research": {
            "highlights": distilled.get("highlights", []),
            "tips": distilled.get("tips", []),
            "local_currency_hint": distilled.get("local_currency_hint", "Unknown currency."),
            "neighborhoods": neighborhoods,
            "recommended_lodging_area": distilled.get("recommended_lodging_area", ""),
            "grounded_facts": distilled.get("grounded_facts", []),
            "weather": weather,
            "sources": clean_sources,
        },
        "rag_context": rag_chunks,
        "rag_sources": clean_sources,
        "neighborhood_clusters": neighborhoods,
        "_corridor_raw": corridor_intel,
        "_research_raw": {},
    }


# ─── Node 3: planner agent ────────────────────────────────────────────
async def planner_agent(state: TripState) -> dict[str, Any]:
    """Turn research + budget into a concrete draft itinerary.

    Enforces the Lodging Anchor pattern (persistent base rather than repeated
    hourly activity), Geographic Clustering (neighborhood grouping to eliminate
    transit friction), and Inter-City Travel Corridor customization.
    """
    prefs = state["preferences"]
    research = state.get("research", {})
    notes = state.get("revision_notes", [])
    rag_context = state.get("rag_context", [])
    clusters = state.get("neighborhood_clusters", [])
    corridor_intel = state.get("_corridor_raw") or {}

    origin = (prefs.get("origin") or "").strip()
    dest = prefs["destination"]

    import datetime as dt

    nights = max(
        (dt.date.fromisoformat(prefs["end_date"])
         - dt.date.fromisoformat(prefs["start_date"])).days,
        1,
    )

    budget = allocate_budget(prefs["budget"], prefs["currency"], nights, prefs["travelers"])
    packing_list = generate_packing_list(
        dest,
        research.get("weather", {}),
        nights,
        prefs["interests"],
    )

    # Establish lodging anchor base from RAG / budget heuristics
    rag = get_rag_engine()
    lodging_recommendation = rag.get_lodging_anchor_recommendation(
        dest,
        budget_tier="moderate" if budget["lodging_per_night"] >= 100 else "budget",
    )
    lodging_recommendation["estimated_nightly_rate"] = budget.get("lodging_per_night")

    rag_text = "\n".join(f"- {c['content']}" for c in rag_context[:3]) or "None"
    clusters_text = ", ".join(
        c.get("name", "") for c in clusters if isinstance(c, dict) and c.get("name")
    ) or "Central District"

    corridor_chunks = corridor_intel.get("corridor_chunks", [])
    corridor_text = "\n".join(f"- {c['content']}" for c in corridor_chunks[:3]) if corridor_chunks else "None"
    custom_notes = (prefs.get("custom_notes") or state.get("custom_notes") or "").strip()

    # Reuse travel options if already fetched concurrently in research_fetch
    existing_travel = state.get("_travel_options") or state.get("travel_options")
    if not existing_travel or not existing_travel.get("hotels"):
        travel_task = fetch_travel_options(prefs)
    else:
        async def _use_cached(): return existing_travel
        travel_task = _use_cached()

    # If top hotel is already discovered by SerpAPI, anchor the prompt with it
    from app.services.travel_picks import _parse_price
    if existing_travel and existing_travel.get("recommendations", {}).get("hotels"):
        top_h = existing_travel["recommendations"]["hotels"][0].get("offer")
        if top_h:
            h_price = _parse_price(top_h.get("price")) or budget.get("lodging_per_night")
            lodging_recommendation = {
                "name": top_h.get("name") or lodging_recommendation["name"],
                "neighborhood": f"Central {dest.split(',')[0]}",
                "address": f"{dest.split(',')[0]}, {dest}",
                "check_in_time": "15:00",
                "check_out_time": "11:00",
                "estimated_nightly_rate": h_price,
                "currency": prefs.get("currency", "USD"),
                "why_recommended": f"Top rated ({top_h.get('rating', '4.5')}★) verified hotel matching budget.",
            }

    corridor_instructions = ""
    if origin:
        corridor_instructions = (
            f"4. TRAVEL CORRIDOR (FROM {origin} TO {dest}):\n"
            "Generate a JourneyCorridorSchema object with:\n"
            "- recommended_transit_mode: specific best transit (e.g. 'Express Rail (Vande Bharat, ~4.5 hrs)', 'Non-stop Flight (~1h 15m)', 'Express Highway Drive (~5 hrs)').\n"
            "- travel_time_estimate: estimated door-to-door transit time.\n"
            "- arrival_transfer_guidance: specific guidance on reaching the lodging anchor from arrival terminal/station.\n"
            "- climate_and_cultural_contrast: advice on climate/customs differences between origin and destination.\n"
            "- return_departure_tip: check-out and departure transit instructions on the final day.\n"
            "- grounded_route_facts: key bullet points extracted from corridor intelligence.\n"
        )

    custom_vision_instructions = ""
    if custom_notes:
        custom_vision_instructions = (
            f"5. TRAVELER'S CUSTOM VISION & PREFERENCES (TOP PRIORITY):\n"
            f"The traveler explicitly specified: '{custom_notes}'. "
            "You MUST customize the itinerary pace, day themes, venue selection, and dining to satisfy this exact vision.\n"
        )

    llm = get_llm()
    planner_result, travel_options = await asyncio.gather(
        llm.complete_json(
            system=(
                "You are an expert itinerary planner producing a precise, review-ready draft.\n"
                "CRITICAL ARCHITECTURAL CONSTRAINTS:\n"
                "1. LODGING ANCHOR PATTERN: Accommodation is a persistent state base. "
                "EVERY SINGLE DAY MUST BEGIN WITH MORNING DEPARTURE FROM THE HOTEL (or Day 1 arrival check-in) "
                "AND MUST CLOSE THE DAILY CIRCUIT BY ENDING WITH RETURNING TO THE HOTEL IN THE EVENING (e.g. '09:30 PM - Return to Hotel').\n"
                "2. 12-HOUR AM/PM TIMINGS & RICH CONTENT: Use clean 12-hour format (e.g. '09:00 AM', '11:30 AM', '02:00 PM', '05:30 PM', '08:00 PM', '09:30 PM'). "
                "For every activity, provide an engaging 2-3 sentence description explaining the experience, what to order/see, practical tips, opening hours, duration, and cost.\n"
                "3. GEOGRAPHIC CLUSTERING: Each day MUST be strictly clustered around a single geographic district or adjacent neighborhood. "
                "Group attractions within 15-20 minutes walking or short transit of each other.\n"
                "4. Use REAL venue and landmark names (e.g. 'Parmarth Niketan', 'São Jorge Castle'). Respect exact trip dates.\n"
                f"{corridor_instructions}"
                f"{custom_vision_instructions}"
            ),
            user=(
                f"Origin (Traveling from): {origin or 'Not specified'}\n"
                f"Destination: {dest}\n"
                f"Custom Trip Vision / Traveler Notes: {custom_notes or 'Standard sightseeing'}\n"
                f"Start Date: {prefs['start_date']}\n"
                f"End Date: {prefs['end_date']}\n"
                f"Travelers: {prefs['travelers']}\n"
                f"Budget breakdown: {budget}\n"
                f"Persistent Lodging Anchor Base: {lodging_recommendation}\n"
                f"Available Neighborhood Clusters for Daily Grouping: {clusters_text}\n"
                f"Travel Corridor RAG Intelligence:\n{corridor_text}\n"
                f"Highlights to include: {research.get('highlights', [])}\n"
                f"Research tips: {research.get('tips', [])}\n"
                f"Grounded Domain Facts: {rag_text}\n"
                f"Weather Data: {research.get('weather', {})}\n"
                f"Human feedback to incorporate (if any): {notes[-1] if notes else 'none'}\n"
                "Return JSON conforming to PlannerResultSchema with keys: summary, lodging_anchor, journey_corridor, "
                "days (each with day_number, date, focus, neighborhood_cluster, weather_note, activities), notes, transit_strategy."
            ),
            schema=PlannerResultSchema,
        ),
        travel_task,
    )

    schedule = planner_result.get("days", [])
    lodging_anchor = planner_result.get("lodging_anchor") or lodging_recommendation
    journey_corridor = planner_result.get("journey_corridor")

    if origin and not journey_corridor:
        journey_corridor = {
            "departure_city": origin,
            "arrival_city": dest,
            "recommended_transit_mode": f"Direct transit / flight from {origin} to {dest}",
            "travel_time_estimate": "Estimated 2–5 hours door-to-door depending on chosen connection",
            "arrival_transfer_guidance": f"Take official airport/station transfer directly to {lodging_anchor.get('neighborhood', 'central')} to check in.",
            "climate_and_cultural_contrast": f"Plan for regional climate differences between {origin} and {dest}.",
            "return_departure_tip": f"Check out of {lodging_anchor.get('name', 'hotel')} and allow 2.5 hours transfer time before departure.",
            "grounded_route_facts": [c["content"][:120] for c in corridor_chunks[:2]],
        }

    # Enrich Journey Corridor with closest airport and distance calculations
    if origin and journey_corridor:
        from app.services.places_suggest import suggest_places
        if not journey_corridor.get("departure_airport"):
            orig_sugg = suggest_places(origin, limit=1)
            if orig_sugg:
                o_item = orig_sugg[0]
                d_km = o_item.get("distance_km", 0)
                d_text = f" · {d_km} km from center" if d_km > 0 else " · Direct Airport Hub"
                journey_corridor["departure_airport"] = f"{o_item.get('closest_airport_name', 'Airport')} ({o_item.get('iata', '')}){d_text}"
        if not journey_corridor.get("arrival_airport"):
            dest_sugg = suggest_places(dest, limit=1)
            if dest_sugg:
                d_item = dest_sugg[0]
                d_km = d_item.get("distance_km", 0)
                d_text = f" · {d_km} km transfer" if d_km > 0 else " · Direct Airport Hub"
                journey_corridor["arrival_airport"] = f"{d_item.get('closest_airport_name', 'Airport')} ({d_item.get('iata', '')}){d_text}"
                journey_corridor["arrival_airport_distance_km"] = d_km

    if not schedule:
        schedule = build_day_schedule(
            prefs["start_date"],
            prefs["end_date"],
            research.get("highlights", []),
            research.get("weather", {}),
        )

    draft_route_map = await build_route_map(dest, schedule, lodging_anchor=lodging_anchor)

    return {
        "status": "awaiting_review",
        "progress_message": "Draft ready — pick optional flight/hotel, then approve.",
        "travel_options": travel_options,
        "travel_selections": {},
        "lodging_anchor": lodging_anchor,
        "journey_corridor": journey_corridor,
        "custom_notes": custom_notes,
        "route_map": draft_route_map,
        "_route_map": draft_route_map,
        "draft_itinerary": {
            "summary": planner_result.get("summary", ""),
            "lodging_anchor": lodging_anchor,
            "journey_corridor": journey_corridor,
            "custom_notes": custom_notes,
            "budget": budget,
            "days": schedule,
            "packing_list": packing_list,
            "tips": research.get("tips", []),
            "planner_notes": planner_result.get("notes", []),
            "transit_strategy": planner_result.get("transit_strategy", ""),
            "local_currency_hint": research.get("local_currency_hint", ""),
            "weather": research.get("weather", {}),
            "weather_available": research.get("weather", {}).get("available", False),
            "route_map": draft_route_map,
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
        custom_days = decision.get("customized_days")
        if isinstance(custom_days, list) and custom_days:
            current_draft = dict(state.get("draft_itinerary") or {})
            current_draft["days"] = custom_days
            update["draft_itinerary"] = current_draft

        selections = decision.get("travel_selections")
        if isinstance(selections, dict):
            update["travel_selections"] = {
                "flight": selections.get("flight"),
                "hotel": selections.get("hotel"),
            }
            # If user selected a hotel, elevate it to be the persistent lodging anchor
            if selections.get("hotel") and isinstance(selections["hotel"], dict):
                hotel = selections["hotel"]
                current_anchor = dict(state.get("lodging_anchor") or {})
                current_anchor["name"] = hotel.get("name") or current_anchor.get("name", "Selected Hotel")
                if hotel.get("price"):
                    try:
                        clean_price = str(hotel["price"]).replace("$", "").replace("€", "").replace("£", "").split()[0]
                        current_anchor["estimated_nightly_rate"] = float(clean_price)
                    except (ValueError, TypeError):
                        pass
                current_anchor["why_recommended"] = "User-selected hotel from review gate."
                update["lodging_anchor"] = current_anchor

    if action in ("reject", "modify") and feedback:
        notes = list(state.get("revision_notes", []))
        notes.append(f"[{action}] {feedback}")
        update["revision_notes"] = notes
        update["revision_count"] = state.get("revision_count", 0) + 1
    return update


# ─── Node 5: finalize (expand + route map, then pack) ───────────────────
async def finalize_expand(state: TripState) -> dict[str, Any]:
    """Expand approved draft + build route map anchored to lodging base and journey corridor."""
    prefs = state["preferences"]
    draft = state.get("draft_itinerary", {})
    draft_days = draft.get("days", [])
    lodging_anchor = state.get("lodging_anchor") or draft.get("lodging_anchor") or {}
    journey_corridor = state.get("journey_corridor") or draft.get("journey_corridor")
    llm = get_llm()
    existing_travel = state.get("travel_options")
    dest = prefs["destination"]
    origin = prefs.get("origin") or ""
    custom_notes = state.get("custom_notes") or (prefs.get("custom_notes") or "").strip()

    expanded = await llm.complete_json(
        system=(
            "You are an expert travel planner finalizing an approved itinerary.\n"
            "Expand the skeletal day plan into a detailed, geographically clustered schedule.\n"
            "LODGING ANCHOR RULES:\n"
            "Each day starts by departing from the lodging anchor and ends by returning to it for the evening.\n"
            "On Day 1: Incorporate arrival from origin and hotel check-in/luggage drop.\n"
            "On Final Day: Incorporate hotel check-out and departure transit.\n"
            "Ensure expanded activity descriptions, pacing, and experiences directly reflect the traveler's stated custom vision.\n"
            "Use accurate venue/landmark names in location_name for map routing.\n"
            "Each activity needs: time (HH:MM), title, description (1-2 sentences), "
            "location_name (official name of the place), neighborhood, and activity_type (lodging/sightseeing/dining/culture/relaxation/transit).\n"
            "Do NOT add duplicate 'Stay at Hotel' entries in the middle of the day."
        ),
        user=(
            f"Origin (Departing from): {origin or 'Not specified'}\n"
            f"Destination: {dest}\n"
            f"Traveler's Custom Trip Vision: {custom_notes or 'Standard sightseeing'}\n"
            f"Lodging Anchor: {lodging_anchor}\n"
            f"Inter-City Journey Corridor: {journey_corridor}\n"
            f"Draft schedule: {draft_days}\n"
            f"Tips: {draft.get('tips', [])}\n"
            f"Planner notes: {draft.get('planner_notes', [])}\n"
            "Return JSON matching FinalizeResultSchema with key 'days', optional 'lodging_anchor', and optional 'journey_corridor'."
        ),
        schema=FinalizeResultSchema,
    )

    expanded_days = expanded.get("days") or draft_days
    if expanded.get("journey_corridor"):
        journey_corridor = expanded["journey_corridor"]

    from app.services.serp import search_nearby_restaurants
    
    async def _inject_restaurants(day: dict[str, Any]) -> None:
        activities = day.get("activities") or []
        if not activities:
            return

        mid_idx = max(0, len(activities) // 2 - 1)
        mid_act = activities[mid_idx]
        lunch_task = None
        if isinstance(mid_act, dict) and mid_act.get("location_name"):
            lunch_task = search_nearby_restaurants(mid_act["location_name"], dest, "Lunch")
            
        last_act = activities[-1]
        dinner_task = None
        if isinstance(last_act, dict) and last_act.get("location_name"):
            dinner_task = search_nearby_restaurants(last_act["location_name"], dest, "Dinner")

        if not lunch_task and not dinner_task:
            return

        results = await asyncio.gather(
            lunch_task if lunch_task else asyncio.sleep(0),
            dinner_task if dinner_task else asyncio.sleep(0)
        )
        lunch_res, dinner_res = results

        if lunch_res:
            activities.append(lunch_res)
        if dinner_res:
            activities.append(dinner_res)
            
        def _time_key(act: Any) -> str:
            if not isinstance(act, dict): return "99:99"
            return act.get("time") or "99:99"
            
        activities.sort(key=_time_key)
        day["activities"] = activities

    await asyncio.gather(*(_inject_restaurants(d) for d in expanded_days))

    travel_options = existing_travel
    if not travel_options:
        travel_options = await fetch_travel_options(prefs)

    route_map = await build_route_map(dest, expanded_days, lodging_anchor=lodging_anchor)

    return {
        "status": "finalizing",
        "progress_message": "Expanding activities and mapping your route…",
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
    lodging_anchor = state.get("lodging_anchor") or draft.get("lodging_anchor") or {}
    journey_corridor = state.get("journey_corridor") or draft.get("journey_corridor")

    # Generate RFC 5545 calendar and smart packing list via MCP
    mcp_client = get_mcp_client()
    cal_ics = ""
    expanded_days = state.get("_expanded_days") or []
    lodging_name = (lodging_anchor.get("name") if isinstance(lodging_anchor, dict) else "") or "Curated Lodging"
    try:
        cal_ics = await mcp_client.generate_calendar(
            trip_title=f"{prefs['destination']} Itinerary",
            start_date=prefs.get("start_date", ""),
            days=expanded_days,
            lodging_name=lodging_name,
        )
        mcp_flow("Generated RFC 5545 calendar .ics feed via MCP", size=len(cal_ics))
    except Exception as exc:
        mcp_flow("MCP calendar generation fallback triggered", error=str(exc)[:60])

    weather_obj = draft.get("weather") or state.get("research", {}).get("weather") or {}
    weather_cond = weather_obj.get("summary") or "Fair"

    # MCP-refined packing list
    packing_list = draft.get("packing_list") or []
    try:
        mcp_packing = await mcp_client.generate_smart_packing(
            destination=prefs["destination"],
            weather_condition=weather_cond,
            duration_days=len(expanded_days) or 4,
            interests=prefs.get("interests") or [],
        )
        if mcp_packing:
            packing_list = mcp_packing
            mcp_flow("Generated weather-aware packing list via MCP", items=len(packing_list))
    except Exception:
        pass

    mcp_tools = [
        "fetch_travel_safety_and_etiquette",
        "generate_smart_packing_list",
        "generate_calendar_ics",
        "generate_itinerary_pdf",
    ]
    advisories = state.get("travel_advisories") or state.get("_travel_advisories") or {}

    final_plan: dict[str, Any] = {
        "destination": prefs["destination"],
        "dates": {"start": prefs["start_date"], "end": prefs["end_date"]},
        "travelers": prefs["travelers"],
        "summary": draft.get("summary", ""),
        "lodging_anchor": lodging_anchor,
        "journey_corridor": journey_corridor,
        "custom_notes": state.get("custom_notes") or (prefs.get("custom_notes") or ""),
        "budget": draft.get("budget", {}),
        "itinerary": expanded_days,
        "route_map": state.get("_route_map") or {},
        "travel_options": travel_options,
        "origin": prefs.get("origin") or "",
        "weather": weather_obj,
        "packing_list": packing_list,
        "local_currency_hint": draft.get("local_currency_hint", ""),
        "tips": draft.get("tips", []),
        "transit_strategy": draft.get("transit_strategy", ""),
        "rag_sources": state.get("rag_sources", []),
        "revisions": state.get("revision_count", 0),
        "approved": True,
        "calendar_ics": cal_ics,
        "travel_advisories": advisories,
        "mcp_tools_used": mcp_tools,
    }
    if selected:
        final_plan["selected_travel"] = selected

    return {
        "status": "completed",
        "progress_message": "Your trip is ready!",
        "final_plan": final_plan,
        "calendar_ics": cal_ics,
        "travel_advisories": advisories,
        "mcp_tools_used": mcp_tools,
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
