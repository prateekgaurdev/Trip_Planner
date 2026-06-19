"""Assemble the StateGraph and wire persistence.

State persistence across the HITL pause is solved by a checkpointer keyed on
thread_id == plan_id. We use the async SQLite saver so every /review call
against a plan_id automatically resumes the exact paused checkpoint — no
hand-rolled in-memory state, which is the trap most submissions fall into.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.nodes import (
    finalize_enrich,
    finalize_expand,
    finalize_pack,
    human_gate,
    orchestrator,
    planner_agent,
    research_agent,
    research_fetch,
    route_after_review,
)
from app.pipeline_log import node_end, node_start, set_plan_id
from app.state import TripState


def _logged(name: str, fn):
    """Wrap a graph node with start/end timing logs."""

    async def wrapper(state: TripState) -> dict:
        plan_id = state.get("plan_id", "—")
        set_plan_id(plan_id)
        t0 = node_start(name)
        try:
            result = await fn(state)
            detail: dict[str, Any] = {}
            if isinstance(result, dict):
                if result.get("status"):
                    detail["status"] = result["status"]
            node_end(name, t0, **detail)
            return result
        except GraphInterrupt:
            node_end(name, t0, paused="review")
            raise
        except Exception:
            node_end(name, t0, error="failed")
            raise

    return wrapper


def _build_uncompiled() -> StateGraph:
    """The graph topology — readable in ~10 lines, which is the point."""
    g = StateGraph(TripState)

    g.add_node("orchestrator", _logged("orchestrator", orchestrator))
    g.add_node("research_fetch", _logged("research_fetch", research_fetch))
    g.add_node("research_agent", _logged("research_agent", research_agent))
    g.add_node("planner_agent", _logged("planner_agent", planner_agent))
    g.add_node("human_gate", _logged("human_gate", human_gate))
    g.add_node("finalize_expand", _logged("finalize_expand", finalize_expand))
    g.add_node("finalize_enrich", _logged("finalize_enrich", finalize_enrich))
    g.add_node("finalize_pack", _logged("finalize_pack", finalize_pack))

    g.add_edge(START, "orchestrator")
    g.add_edge("orchestrator", "research_fetch")
    g.add_edge("research_fetch", "research_agent")
    g.add_edge("research_agent", "planner_agent")
    g.add_edge("planner_agent", "human_gate")

    g.add_conditional_edges(
        "human_gate",
        route_after_review,
        {
            "finalize_expand": "finalize_expand",
            "research_agent": "research_fetch",
            "planner_agent": "planner_agent",
            "human_gate": "human_gate",
        },
    )
    g.add_edge("finalize_expand", "finalize_enrich")
    g.add_edge("finalize_enrich", "finalize_pack")
    g.add_edge("finalize_pack", END)
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
