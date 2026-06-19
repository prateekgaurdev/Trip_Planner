"""Background graph runner — returns HTTP quickly, progress via polling."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.types import Command

from app.pipeline_log import graph_end, graph_start, set_plan_id

logger = logging.getLogger(__name__)

_running: dict[str, asyncio.Task[Any]] = {}


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
