"""FastAPI application — the 5 endpoints, fully async.

Lifecycle of a plan:
  POST /plan              → kick off a new plan (returns plan_id immediately)
  GET  /plan/{id}         → reads the live checkpoint (poll fallback)
  GET  /plan/{id}/stream  → SSE stream of live graph execution events
  POST /plan/{id}/review  → resumes with Command(resume={action,feedback})
  GET  /plan/{id}/final   → returns the final plan once status == completed
"""
from __future__ import annotations

import logging
import uuid
import os
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse
from langgraph.types import Command

from app.services.auth import get_current_user, get_optional_user

from app.config import get_settings, graph_runs_in_background, prepare_checkpoint_path, stubs_enabled
from app.graph import build_graph
from app.graph_runner import invoke_graph, is_plan_running, start_graph_run, stream_graph_events
from app.pipeline_log import (
    LOG as pipeline_log,
    flow,
    hitl_resume,
    http as log_http,
    setup_pipeline_logging,
    startup_banner,
)
from app.schemas import (
    PlaceChatRequest,
    PlaceChatResponse,
    PlaceReplaceRequest,
    PlaceReplaceResponse,
    AlternativeActivity,
    ActivityImageLookupRequest,
    ActivityImageLookupResponse,
    FinalPlanResponse,
    PlanCreatedResponse,
    PlanRequest,
    PlanStateResponse,
    ReviewRequest,
    UserSignupRequest,
    UserLoginRequest,
    UserResponse,
    AuthTokenResponse,
    CommunityTripCreateRequest,
    CommunityReviewCreateRequest,
    CommunityReviewResponse,
    CommunityTripResponse,
    CommunityTripDetailResponse,
    CommunityRemixRequest,
    CommunityRemixResponse,
)


logger = logging.getLogger(__name__)

_graph_init_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the checkpointer-backed graph once for the app's lifetime."""
    setup_pipeline_logging()
    get_settings.cache_clear()
    settings = get_settings()
    startup_banner(settings)

    # Initialize Supabase / SQLite database schema
    from app.services.db import init_db
    from app.services.community import seed_initial_community_trips
    try:
        await init_db()
        await seed_initial_community_trips()
    except Exception as exc:
        logger.warning("Database schema init deferred: %s", exc)

    # Vercel: skip startup SQLite — filesystem init is lazy in get_graph().
    if os.environ.get("VERCEL"):
        yield
        return
    async with build_graph() as graph:
        app.state.graph = graph
        yield


