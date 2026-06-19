"""LLM client wrapper around Google Gemini.

Exposes one tiny async method, `complete_json`, that asks the model for a
strict-JSON answer and parses it. Nodes never import langchain directly —
they depend on this interface, so the provider is swappable in one place.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import get_settings, stubs_enabled


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns unparseable output."""


def _content_to_text(content: Any) -> str:
    """Normalize LangChain/Gemini message content to a plain string.

    Newer Gemini responses may return content as a list of blocks instead of
    a single string — e.g. [{"type": "text", "text": "..."}].
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"])
        return json.dumps(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from a model response.

    Gemini occasionally wraps JSON in ```json fences or adds prose; we strip
    the fence and slice from the first '{' to the last '}'.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"No JSON object found in LLM output: {text[:200]!r}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise LLMError(f"Invalid JSON from LLM: {exc}") from exc


class GeminiClient:
    """Thin async wrapper over langchain-google-genai."""

    def __init__(self) -> None:
        settings = get_settings()
        # Imported lazily so `USE_STUBS=true` runs need no langchain/genai.
        from langchain_google_genai import ChatGoogleGenerativeAI

        self._llm = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.google_api_key,
            temperature=0.4,
        )

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        from app.config import get_settings
        from app.pipeline_log import api_end, api_start, infer_llm_label

        label = infer_llm_label(system)
        settings = get_settings()
        t0 = api_start("Gemini", label, model=settings.gemini_model, prompt_chars=len(system) + len(user))
        prompt = (
            f"{system}\n\n{user}\n\n"
            "Respond with ONLY a valid JSON object. No markdown, no prose."
        )
        try:
            resp = await self._llm.ainvoke(prompt)
        except Exception as exc:  # pragma: no cover - network dependent
            api_end("Gemini", label, t0, ok=False, error=str(exc)[:120])
            raise LLMError(f"Gemini call failed: {exc}") from exc

        raw = resp.content if hasattr(resp, "content") else resp
        if isinstance(raw, dict):
            api_end("Gemini", label, t0, keys=list(raw.keys()))
            return raw
        text = _content_to_text(raw)
        if not text.strip():
            api_end("Gemini", label, t0, ok=False, error="empty response")
            raise LLMError("Gemini returned an empty response.")
        parsed = _extract_json(text)
        api_end("Gemini", label, t0, keys=list(parsed.keys()))
        return parsed


class StubLLM:
    """Deterministic, network-free LLM used when USE_STUBS=true.

    Returns minimal but well-shaped JSON so the whole graph can run end to
    end in tests without API keys.
    """

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        sys_lower = system.lower()
        if "research" in sys_lower:
            return {
                "highlights": [
                    "Historic old town walking area",
                    "Renowned local food market",
                    "Scenic riverside park",
                ],
                "tips": ["Buy a transit day-pass", "Book popular sites in advance"],
            }
        if "finalizing" in sys_lower or "finaliz" in sys_lower:
            return {
                "days": [
                    {
                        "day_number": 1,
                        "date": "2026-09-10",
                        "title": "Old town & markets",
                        "description": "A cultural introduction to the city.",
                        "activities": [
                            {
                                "time": "09:30",
                                "title": "Historic old town walking area",
                                "description": "Explore cobbled lanes and local architecture.",
                                "image_keyword": "old town",
                                "location_name": "Historic old town",
                            },
                            {
                                "time": "13:00",
                                "title": "Renowned local food market",
                                "description": "Sample regional specialties and street food.",
                                "image_keyword": "food market",
                                "location_name": "Local food market",
                            },
                        ],
                    },
                    {
                        "day_number": 2,
                        "date": "2026-09-11",
                        "title": "Riverside & museums",
                        "description": "A relaxed day mixing outdoors and culture.",
                        "activities": [
                            {
                                "time": "10:00",
                                "title": "Scenic riverside park",
                                "description": "Stroll along the waterfront and people-watch.",
                                "image_keyword": "riverside",
                                "location_name": "Riverside park",
                            },
                            {
                                "time": "15:00",
                                "title": "City museum",
                                "description": "Dive into local history and art.",
                                "image_keyword": "museum",
                                "location_name": "City museum",
                            },
                        ],
                    },
                ]
            }
        if "skeletal schedule" in sys_lower or "day-by-day skeletal" in sys_lower:
            return {
                "days": [
                    {
                        "day_number": 1,
                        "date": "2026-09-10",
                        "focus": "flexible",
                        "activities": ["Historic old town walking area", "Local food market"],
                        "weather_note": "",
                    },
                    {
                        "day_number": 2,
                        "date": "2026-09-11",
                        "focus": "indoor",
                        "activities": ["Scenic riverside park", "City museum"],
                        "weather_note": "",
                    },
                ]
            }
        # Planner-style request.
        return {
            "summary": "A balanced trip blending culture, food, and rest.",
            "notes": ["Stubbed plan generated without an LLM."],
        }


def get_llm() -> GeminiClient | StubLLM:
    """Return Gemini in production; StubLLM only under pytest with USE_STUBS=true."""
    if stubs_enabled():
        return StubLLM()
    return GeminiClient()
