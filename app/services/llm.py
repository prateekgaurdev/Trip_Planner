"""LLM client wrapper around Google Gemini.

Exposes one tiny async method, `complete_json`, that asks the model for a
strict-JSON answer and parses it. Nodes never import langchain directly —
they depend on this interface, so the provider is swappable in one place.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel
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

    async def complete_json(
        self, system: str, user: str, schema: type[BaseModel] | None = None
    ) -> dict[str, Any]:
        from app.config import get_settings
        from app.pipeline_log import api_end, api_start, infer_llm_label

        label = infer_llm_label(system)
        settings = get_settings()
        t0 = api_start("Gemini", label, model=settings.gemini_model, prompt_chars=len(system) + len(user))

        try:
            if schema is not None:
                # with_structured_output guarantees typed schema enforcement
                structured_llm = self._llm.with_structured_output(schema)
                messages = [
                    ("system", system),
                    ("human", user),
                ]
                resp = await structured_llm.ainvoke(messages)
                if isinstance(resp, BaseModel):
                    parsed = resp.model_dump()
                elif isinstance(resp, dict):
                    # Strict validation through Pydantic v2 schema
                    parsed = schema.model_validate(resp).model_dump()
                else:
                    raise LLMError(f"Unexpected response type from structured LLM: {type(resp)}")
                api_end("Gemini", label, t0, keys=list(parsed.keys()))
                return parsed
            else:
                prompt = (
                    f"{system}\n\n{user}\n\n"
                    "Respond with ONLY a valid JSON object. No markdown, no prose."
                )
                resp = await self._llm.ainvoke(prompt)
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
        except Exception as exc:  # pragma: no cover - network dependent
            api_end("Gemini", label, t0, ok=False, error=str(exc)[:120])
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Gemini call failed: {exc}") from exc

    async def complete(self, system: str, user: str) -> str:
        messages = [
            ("system", system),
            ("human", user),
        ]
        resp = await self._llm.ainvoke(messages)
        raw = resp.content if hasattr(resp, "content") else str(resp)
        return _content_to_text(raw).strip()


class StubLLM:
    """Deterministic, network-free LLM used when USE_STUBS=true.

    Returns rich, schema-valid data so the whole graph can run end to
    end in tests without API keys. Validates against schema if provided.
    """

    async def complete_json(
        self, system: str, user: str, schema: type[BaseModel] | None = None
    ) -> dict[str, Any]:
        sys_lower = system.lower()
        if "research" in sys_lower:
            data = {
                "highlights": [
                    "Historic old town walking area",
                    "Renowned local food market",
                    "Scenic riverside park",
                ],
                "tips": [
                    "Buy a transit day-pass for seamless metro and tram rides",
                    "Book popular landmarks in advance to skip queue lines"
                ],
                "local_currency_hint": "Local currency is EUR. $1 USD ≈ 0.93 EUR",
                "neighborhoods": [
                    {
                        "name": "Historic Old Town",
                        "vibe": "Walkable historic center",
                        "key_attractions": ["Historic old town walking area", "Renowned local food market"],
                    },
                    {
                        "name": "Riverside District",
                        "vibe": "Scenic promenade & arts",
                        "key_attractions": ["Scenic riverside park", "City museum"],
                    },
                ],
                "recommended_lodging_area": "Historic Old Town",
                "grounded_facts": [
                    "Centrally located near Rossio station with direct airport metro connection.",
                    "Dining custom: dinner begins after 20:00; couvert is optional."
                ],
            }
            if schema:
                return schema.model_validate(data).model_dump()
            return data

        if "finalizing" in sys_lower or "finaliz" in sys_lower:
            data = {
                "lodging_anchor": {
                    "name": "Heritage Old Town Boutique Stay",
                    "neighborhood": "Historic Old Town",
                    "address": "Praca Central 10, Downtown",
                    "check_in_time": "15:00",
                    "check_out_time": "11:00",
                    "estimated_nightly_rate": 140.0,
                    "currency": "USD",
                    "why_recommended": "Centrally positioned in the historic core, allowing daily walkouts and direct evening returns.",
                },
                "journey_corridor": {
                    "departure_city": "London, UK",
                    "arrival_city": "Lisbon, Portugal",
                    "recommended_transit_mode": "Direct Flight (~2h 45m)",
                    "travel_time_estimate": "Approx. 4.5 hours door-to-door",
                    "arrival_transfer_guidance": "Take the Red Line Metro directly from Lisbon Airport to São Sebastião, connecting to Baixa-Chiado (25 mins).",
                    "climate_and_cultural_contrast": "Transition from cooler UK maritime weather to mild Iberian sunshine; dinner begins after 20:00.",
                    "return_departure_tip": "Allow 2.5 hours prior to departure for terminal check-in at Humberto Delgado Airport.",
                    "grounded_route_facts": [
                        "Direct flights run daily between London hubs (LHR/LGW) and Lisbon (LIS).",
                        "Metro connects Lisbon airport directly to city center for under €2."
                    ],
                },
                "days": [
                    {
                        "day_number": 1,
                        "date": "2026-09-10",
                        "title": "Historic Old Town & Market Exploration",
                        "neighborhood_cluster": "Historic Old Town",
                        "description": "A cultural introduction anchored to your central lodging base.",
                        "activities": [
                            {
                                "time": "09:30",
                                "title": "Historic old town walking area",
                                "description": "Explore cobbled lanes, historic architecture, and artisan shops starting from your lodging base.",
                                "location_name": "Historic old town",
                                "neighborhood": "Historic Old Town",
                                "activity_type": "sightseeing",
                            },
                            {
                                "time": "13:00",
                                "title": "Renowned local food market",
                                "description": "Sample regional specialties, fresh pastries, and authentic street food.",
                                "location_name": "Local food market",
                                "neighborhood": "Historic Old Town",
                                "activity_type": "dining",
                            },
                        ],
                    },
                    {
                        "day_number": 2,
                        "date": "2026-09-11",
                        "title": "Riverside Promenade & Arts District",
                        "neighborhood_cluster": "Riverside District",
                        "description": "A scenic day along the riverfront mixing open-air walks and museum culture.",
                        "activities": [
                            {
                                "time": "10:00",
                                "title": "Scenic riverside park",
                                "description": "Stroll along the waterfront promenade and enjoy panoramic city views.",
                                "location_name": "Riverside park",
                                "neighborhood": "Riverside District",
                                "activity_type": "relaxation",
                            },
                            {
                                "time": "15:00",
                                "title": "City museum",
                                "description": "Explore rich local art collections and historical exhibitions.",
                                "location_name": "City museum",
                                "neighborhood": "Riverside District",
                                "activity_type": "culture",
                            },
                        ],
                    },
                ]
            }
            if schema:
                return schema.model_validate(data).model_dump()
            return data

        if "review-ready draft" in sys_lower:
            data = {
                "summary": "A balanced, geographically clustered itinerary blending heritage walking routes, authentic regional cuisine, and scenic waterfront culture.",
                "lodging_anchor": {
                    "name": "Heritage Old Town Boutique Stay",
                    "neighborhood": "Historic Old Town",
                    "address": "Praca Central 10, Downtown",
                    "check_in_time": "15:00",
                    "check_out_time": "11:00",
                    "estimated_nightly_rate": 140.0,
                    "currency": "USD",
                    "why_recommended": "Centrally positioned in the historic core, allowing daily walkouts and direct evening returns without cross-city transit friction.",
                },
                "journey_corridor": {
                    "departure_city": "London, UK",
                    "arrival_city": "Lisbon, Portugal",
                    "recommended_transit_mode": "Direct Flight (~2h 45m)",
                    "travel_time_estimate": "Approx. 4.5 hours door-to-door",
                    "arrival_transfer_guidance": "Take the Red Line Metro directly from Lisbon Airport to São Sebastião, connecting to Baixa-Chiado (25 mins).",
                    "climate_and_cultural_contrast": "Transition from cooler UK maritime weather to mild Iberian sunshine; dinner begins after 20:00.",
                    "return_departure_tip": "Allow 2.5 hours prior to departure for terminal check-in at Humberto Delgado Airport.",
                    "grounded_route_facts": [
                        "Direct flights run daily between London hubs (LHR/LGW) and Lisbon (LIS).",
                        "Metro connects Lisbon airport directly to city center for under €2."
                    ],
                },
                "days": [
                    {
                        "day_number": 1,
                        "date": "2026-09-10",
                        "focus": "flexible",
                        "neighborhood_cluster": "Historic Old Town",
                        "activities": [
                            {
                                "time": "09:30 AM",
                                "title": "Historic Old Town Walking Area",
                                "description": "Explore cobbled lanes, historic architecture, and artisan shops starting from your lodging base.",
                                "location_name": "Historic Old Town",
                                "opening_hours": "08:00 AM – 08:00 PM",
                                "estimated_cost": "Free",
                                "estimated_duration": "2.5 hrs"
                            },
                            {
                                "time": "01:00 PM",
                                "title": "Renowned Local Food Market",
                                "description": "Sample regional specialties, fresh pastries, and authentic street food.",
                                "location_name": "Local Food Market",
                                "opening_hours": "09:00 AM – 06:00 PM",
                                "estimated_cost": "$25 USD",
                                "estimated_duration": "1.5 hrs"
                            }
                        ],
                        "weather_note": "Mild and dry — ideal for walking",
                    },
                    {
                        "day_number": 2,
                        "date": "2026-09-11",
                        "focus": "indoor",
                        "neighborhood_cluster": "Riverside District",
                        "activities": [
                            {
                                "time": "10:00 AM",
                                "title": "Scenic Riverside Park",
                                "description": "Stroll along the waterfront promenade and enjoy panoramic city views.",
                                "location_name": "Riverside Park",
                                "opening_hours": "Open 24/7",
                                "estimated_cost": "Free",
                                "estimated_duration": "2 hrs"
                            },
                            {
                                "time": "03:00 PM",
                                "title": "City Museum of Art & History",
                                "description": "Explore rich local art collections and historical exhibitions.",
                                "location_name": "City Museum",
                                "opening_hours": "10:00 AM – 06:00 PM",
                                "estimated_cost": "$12 entry",
                                "estimated_duration": "2.5 hrs"
                            }
                        ],
                        "weather_note": "Favorable afternoon conditions",
                    },
                ],
                "notes": [
                    "All daily activities are clustered within single walking zones to eliminate transit friction.",
                    "Lodging anchor serves as the daily morning departure and evening return hub.",
                    "Budget allocation strictly observed across lodging, food, and culture.",
                ],
                "transit_strategy": "Days 1 and 2 operate within distinct 2 km walkable clusters with lodging anchor as the transit hub.",
            }
            if schema:
                return schema.model_validate(data).model_dump()
            return data

        if "skeletal schedule" in sys_lower or "day-by-day skeletal" in sys_lower:
            return {
                "days": [
                    {
                        "day_number": 1,
                        "date": "2026-09-10",
                        "focus": "flexible",
                        "neighborhood_cluster": "Historic Old Town",
                        "activities": ["Historic old town walking area", "Renowned local food market"],
                        "weather_note": "",
                    },
                    {
                        "day_number": 2,
                        "date": "2026-09-11",
                        "focus": "indoor",
                        "neighborhood_cluster": "Riverside District",
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

    async def complete(self, system: str, user: str) -> str:
        return (
            "Upgraded with community recommendations: Incorporating early morning sunrise visits "
            "and local dining reservations to ensure seamless pacing and authentic local immersion."
        )


def get_llm() -> GeminiClient | StubLLM:
    """Return Gemini in production; StubLLM only under pytest with USE_STUBS=true."""
    if stubs_enabled():
        return StubLLM()
    return GeminiClient()
