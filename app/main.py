"""FastAPI application — the 4 required endpoints, fully async.

Lifecycle of a plan:
  POST /plan            → runs orchestrator→research→planner→interrupt(),
                          control returns here paused at the gate.
  GET  /plan/{id}       → reads the live checkpoint (poll until awaiting_review)
  POST /plan/{id}/review→ resumes with Command(resume={action,feedback})
  GET  /plan/{id}/final → returns the final plan once status == completed
"""
from __future__ import annotations

import uuid
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from app.graph import build_graph
from app.schemas import (
    FinalPlanResponse,
    PlanCreatedResponse,
    PlanRequest,
    PlanStateResponse,
    ReviewRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the checkpointer-backed graph once for the app's lifetime."""
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


def _thread(plan_id: str) -> dict[str, Any]:
    """LangGraph config — thread_id == plan_id is the whole persistence trick."""
    return {"configurable": {"thread_id": plan_id}}


async def _current_state(graph, plan_id: str) -> dict[str, Any] | None:
    snapshot = await graph.aget_state(_thread(plan_id))
    if not snapshot or not snapshot.values:
        return None
    return snapshot.values


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plan", response_model=PlanCreatedResponse, status_code=201, tags=["plan"])
async def create_plan(req: PlanRequest) -> PlanCreatedResponse:
    """Kick off a new plan. Runs until the graph hits the HITL interrupt."""
    plan_id = str(uuid.uuid4())
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
        },
    }
    # Runs to the interrupt() and returns; the checkpoint is now persisted.
    await app.state.graph.ainvoke(initial, config=_thread(plan_id))
    state = await _current_state(app.state.graph, plan_id)
    return PlanCreatedResponse(
        plan_id=plan_id, status=(state or {}).get("status", "researching")
    )


@app.get("/plan/{plan_id}", response_model=PlanStateResponse, tags=["plan"])
async def get_plan(plan_id: str) -> PlanStateResponse:
    """Return the current checkpointed state (poll until awaiting_review)."""
    state = await _current_state(app.state.graph, plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")
    return PlanStateResponse(
        plan_id=plan_id,
        status=state.get("status", "unknown"),
        preferences=state.get("preferences"),
        research=state.get("research"),
        draft_itinerary=state.get("draft_itinerary"),
        revision_count=state.get("revision_count", 0),
        revision_notes=state.get("revision_notes", []),
    )


@app.post("/plan/{plan_id}/review", response_model=PlanStateResponse, tags=["plan"])
async def review_plan(plan_id: str, req: ReviewRequest) -> PlanStateResponse:
    """Resume the paused graph with the human's decision.

    Command(resume=...) feeds the payload straight back into the interrupt()
    call inside human_gate, and the graph continues: approve→finalize,
    reject→research, modify→planner.
    """
    state = await _current_state(app.state.graph, plan_id)
    if state is None:
        raise HTTPException(404, f"No plan with id {plan_id}")
    if state.get("status") != "awaiting_review":
        raise HTTPException(
            409,
            f"Plan is '{state.get('status')}', not awaiting_review; cannot review now.",
        )

    await app.state.graph.ainvoke(
        Command(resume={"action": req.action, "feedback": req.feedback}),
        config=_thread(plan_id),
    )

    new_state = await _current_state(app.state.graph, plan_id)
    return PlanStateResponse(
        plan_id=plan_id,
        status=(new_state or {}).get("status", "unknown"),
        preferences=(new_state or {}).get("preferences"),
        research=(new_state or {}).get("research"),
        draft_itinerary=(new_state or {}).get("draft_itinerary"),
        revision_count=(new_state or {}).get("revision_count", 0),
        revision_notes=(new_state or {}).get("revision_notes", []),
    )


@app.get("/plan/{plan_id}/final", response_model=FinalPlanResponse, tags=["plan"])
async def get_final(plan_id: str) -> FinalPlanResponse:
    """Return the finalised plan, or 409 if it isn't approved yet."""
    state = await _current_state(app.state.graph, plan_id)
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
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

