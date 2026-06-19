"""Resolve nearby commercial airports for a city — fast local lookup, no LLM."""
from __future__ import annotations

from app.services.airport_lookup import resolve_airport_records


async def nearby_airports(city: str) -> list[dict[str, str]]:
    """Return up to 2 commercial airports near *city*."""
    city = city.strip()
    if not city:
        return []

    from app.config import stubs_enabled

    if stubs_enabled():
        from app.services.airport_lookup import lookup_iata_codes, airport_records_for_codes

        codes = lookup_iata_codes(city) or ["AAA", "BBB"]
        return airport_records_for_codes(codes[:2])

    return resolve_airport_records(city, max_airports=2)