app = FastAPI(
    title="AI Travel Planner",
    version="0.1.0",
    description=(
        "Multi-agent travel planner (LangGraph) with human-in-the-loop "
        "approval. See /docs for interactive Swagger."
    ),
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    detail = str(exc) or exc.__class__.__name__
    return JSONResponse(status_code=500, content={"detail": detail[:300]})


@app.middleware("http")
async def log_requests(request, call_next):
    """Log API traffic — polls at DEBUG to avoid terminal spam."""
    path = request.url.path
    if path.startswith("/css") or path.startswith("/js") or path.endswith((".css", ".js", ".svg", ".png", ".ico")):
        return await call_next(request)

    import time

    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000

    is_poll = request.method == "GET" and path.startswith("/plan/") and path.count("/") == 2
    if is_poll:
        pipeline_log.debug("HTTP GET %s → %s (%.0fms)", path, response.status_code, ms)
    elif path.startswith(("/plan", "/health")):
        log_http(request.method, path, status=response.status_code, ms=ms)
    return response


def _thread(plan_id: str) -> dict[str, Any]:
    """LangGraph config — thread_id == plan_id plus LangSmith metadata and tags."""
    return {
        "configurable": {"thread_id": plan_id},
        "run_name": f"wayfarer_{plan_id[:8]}",
        "tags": ["wayfarer", "travel-planner", "hitl", "production"],
        "metadata": {
            "plan_id": plan_id,
            "system": "Wayfarer Travel Planner",
        },
    }


async def _current_state(plan_id: str) -> dict[str, Any] | None:
    graph = await get_graph()
    snapshot = await graph.aget_state(_thread(plan_id))
    if not snapshot or not snapshot.values:
        return None
    return snapshot.values


def _plan_response(plan_id: str, state: dict[str, Any] | None) -> PlanStateResponse:
    state = state or {}
    prefs = state.get("preferences") or {}
    return PlanStateResponse(
        plan_id=plan_id,
        status=state.get("status", "unknown"),
        progress_message=state.get("progress_message") or "",
        preferences=prefs,
        custom_notes=state.get("custom_notes") or prefs.get("custom_notes") or "",
        research=state.get("research"),
        draft_itinerary=state.get("draft_itinerary"),
        travel_options=state.get("travel_options"),
        lodging_anchor=state.get("lodging_anchor"),
        journey_corridor=state.get("journey_corridor"),
        route_map=state.get("route_map") or (state.get("draft_itinerary") or {}).get("route_map"),
        rag_sources=state.get("rag_sources", []),
        revision_count=state.get("revision_count", 0),
        revision_notes=state.get("revision_notes", []),
        travel_advisories=state.get("travel_advisories") or (state.get("final_plan") or {}).get("travel_advisories"),
        calendar_ics=state.get("calendar_ics") or (state.get("final_plan") or {}).get("calendar_ics"),
        mcp_tools_used=state.get("mcp_tools_used") or (state.get("final_plan") or {}).get("mcp_tools_used") or [],
    )

async def get_graph():
    """Lazily initialize graph if lifespan isn't supported (e.g. on Vercel)."""
    if getattr(app.state, "graph", None) is not None:
        return app.state.graph

    async with _graph_init_lock:
        if getattr(app.state, "graph", None) is not None:
            return app.state.graph

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from app.graph import _build_uncompiled

        settings = get_settings()
        db_path = prepare_checkpoint_path(settings.checkpoint_db)
        if not getattr(app.state, "checkpointer", None):
            cm = AsyncSqliteSaver.from_conn_string(db_path)
            app.state.checkpointer = await cm.__aenter__()
            app.state._checkpointer_cm = cm

        app.state.graph = _build_uncompiled().compile(
            checkpointer=app.state.checkpointer
        )
        return app.state.graph


@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    """Quick sanity check — confirms whether the server is using real APIs or stubs, RAG, LangSmith."""
    settings = get_settings()
    return {
        "status": "ok",
        "mode": "live" if not stubs_enabled() else "stubs",
        "gemini_model": settings.gemini_model,
        "rag_enabled": settings.rag_enabled,
        "langsmith_configured": bool(settings.langsmith_api_key or settings.langsmith_tracing),
        "google_api_key_set": bool(settings.google_api_key),
        "tavily_api_key_set": bool(settings.tavily_api_key),
        "openrouteservice_api_key_set": bool(settings.openrouteservice_api_key),
        "serpapi_key_set": bool(settings.serpapi_key),
        "carto_api_key_set": bool(settings.carto_api_key),
        "carto_api_key": settings.carto_api_key, # provide to frontend map client
        "mcp_enabled": bool(getattr(settings, "mcp_enabled", True)),
        "database_configured": bool(settings.database_url),
        "database_type": "postgres" if (not stubs_enabled() and settings.database_url.startswith(("postgres://", "postgresql://"))) else "sqlite",
    }


@app.post("/images/lookup", response_model=ActivityImageLookupResponse, tags=["images"])
async def lookup_activity_image(req: ActivityImageLookupRequest) -> ActivityImageLookupResponse:
    """Resolve a Wikipedia thumbnail for one activity (parallel-friendly, cached)."""
    from app.tools import lookup_activity_image as _lookup

    result = await _lookup(req.destination, req.title, req.location_name)
    return ActivityImageLookupResponse(**result)


@app.post("/plan", response_model=PlanCreatedResponse, status_code=201, tags=["plan"])
async def create_plan(req: PlanRequest) -> PlanCreatedResponse:
    """Kick off a new plan. Runs until the graph hits the HITL interrupt."""
    plan_id = str(uuid.uuid4())
    flow(
        "CREATE plan",
        dest=req.destination,
        budget=f"{req.budget} {req.currency}",
        from_city=req.origin or "-",
    )
    initial = {
        "plan_id": plan_id,
        "preferences": {
            "destination": req.destination,
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "budget": req.budget,
            "currency": req.currency,
            "travelers": req.travelers,
            "interests": req.interests,
            "origin": req.origin.strip(),
            "flight_destination": req.flight_destination.strip(),
            "include_flights": req.include_flights,
            "include_hotels": req.include_hotels,
            "custom_notes": req.custom_notes.strip(),
        },
        "custom_notes": req.custom_notes.strip(),
    }
    if graph_runs_in_background():
        flow("CREATE background job queued")
        start_graph_run(plan_id, initial)
        return PlanCreatedResponse(plan_id=plan_id, status="researching")
    await invoke_graph(plan_id, initial)
    state = await _current_state(plan_id)
    status = (state or {}).get("status", "researching")
    flow("CREATE finished", status=status)
    return PlanCreatedResponse(
        plan_id=plan_id, status=status
    )


@app.get("/plan/{plan_id}", response_model=PlanStateResponse, tags=["plan"])
async def get_plan(plan_id: str) -> PlanStateResponse:
    """Return the current checkpointed state (poll until awaiting_review)."""
    state = await _current_state(plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")
    return _plan_response(plan_id, state)


@app.post("/plan/{plan_id}/review", response_model=PlanStateResponse, tags=["plan"])
async def review_plan(plan_id: str, req: ReviewRequest) -> PlanStateResponse:
    """Resume the paused graph with the human's decision.

    Command(resume=...) feeds the payload straight back into the interrupt()
    call inside human_gate, and the graph continues: approve→finalize,
    reject→research, modify→planner.
    """
    state = await _current_state(plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")
    if state.get("status") != "awaiting_review":
        raise HTTPException(
            409,
            f"Plan is '{state.get('status')}', not awaiting_review; cannot review now.",
        )

    if is_plan_running(plan_id):
        raise HTTPException(409, "Plan is already being processed; please wait.")

    resume = {
        "action": req.action,
        "feedback": req.feedback,
        "travel_selections": req.travel_selections,
    }
    sel = req.travel_selections or {}
    hitl_resume(
        req.action,
        feedback=req.feedback or "",
        has_travel=bool(sel.get("flight") or sel.get("hotel")),
    )
    kickoff_status = {
        "approve": "finalizing",
        "modify": "planning",
        "reject": "researching",
    }[req.action]
    kickoff_message = {
        "approve": "Approved — expanding your itinerary and mapping your route…",
        "modify": "Sending your notes to the planner agent…",
        "reject": "Restarting — fresh web search and weather for your trip…",
    }[req.action]

    if graph_runs_in_background():
        flow("REVIEW background job queued", action=req.action)
        start_graph_run(plan_id, Command(resume=resume))
        return PlanStateResponse(
            plan_id=plan_id,
            status=kickoff_status,
            progress_message=kickoff_message,
            preferences=state.get("preferences"),
            custom_notes=state.get("custom_notes") or (state.get("preferences") or {}).get("custom_notes") or "",
            research=state.get("research"),
            draft_itinerary=state.get("draft_itinerary"),
            lodging_anchor=state.get("lodging_anchor"),
            journey_corridor=state.get("journey_corridor"),
            rag_sources=state.get("rag_sources", []),
            revision_count=state.get("revision_count", 0),
            revision_notes=state.get("revision_notes", []),
            travel_advisories=state.get("travel_advisories"),
            calendar_ics=state.get("calendar_ics"),
            mcp_tools_used=state.get("mcp_tools_used") or [],
        )

    await invoke_graph(plan_id, Command(resume=resume))
    new_state = await _current_state(plan_id)
    flow("REVIEW finished", status=(new_state or {}).get("status"))
    return _plan_response(plan_id, new_state)


@app.get("/plan/{plan_id}/stream", tags=["plan"])
async def stream_plan(plan_id: str, request: Request):
    """SSE stream of real-time graph execution events.

    Opens a Server-Sent Events connection. The frontend listens via
    EventSource and receives node-level progress, the HITL pause payload,
    or the final completed plan — all pushed in real-time instead of polled.
    """
    import json

    state = await _current_state(plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")

    status = state.get("status", "unknown")

    # Determine what to stream based on current plan status.
    # If the plan is already completed or awaiting review, send one-shot state.
    if status == "completed":
        async def _completed_gen():
            event = {
                "event": "completed",
                "node": "end",
                "status": "completed",
                "final_plan": state.get("final_plan", {}),
            }
            yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(
            _completed_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    if status == "awaiting_review":
        async def _paused_gen():
            event = {
                "event": "paused",
                "node": "human_gate",
                "status": "awaiting_review",
                "progress_message": state.get("progress_message", ""),
                "plan_id": plan_id,
                "preferences": state.get("preferences"),
                "research": state.get("research"),
                "draft_itinerary": state.get("draft_itinerary"),
                "travel_options": state.get("travel_options"),
                "revision_count": state.get("revision_count", 0),
                "revision_notes": state.get("revision_notes", []),
            }
            yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(
            _paused_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # For in-progress plans, we need the graph to already be running.
    # Send a "waiting" event and let the frontend poll/retry.
    async def _in_progress_gen():
        event = {
            "event": "state_update",
            "node": "unknown",
            "status": status,
            "message": state.get("progress_message", f"Plan is {status}…"),
        }
        yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _in_progress_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/plan/stream", status_code=201, tags=["plan"])
async def create_plan_sse(req: PlanRequest, request: Request):
    """Create a plan AND stream its execution via SSE.

    This is the primary entry-point for SSE-capable clients. Instead of
    returning a JSON body and requiring polling, it returns a
    text/event-stream response that pushes node events in real-time.
    """
    import json

    plan_id = str(uuid.uuid4())
    flow(
        "CREATE plan (SSE)",
        dest=req.destination,
        budget=f"{req.budget} {req.currency}",
        from_city=req.origin or "-",
    )
    initial = {
        "plan_id": plan_id,
        "preferences": {
            "destination": req.destination,
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "budget": req.budget,
            "currency": req.currency,
            "travelers": req.travelers,
            "interests": req.interests,
            "origin": req.origin.strip(),
            "flight_destination": req.flight_destination.strip(),
            "include_flights": req.include_flights,
            "include_hotels": req.include_hotels,
            "custom_notes": req.custom_notes.strip(),
        },
        "custom_notes": req.custom_notes.strip(),
    }

    async def _sse_gen():
        # First event: send the plan_id so the frontend knows it.
        yield f"data: {json.dumps({'event': 'plan_created', 'plan_id': plan_id})}\n\n"
        async for event in stream_graph_events(plan_id, initial):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/plan/{plan_id}/review/stream", tags=["plan"])
async def review_plan_sse(plan_id: str, req: ReviewRequest, request: Request):
    """Resume the paused graph with the human's decision AND stream SSE events.

    Combines the review action with real-time streaming — the frontend gets
    node-by-node progress without needing a separate GET /stream call.
    """
    import json

    state = await _current_state(plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")
    if state.get("status") != "awaiting_review":
        raise HTTPException(
            409,
            f"Plan is '{state.get('status')}', not awaiting_review; cannot review now.",
        )
    if is_plan_running(plan_id):
        raise HTTPException(409, "Plan is already being processed; please wait.")

    resume = {
        "action": req.action,
        "feedback": req.feedback,
        "travel_selections": req.travel_selections,
    }
    sel = req.travel_selections or {}
    hitl_resume(
        req.action,
        feedback=req.feedback or "",
        has_travel=bool(sel.get("flight") or sel.get("hotel")),
    )

    async def _sse_gen():
        async for event in stream_graph_events(plan_id, Command(resume=resume)):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/plan/{plan_id}/final", response_model=FinalPlanResponse, tags=["plan"])
async def get_final(plan_id: str) -> FinalPlanResponse:
    """Return the finalised plan, or 409 if it isn't approved yet."""
    state = await _current_state(plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")
    if state.get("status") != "completed":
        raise HTTPException(
            409, f"Plan not finalised (status='{state.get('status')}')."
        )
    return FinalPlanResponse(
        plan_id=plan_id, status="completed", final_plan=state["final_plan"]
    )


@app.get("/mcp/tools", tags=["mcp"])
async def list_mcp_tools() -> dict[str, Any]:
    """Discover tools exposed by the Model Context Protocol (MCP) server."""
    from app.services.mcp_client import get_mcp_client

    client = get_mcp_client()
    tools = await client.list_tools()
    return {
        "mcp_version": "2024-11-05",
        "server_name": "wayfarer-travel-concierge",
        "tools_count": len(tools),
        "tools": tools,
    }


@app.get("/plan/{plan_id}/pdf", tags=["export"])
async def download_plan_pdf(plan_id: str) -> Response:
    """Download publication-grade visual PDF itinerary generated via MCP."""
    import base64
    import re
    from app.mcp.pdf_generator import build_itinerary_pdf
    from app.services.mcp_client import get_mcp_client

    state = await _current_state(plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")

    plan_data = state.get("final_plan") or state.get("draft_itinerary") or state
    dest = (
        plan_data.get("destination")
        or (state.get("preferences") or {}).get("destination")
        or "Trip"
    )
    safe_dest = re.sub(r"[^\w\-]", "_", dest).strip("_")
    filename = f"Wayfarer_Itinerary_{safe_dest}.pdf"

    # Invoke PDF generator (via MCP client manager)
    mcp_client = get_mcp_client()
    try:
        mcp_res = await mcp_client.generate_pdf(plan_data)
        if isinstance(mcp_res, dict) and mcp_res.get("pdf_base64"):
            pdf_bytes = base64.b64decode(mcp_res["pdf_base64"])
        else:
            pdf_bytes = build_itinerary_pdf(plan_data)
    except Exception as exc:
        logger.warning("MCP PDF generation failed, falling back to direct builder: %s", exc)
        pdf_bytes = build_itinerary_pdf(plan_data)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


@app.get("/plan/{plan_id}/calendar.ics", tags=["export"])
async def download_calendar_ics(plan_id: str) -> Response:
    """Download RFC 5545 iCalendar feed (.ics) generated via MCP."""
    import re
    from app.services.mcp_client import get_mcp_client

    state = await _current_state(plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")

    cal_ics = state.get("calendar_ics") or (state.get("final_plan") or {}).get("calendar_ics")
    dest = (state.get("preferences") or {}).get("destination") or "Trip"
    safe_dest = re.sub(r"[^\w\-]", "_", dest).strip("_")
    filename = f"Wayfarer_{safe_dest}.ics"

    if not cal_ics:
        plan_data = state.get("final_plan") or state.get("draft_itinerary") or state
        days = plan_data.get("itinerary") or plan_data.get("days") or []
        lodging = state.get("lodging_anchor") or (state.get("draft_itinerary") or {}).get("lodging_anchor") or {}
        lodging_name = lodging.get("name") if isinstance(lodging, dict) else "Curated Lodging"
        mcp_client = get_mcp_client()
        cal_ics = await mcp_client.generate_calendar(
            trip_title=f"{dest} Itinerary",
            start_date=(state.get("preferences") or {}).get("start_date", ""),
            days=days,
            lodging_name=lodging_name,
        )

    return Response(
        content=cal_ics,
        media_type="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


@app.post("/place/chat", response_model=PlaceChatResponse, tags=["place"])
async def place_chat(req: PlaceChatRequest) -> PlaceChatResponse:
    from app.services.llm import GeminiClient, StubLLM
    
    if stubs_enabled():
        return PlaceChatResponse(
            reply=f"While incredibly atmospheric, {req.place_name} can get quite busy. It's fantastic if you love lively energy, but for a quiet intimate night, you might want to ask for a corner table.",
            vibe_tags=["Lively", "Atmospheric", "Book Ahead"],
            suggested_followups=["Is there outdoor seating?", "What's the best time to visit?"]
        )
        
    system_prompt = (
        "You are a hyper-local insider and 'vibe concierge' for {destination}. "
        "The user is asking about: {place_name} (Category: {category}). "

        "Give a direct, honest, vivid, and highly conversational answer. "
        "Don't sound like a Wikipedia article. Talk about the crowd, the noise, the dress code, the hidden tips. "
        "Be concise (2-3 short paragraphs max)."
    ).format(destination=req.destination, place_name=req.place_name, category=req.category)
    
    # Construct history
    history_text = ""
    for msg in req.chat_history[-4:]:
        role = msg.get("role", "user")
        history_text += f"{role.upper()}: {msg.get('content', '')}\n"
        
    user_prompt = f"Description: {req.description}\n\nChat History:\n{history_text}\n\nUSER QUESTION: {req.query}"
    
    llm = GeminiClient()
    parsed = await llm.complete_json(system_prompt, user_prompt, PlaceChatResponse)
    return PlaceChatResponse(**parsed)

@app.post("/place/replace", response_model=PlaceReplaceResponse, tags=["place"])
async def place_replace(req: PlaceReplaceRequest) -> PlaceReplaceResponse:
    from app.services.llm import GeminiClient, StubLLM
    
    if stubs_enabled():
        return PlaceReplaceResponse(
            alternatives=[
                AlternativeActivity(
                    title="Vashishta Gufa Cave",
                    location_name="Vashishta Gufa",
                    description="A highly spiritual, quiet ancient meditation cave right on the banks of the Ganges. Far less crowded than other spots.",
                    time="09:00 AM",
                    estimated_cost=0,
                    category="Spiritual"
                ),
                AlternativeActivity(
                    title="Neer Garh Waterfall",
                    location_name="Neer Garh",
                    description="A scenic nature hike with natural pools. Perfect refreshing morning alternative.",
                    time="09:30 AM",
                    estimated_cost=5,
                    category="Nature"
                ),
                AlternativeActivity(
                    title="Secret Riverside Cafe",
                    location_name="Tapovan",
                    description="A cozy vegan cafe with undisturbed river views, perfect for a relaxed start.",
                    time="10:00 AM",
                    estimated_cost=15,
                    category="Dining"
                )
            ]
        )
        
    system_prompt = (
        "You are an expert travel planner for {destination}. "
        "The user wants to REPLACE the activity '{current_place}' on Day {day_number} ({time_slot}). "
        "STRICT CONSTRAINT: Do NOT suggest ANY of these places already in the itinerary: {existing}. "
        "Suggest 3 DISTINCT, highly rated, logical alternatives that fit the time slot. "
        "If the user provided a preference ('{pref}'), tailor the suggestions heavily towards it."
    ).format(
        destination=req.destination,
        current_place=req.current_place,
        day_number=req.day_number,
        time_slot=req.time_slot,
        existing=", ".join(req.existing_places) if req.existing_places else "None",
        pref=req.user_preference or "Make them generally awesome and highly rated."
    )
    
    user_prompt = "Give me 3 unique alternatives."
    
    llm = GeminiClient()
    parsed = await llm.complete_json(system_prompt, user_prompt, PlaceReplaceResponse)
    return PlaceReplaceResponse(**parsed)


# ─── Supabase Authentication Endpoints ────────────────────────────────
@app.post("/auth/signup", response_model=AuthTokenResponse, status_code=201, tags=["auth"])
async def auth_signup(req: UserSignupRequest) -> AuthTokenResponse:
    """Create a new user account in Supabase PostgreSQL."""
    from app.services.auth import create_access_token, create_user

    user = await create_user(
        email=req.email,
        password=req.password,
        full_name=req.full_name,
        bio=req.bio,
        avatar_url=req.avatar_url,
    )
    token = create_access_token(user["id"], user["email"], user["full_name"])
    return AuthTokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse(
            id=str(user["id"]),
            email=user["email"],
            full_name=user["full_name"],
            avatar_url=user.get("avatar_url") or "",
            bio=user.get("bio") or "",
            created_at=str(user.get("created_at") or ""),
        ),
    )


@app.post("/auth/login", response_model=AuthTokenResponse, tags=["auth"])
async def auth_login(req: UserLoginRequest) -> AuthTokenResponse:
    """Authenticate user credentials and return a signed JWT Bearer session token."""
    from app.services.auth import authenticate_user, create_access_token

    user = await authenticate_user(req.email, req.password)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(user["id"], user["email"], user["full_name"])
    return AuthTokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse(
            id=str(user["id"]),
            email=user["email"],
            full_name=user["full_name"],
            avatar_url=user.get("avatar_url") or "",
            bio=user.get("bio") or "",
            created_at=str(user.get("created_at") or ""),
        ),
    )


@app.get("/auth/me", response_model=UserResponse, tags=["auth"])
async def auth_me(current_user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    """Return the authenticated user profile."""
    return UserResponse(
        id=str(current_user["id"]),
        email=current_user["email"],
        full_name=current_user["full_name"],
        avatar_url=current_user.get("avatar_url") or "",
        bio=current_user.get("bio") or "",
        created_at=str(current_user.get("created_at") or ""),
    )


# ─── Community Itinerary Sharing & Structured Reviews ─────────────────
@app.get("/community/trips", response_model=list[CommunityTripResponse], tags=["community"])
async def get_community_trips_feed(
    destination: str | None = None,
    tag: str | None = None,
    sort: str = "likes",
    limit: int = 40,
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> list[CommunityTripResponse]:
    """Retrieve community-shared itineraries with search, tag filters, and upvotes."""
    from app.services.community import list_community_trips

    trips = await list_community_trips(
        destination=destination,
        tag=tag,
        sort_by=sort,
        limit=limit,
        current_user_id=user["id"] if user else None,
    )
    return [
        CommunityTripResponse(
            id=str(t["id"]),
            user_id=str(t["user_id"]) if t.get("user_id") else None,
            author_name=t["author_name"],
            author_avatar=t.get("author_avatar") or "",
            destination=t["destination"],
            title=t["title"],
            description=t.get("description") or "",
            origin=t.get("origin"),
            duration_days=t.get("duration_days") or 1,
            budget=t.get("budget"),
            currency=t.get("currency") or "USD",
            travelers=t.get("travelers") or 1,
            tags=t.get("tags") or [],
            likes_count=t.get("likes_count") or 0,
            reviews_count=t.get("reviews_count") or 0,
            average_rating=float(t.get("average_rating") or 5.0),
            created_at=str(t.get("created_at") or ""),
            user_has_liked=bool(t.get("user_has_liked", False)),
        )
        for t in trips
    ]


@app.post("/community/trips", response_model=CommunityTripResponse, status_code=201, tags=["community"])
async def publish_community_trip(
    req: CommunityTripCreateRequest,
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> CommunityTripResponse:
    """Publish an itinerary to the public community feed."""
    from app.services.community import create_community_trip

    trip = await create_community_trip(req, user)
    return CommunityTripResponse(
        id=str(trip["id"]),
        user_id=str(trip["user_id"]) if trip.get("user_id") else None,
        author_name=trip["author_name"],
        author_avatar=trip.get("author_avatar") or "",
        destination=trip["destination"],
        title=trip["title"],
        description=trip.get("description") or "",
        origin=trip.get("origin"),
        duration_days=trip.get("duration_days") or 1,
        budget=trip.get("budget"),
        currency=trip.get("currency") or "USD",
        travelers=trip.get("travelers") or 1,
        tags=trip.get("tags") or [],
        likes_count=trip.get("likes_count") or 0,
        reviews_count=trip.get("reviews_count") or 0,
        average_rating=float(trip.get("average_rating") or 5.0),
        created_at=str(trip.get("created_at") or ""),
        user_has_liked=False,
    )


@app.get("/community/trips/{trip_id}", response_model=CommunityTripDetailResponse, tags=["community"])
async def get_community_trip_detail(
    trip_id: str,
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> CommunityTripDetailResponse:
    """Fetch complete itinerary details and structured community reviews."""
    from app.services.community import get_community_trip

    trip = await get_community_trip(trip_id, user["id"] if user else None)
    if not trip:
        raise HTTPException(404, f"Community trip {trip_id} not found.")

    revs = [
        CommunityReviewResponse(
            id=str(r["id"]),
            trip_id=str(r["trip_id"]),
            user_id=str(r["user_id"]) if r.get("user_id") else None,
            user_name=r["user_name"],
            user_avatar=r.get("user_avatar") or "",
            rating=int(r["rating"]),
            liked_aspects=r["liked_aspects"],
            suggested_additions=r["suggested_additions"],
            comment=r.get("comment") or "",
            created_at=str(r.get("created_at") or ""),
        )
        for r in trip.get("reviews", [])
    ]

    return CommunityTripDetailResponse(
        id=str(trip["id"]),
        user_id=str(trip["user_id"]) if trip.get("user_id") else None,
        author_name=trip["author_name"],
        author_avatar=trip.get("author_avatar") or "",
        destination=trip["destination"],
        title=trip["title"],
        description=trip.get("description") or "",
        origin=trip.get("origin"),
        duration_days=trip.get("duration_days") or 1,
        budget=trip.get("budget"),
        currency=trip.get("currency") or "USD",
        travelers=trip.get("travelers") or 1,
        tags=trip.get("tags") or [],
        likes_count=trip.get("likes_count") or 0,
        reviews_count=trip.get("reviews_count") or 0,
        average_rating=float(trip.get("average_rating") or 5.0),
        created_at=str(trip.get("created_at") or ""),
        user_has_liked=bool(trip.get("user_has_liked", False)),
        itinerary_data=trip.get("itinerary_data") or {},
        reviews=revs,
    )


@app.post("/community/trips/{trip_id}/reviews", response_model=CommunityReviewResponse, status_code=201, tags=["community"])
async def submit_community_trip_review(
    trip_id: str,
    req: CommunityReviewCreateRequest,
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> CommunityReviewResponse:
    """Submit a structured review with ratings, what was liked, and pro tips to add."""
    from app.services.community import add_trip_review

    rev = await add_trip_review(trip_id, req, user)
    return CommunityReviewResponse(
        id=str(rev["id"]),
        trip_id=str(rev["trip_id"]),
        user_id=str(rev["user_id"]) if rev.get("user_id") else None,
        user_name=rev["user_name"],
        user_avatar=rev.get("user_avatar") or "",
        rating=int(rev["rating"]),
        liked_aspects=rev["liked_aspects"],
        suggested_additions=rev["suggested_additions"],
        comment=rev.get("comment") or "",
        created_at=str(rev.get("created_at") or ""),
    )


@app.post("/community/trips/{trip_id}/like", tags=["community"])
async def toggle_community_like(
    trip_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Toggle upvote/like for an itinerary (requires login)."""
    from app.services.community import toggle_trip_like

    return await toggle_trip_like(trip_id, user["id"])


@app.post("/community/trips/{trip_id}/remix-ai", response_model=CommunityRemixResponse, tags=["community"])
async def remix_community_trip(
    trip_id: str,
    req: CommunityRemixRequest,
) -> CommunityRemixResponse:
    """AI Remix: Synthesizes traveler suggestions into an enhanced plan."""
    from app.services.community import remix_trip_with_community_suggestions

    result = await remix_trip_with_community_suggestions(trip_id, req.custom_instruction)
    return CommunityRemixResponse(**result)


# ─── Static files & Dedicated Pages ───────────────────────────────────
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


@app.get("/community", tags=["meta"])
@app.get("/community/", tags=["meta"])
@app.get("/community.html", tags=["meta"])
async def community_page():
    """Serve the dedicated Community Itinerary Hub page."""
    comm_path = os.path.join(frontend_dir, "community.html")
    if os.path.exists(comm_path):
        return FileResponse(comm_path)
    return FileResponse(os.path.join(frontend_dir, "index.html"))


if not os.environ.get("VERCEL"):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

