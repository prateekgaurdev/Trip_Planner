"""Background graph runner — returns HTTP quickly, progress via polling or SSE."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from app.pipeline_log import graph_end, graph_start, set_plan_id

logger = logging.getLogger(__name__)

_running: dict[str, asyncio.Task[Any]] = {}

# Human-readable progress messages for each graph node.
_NODE_MESSAGES: dict[str, str] = {
    "orchestrator": "Analyzing your travel preferences…",
    "research_fetch": "Searching web, weather & currency in parallel…",
    "research_agent": "Distilling research highlights with AI…",
    "planner_agent": "Drafting your day-by-day itinerary…",
    "human_gate": "Draft ready — waiting for your review.",
    "finalize_expand": "Expanding activities and mapping your route…",
    "finalize_pack": "Packaging your final trip plan…",
}

# Map node names to the pipeline status the frontend understands.
_NODE_TO_STATUS: dict[str, str] = {
    "orchestrator": "researching",
    "research_fetch": "researching",
    "research_agent": "researching",
    "planner_agent": "planning",
    "human_gate": "awaiting_review",
    "finalize_expand": "finalizing",
    "finalize_pack": "finalizing",
}


def is_plan_running(plan_id: str) -> bool:
    task = _running.get(plan_id)
    return task is not None and not task.done()


async def invoke_graph(plan_id: str, payload: Any) -> None:
    from app.main import get_graph, _thread

    set_plan_id(plan_id)
    is_resume = isinstance(payload, Command)
    action = None
    if is_resume and hasattr(payload, "resume") and isinstance(payload.resume, dict):
        action = payload.resume.get("action")

    t0 = graph_start(resume=is_resume, action=action)
    try:
        graph = await get_graph()
        await graph.ainvoke(payload, config=_thread(plan_id))
    finally:
        from app.main import _current_state

        state = await _current_state(plan_id)
        graph_end(t0, status=(state or {}).get("status"))


async def _invoke(plan_id: str, payload: Any) -> None:
    try:
        await invoke_graph(plan_id, payload)
    except Exception:
        logger.exception("Graph run failed for plan %s", plan_id)
    finally:
        _running.pop(plan_id, None)


def start_graph_run(plan_id: str, payload: Any) -> None:
    """Schedule a graph invocation without blocking the HTTP response."""
    prev = _running.pop(plan_id, None)
    if prev and not prev.done():
        prev.cancel()
    _running[plan_id] = asyncio.create_task(_invoke(plan_id, payload))


async def stream_graph_events(
    plan_id: str, payload: Any
) -> AsyncGenerator[dict[str, Any], None]:
    """Async generator yielding SSE-ready dicts as the graph executes.

    Uses ``graph.astream(payload, stream_mode="updates")`` which yields
    ``{node_name: state_update_dict}`` after each node completes.
    When the graph hits a ``GraphInterrupt`` (HITL pause), we read the
    checkpoint and yield the full draft. On completion we yield the final plan.
    """
    from app.main import get_graph, _thread, _current_state

    set_plan_id(plan_id)
    is_resume = isinstance(payload, Command)
    action = None
    if is_resume and hasattr(payload, "resume") and isinstance(payload.resume, dict):
        action = payload.resume.get("action")

    t0 = graph_start(resume=is_resume, action=action)
    graph = await get_graph()
    config = _thread(plan_id)

    try:
        async for chunk in graph.astream(payload, config=config, stream_mode="updates"):
            # chunk is {node_name: state_update_dict}
            for node_name, update in chunk.items():
                if node_name.startswith("__") or node_name == "human_gate":
                    continue  # skip internal keys; skip human_gate to avoid reverting status
                msg = _NODE_MESSAGES.get(node_name, f"Running {node_name}…")
                pipeline_status = _NODE_TO_STATUS.get(node_name, "researching")

                # If the update itself carries a progress_message, prefer it.
                if isinstance(update, dict) and update.get("progress_message"):
                    msg = update["progress_message"]

                yield {
                    "event": "node_update",
                    "node": node_name,
                    "status": pipeline_status,
                    "message": msg,
                }

        # Graph finished normally (reached END) — emit completed event.
        state = await _current_state(plan_id)
        final_status = (state or {}).get("status", "completed")
        graph_end(t0, status=final_status)

        if final_status == "completed":
            yield {
                "event": "completed",
                "node": "end",
                "status": "completed",
                "final_plan": (state or {}).get("final_plan", {}),
            }
        elif final_status == "awaiting_review":
            # Graph paused at HITL gate — emit paused event with draft.
            yield {
                "event": "paused",
                "node": "human_gate",
                "status": "awaiting_review",
                "progress_message": (state or {}).get("progress_message", ""),
                "plan_id": plan_id,
                "preferences": (state or {}).get("preferences"),
                "research": (state or {}).get("research"),
                "draft_itinerary": (state or {}).get("draft_itinerary"),
                "travel_options": (state or {}).get("travel_options"),
                "lodging_anchor": (state or {}).get("lodging_anchor"),
                "journey_corridor": (state or {}).get("journey_corridor"),
                "route_map": (state or {}).get("route_map") or ((state or {}).get("draft_itinerary") or {}).get("route_map"),
                "rag_sources": (state or {}).get("rag_sources", []),
                "revision_count": (state or {}).get("revision_count", 0),
                "revision_notes": (state or {}).get("revision_notes", []),
            }
        else:
            yield {
                "event": "state_update",
                "node": "end",
                "status": final_status,
                "message": (state or {}).get("progress_message", ""),
            }

    except GraphInterrupt:
        # HITL interrupt — graph paused at human_gate.
        state = await _current_state(plan_id)
        graph_end(t0, status="awaiting_review")
        yield {
            "event": "paused",
            "node": "human_gate",
            "status": "awaiting_review",
            "progress_message": (state or {}).get("progress_message", ""),
            "plan_id": plan_id,
            "preferences": (state or {}).get("preferences"),
            "research": (state or {}).get("research"),
            "draft_itinerary": (state or {}).get("draft_itinerary"),
            "travel_options": (state or {}).get("travel_options"),
            "lodging_anchor": (state or {}).get("lodging_anchor"),
            "journey_corridor": (state or {}).get("journey_corridor"),
            "route_map": (state or {}).get("route_map") or ((state or {}).get("draft_itinerary") or {}).get("route_map"),
            "rag_sources": (state or {}).get("rag_sources", []),
            "revision_count": (state or {}).get("revision_count", 0),
            "revision_notes": (state or {}).get("revision_notes", []),
        }
    except Exception as exc:
        logger.exception("SSE stream error for plan %s", plan_id)
        graph_end(t0, status="error")
        yield {
            "event": "error",
            "node": "unknown",
            "status": "error",
            "message": str(exc)[:300],
        }

