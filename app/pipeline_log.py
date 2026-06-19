"""Structured terminal logging for the travel-planning pipeline.

ASCII-only output for Windows terminals. INFO shows HTTP, graph phases, nodes,
and slow/failed API calls. Set LOG_LEVEL=DEBUG for every external API call.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator

_plan_id: ContextVar[str] = ContextVar("plan_id", default="-")
_node_name: ContextVar[str] = ContextVar("node_name", default="-")

LOG = logging.getLogger("wayfarer")

_SLOW_API_MS = 2000
_SKIP_NODE_MS = 80  # orchestrator etc. — omit from INFO when instant


class _PlanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.plan_id = _short_plan(_plan_id.get())
        record.node = _node_name.get()
        return super().format(record)


def _short_plan(plan_id: str) -> str:
    if not plan_id or plan_id in ("-", "\u2014"):
        return "-"
    return plan_id[:8]


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _kv(**extra: Any) -> str:
    if not extra:
        return ""
    return " | " + " | ".join(f"{k}={v}" for k, v in extra.items())


def setup_pipeline_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _PlanFormatter(
            fmt="%(asctime)s %(plan_id)s %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    LOG.setLevel(level)
    LOG.handlers.clear()
    LOG.addHandler(handler)
    LOG.propagate = False

    for noisy in ("httpx", "httpcore", "google_genai", "google_genai.models", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_plan_id(plan_id: str | None) -> None:
    _plan_id.set(plan_id or "-")


def set_node(name: str | None) -> None:
    _node_name.set(name or "-")


def flow(msg: str, **extra: Any) -> None:
    LOG.info("%s%s", msg, _kv(**extra))


def http(method: str, path: str, *, status: int | None = None, ms: float | None = None, **extra: Any) -> None:
    parts = [f"{method} {path}"]
    if status is not None:
        parts.append(f"-> {status}")
    if ms is not None:
        parts.append(f"({ms / 1000:.1f}s)" if ms >= 1000 else f"({ms:.0f}ms)")
    LOG.info("%s%s", " ".join(parts), _kv(**extra))


def api_start(service: str, operation: str, **detail: Any) -> float:
    LOG.debug("API >> %s.%s%s", service, operation, _kv(**detail))
    return time.perf_counter()


def api_end(
    service: str,
    operation: str,
    start: float,
    *,
    ok: bool = True,
    **detail: Any,
) -> None:
    ms = _ms(start)
    tag = "FAIL" if not ok else "OK"
    line = f"  API {service}.{operation} {tag} {ms:.0f}ms{_kv(**detail)}"
    if not ok or ms >= _SLOW_API_MS:
        LOG.info(line)
    else:
        LOG.debug(line)


def node_start(name: str) -> float:
    set_node(name)
    return time.perf_counter()


def node_end(name: str, start: float, **detail: Any) -> None:
    ms = _ms(start)
    suffix = _kv(**detail)
    if ms < _SKIP_NODE_MS and LOG.isEnabledFor(logging.DEBUG) is False:
        if not detail.get("paused") and not detail.get("error"):
            return
    LOG.info("  node %-16s %5.0fms%s", name, ms, suffix)


def graph_start(*, resume: bool = False, action: str | None = None) -> float:
    kind = f"resume ({action})" if resume and action else ("resume" if resume else "create")
    LOG.info("GRAPH start | %s", kind)
    return time.perf_counter()


def graph_end(start: float, *, status: str | None = None) -> None:
    ms = _ms(start)
    dur = f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
    LOG.info("GRAPH done | %s | status=%s", dur, status or "?")


def hitl_pause() -> None:
    LOG.info("HITL paused | waiting for approve / modify / reject")


def hitl_resume(action: str, *, feedback: str = "", has_travel: bool = False) -> None:
    extra = []
    if feedback:
        extra.append(f"feedback={feedback[:60]!r}")
    if has_travel:
        extra.append("travel=yes")
    tail = (" | " + " | ".join(extra)) if extra else ""
    LOG.info("HITL resume | action=%s%s", action, tail)


def startup_banner(settings: Any) -> None:
    from app.config import stubs_enabled

    mode = "STUBS" if stubs_enabled() else "LIVE"
    keys = (
        f"google={'Y' if settings.google_api_key else 'N'}"
        f" tavily={'Y' if settings.tavily_api_key else 'N'}"
        f" serp={'Y' if settings.serpapi_key else 'N'}"
    )
    LOG.warning(
        "Wayfarer %s | model=%s | bg=%s | %s",
        mode,
        settings.gemini_model,
        settings.graph_background if not os.environ.get("VERCEL") else False,
        keys,
    )
    LOG.warning(
        "Pipeline: POST /plan -> research -> planner -> review | "
        "POST /review approve -> finalize -> GET /final"
    )


@asynccontextmanager
async def plan_scope(plan_id: str) -> AsyncIterator[None]:
    token = _plan_id.set(plan_id)
    try:
        yield
    finally:
        _plan_id.reset(token)


def infer_llm_label(system: str) -> str:
    s = system.lower()
    if "research" in s and "analyst" in s:
        return "research_summarize"
    if "review-ready draft" in s:
        return "planner_draft"
    if "skeletal schedule" in s or "day-by-day skeletal" in s:
        return "planner_schedule"
    if "finalizing" in s or "finaliz" in s:
        return "finalize_expand"
    if "concise json summary" in s:
        return "planner_narrative"
    if "aviation" in s:
        return "airports"
    if "wikipedia" in s or "images" in s:
        return "activity_images"
    return "llm_json"
