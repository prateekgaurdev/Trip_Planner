"""Resolve nearby commercial airports for a city using the LLM.

Used only for optional flight price search — not for the sightseeing route map.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.llm import LLMError, get_llm

_AIRPORT_SYSTEM = (
    "You are an aviation geography expert. Given a city or region, identify exactly "
    "two real commercial airports that travelers commonly use to fly to or from that area. "
    "Order by practicality (major hub first, then a sensible alternate). "
    "Use accurate IATA codes. Return ONLY JSON matching this schema:\n"
    '{"airports": [{"name": "Full official name", "iata": "XXX", "city": "City", '
    '"note": "One short reason"}]}\n'
    "Return exactly 2 airports when possible; 1 only if no second airport is realistic."
)

_STUB_AIRPORTS: dict[str, list[dict[str, str]]] = {
    "delhi": [
        {"name": "Indira Gandhi International Airport", "iata": "DEL", "city": "Delhi", "note": "Main hub"},
        {"name": "Hindon Airport", "iata": "HDO", "city": "Ghaziabad", "note": "Regional alternate"},
    ],
    "rishikesh": [
        {"name": "Jolly Grant Airport", "iata": "DED", "city": "Dehradun", "note": "Closest to Rishikesh"},
        {"name": "Indira Gandhi International Airport", "iata": "DEL", "city": "Delhi", "note": "Major hub (~5h drive)"},
    ],
    "london": [
        {"name": "Heathrow Airport", "iata": "LHR", "city": "London", "note": "Primary international"},
        {"name": "Gatwick Airport", "iata": "LGW", "city": "London", "note": "Major alternate"},
    ],
    "new york": [
        {"name": "John F. Kennedy International Airport", "iata": "JFK", "city": "New York", "note": "Main international"},
        {"name": "LaGuardia Airport", "iata": "LGA", "city": "New York", "note": "Domestic & regional"},
    ],
}


def _norm_key(city: str) -> str:
    return re.sub(r"\s+", " ", city.strip().lower().split(",")[0])


def _stub_airports(city: str) -> list[dict[str, str]]:
    key = _norm_key(city)
    for stub_key, airports in _STUB_AIRPORTS.items():
        if stub_key in key or key in stub_key:
            return [a.copy() for a in airports]
    slug = key.replace(" ", "")[:3].upper() or "AAA"
    alt = (slug[0] + slug[-1] + "X")[:3] if len(slug) >= 2 else "ZZZ"
    return [
        {
            "name": f"{city.split(',')[0].strip()} International Airport",
            "iata": slug,
            "city": city.split(",")[0].strip(),
            "note": "Primary (stub)",
        },
        {
            "name": f"{city.split(',')[0].strip()} Regional Airport",
            "iata": alt,
            "city": city.split(",")[0].strip(),
            "note": "Alternate (stub)",
        },
    ]


def _sanitize_airports(raw: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        iata = str(item.get("iata") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not iata or len(iata) != 3 or iata in seen:
            continue
        seen.add(iata)
        out.append(
            {
                "name": name or f"{iata} Airport",
                "iata": iata,
                "city": str(item.get("city") or "").strip(),
                "note": str(item.get("note") or "").strip(),
            }
        )
        if len(out) >= 2:
            break
    return out


async def nearby_airports(city: str) -> list[dict[str, str]]:
    """Return up to 2 commercial airports near *city*."""
    city = city.strip()
    if not city:
        return []

    from app.config import get_settings, stubs_enabled

    if stubs_enabled():
        return _stub_airports(city)

    llm = get_llm()
    try:
        data = await llm.complete_json(
            _AIRPORT_SYSTEM,
            f'Location: "{city}"',
        )
        airports = _sanitize_airports(data.get("airports") or [])
        if airports:
            return airports
    except LLMError:
        pass

    return _stub_airports(city)
