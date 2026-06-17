"""LLM client wrapper around Google Gemini.

Exposes one tiny async method, `complete_json`, that asks the model for a
strict-JSON answer and parses it. Nodes never import langchain directly —
they depend on this interface, so the provider is swappable in one place.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import get_settings


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns unparseable output."""


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
        prompt = (
            f"{system}\n\n{user}\n\n"
            "Respond with ONLY a valid JSON object. No markdown, no prose."
        )
        try:
            resp = await self._llm.ainvoke(prompt)
        except Exception as exc:  # pragma: no cover - network dependent
            raise LLMError(f"Gemini call failed: {exc}") from exc
        return _extract_json(resp.content if hasattr(resp, "content") else str(resp))


class StubLLM:
    """Deterministic, network-free LLM used when USE_STUBS=true.

    Returns minimal but well-shaped JSON so the whole graph can run end to
    end in tests without API keys.
    """

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        if "research" in system.lower():
            return {
                "highlights": [
                    "Historic old town walking area",
                    "Renowned local food market",
                    "Scenic riverside park",
                ],
                "tips": ["Buy a transit day-pass", "Book popular sites in advance"],
            }
        # Planner-style request.
        return {
            "summary": "A balanced trip blending culture, food, and rest.",
            "notes": ["Stubbed plan generated without an LLM."],
        }


def get_llm() -> GeminiClient | StubLLM:
    """Factory honouring the USE_STUBS flag."""
    if get_settings().use_stubs:
        return StubLLM()
    return GeminiClient()
