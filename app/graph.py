"""Assemble the StateGraph and wire persistence.

State persistence across the HITL pause is solved by a checkpointer keyed on
thread_id == plan_id. We use the async SQLite saver so every /review call
against a plan_id automatically resumes the exact paused checkpoint — no
hand-rolled in-memory state, which is the trap most submissions fall into.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.nodes import (
    finalize,
    human_gate,
    orchestrator,
    planner_agent,
    research_agent,
    route_after_review,
)
from app.state import TripState


def _build_uncompiled() -> StateGraph:
    """The graph topology — readable in ~10 lines, which is the point."""
    g = StateGraph(TripState)

    g.add_node("orchestrator", orchestrator)
    g.add_node("research_agent", research_agent)
    g.add_node("planner_agent", planner_agent)
    g.add_node("human_gate", human_gate)
    g.add_node("finalize", finalize)

    g.add_edge(START, "orchestrator")
    g.add_edge("orchestrator", "research_agent")
    g.add_edge("research_agent", "planner_agent")
    g.add_edge("planner_agent", "human_gate")

    # The HITL fork: approve / reject / modify.
    g.add_conditional_edges(
        "human_gate",
        route_after_review,
        {
            "finalize": "finalize",
            "research_agent": "research_agent",
            "planner_agent": "planner_agent",
            "human_gate": "human_gate",
        },
    )
    g.add_edge("finalize", END)
    return g


@asynccontextmanager
async def build_graph() -> AsyncIterator:
    """Yield a compiled graph backed by an async SQLite checkpointer.

    Used as an async context manager (see main.py lifespan) so the SQLite
    connection is opened once at startup and closed cleanly on shutdown.
    """
    settings = get_settings()
    async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db) as saver:
        yield _build_uncompiled().compile(checkpointer=saver)
