"""FastAPI application — the 4 required endpoints, fully async.

Lifecycle of a plan:
  POST /plan            → runs orchestrator→research→planner→interrupt(),
                          control returns here paused at the gate.
  GET  /plan/{id}       → reads the live checkpoint (poll until awaiting_review)
  POST /plan/{id}/review→ resumes with Command(resume={action,feedback})
  GET  /plan/{id}/final → returns the final plan once status == completed
"""
from __future__ import annotations

import logging
import uuid
import os
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from app.config import get_settings, graph_runs_in_background, stubs_enabled
from app.graph import build_graph
from app.graph_runner import invoke_graph, is_plan_running, start_graph_run
from app.pipeline_log import (
    LOG as pipeline_log,
    flow,
    hitl_resume,
    http as log_http,
    setup_pipeline_logging,
    startup_banner,
)
from app.schemas import (
    ActivityImageLookupRequest,
    ActivityImageLookupResponse,
    FinalPlanResponse,
    PlanCreatedResponse,
    PlanRequest,
    PlanStateResponse,
    ReviewRequest,
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
    """LangGraph config — thread_id == plan_id is the whole persistence trick."""
    return {"configurable": {"thread_id": plan_id}}


async def _current_state(plan_id: str) -> dict[str, Any] | None:
    graph = await get_graph()
    snapshot = await graph.aget_state(_thread(plan_id))
    if not snapshot or not snapshot.values:
        return None
    return snapshot.values


def _plan_response(plan_id: str, state: dict[str, Any] | None) -> PlanStateResponse:
    state = state or {}
    return PlanStateResponse(
        plan_id=plan_id,
        status=state.get("status", "unknown"),
        progress_message=state.get("progress_message") or "",
        preferences=state.get("preferences"),
        research=state.get("research"),
        draft_itinerary=state.get("draft_itinerary"),
        travel_options=state.get("travel_options"),
        revision_count=state.get("revision_count", 0),
        revision_notes=state.get("revision_notes", []),
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
        if not getattr(app.state, "checkpointer", None):
            cm = AsyncSqliteSaver.from_conn_string(settings.checkpoint_db)
            app.state.checkpointer = await cm.__aenter__()
            app.state._checkpointer_cm = cm

        app.state.graph = _build_uncompiled().compile(
            checkpointer=app.state.checkpointer
        )
        return app.state.graph


@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    """Quick sanity check — confirms whether the server is using real APIs or stubs."""
    settings = get_settings()
    return {
        "status": "ok",
        "mode": "live" if not stubs_enabled() else "stubs",
        "gemini_model": settings.gemini_model,
        "google_api_key_set": bool(settings.google_api_key),
        "tavily_api_key_set": bool(settings.tavily_api_key),
        "openrouteservice_api_key_set": bool(settings.openrouteservice_api_key),
        "serpapi_key_set": bool(settings.serpapi_key),
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
        },
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
            research=state.get("research"),
            draft_itinerary=state.get("draft_itinerary"),
            revision_count=state.get("revision_count", 0),
            revision_notes=state.get("revision_notes", []),
        )

    await invoke_graph(plan_id, Command(resume=resume))
    new_state = await _current_state(plan_id)
    flow("REVIEW finished", status=(new_state or {}).get("status"))
    return _plan_response(plan_id, new_state)


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

frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if not os.environ.get("VERCEL"):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

