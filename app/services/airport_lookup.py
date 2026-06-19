"""Resolve cities to commercial airport IATA codes (no LLM).

Uses OpenFlights data (ODbL), tourism-city aliases, and optional SerpAPI
autocomplete as fallback. Rishikesh has no airport — maps to DED (Dehradun).
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent.parent / "data" / "airports.dat"

# Cities/regions without their own airport -> nearest commercial IATA code(s).
_CITY_ALIASES: dict[str, list[str]] = {
    "rishikesh": ["DED", "DEL"],
    "haridwar": ["DED", "DEL"],
    "dehradun": ["DED"],
    "dehra dun": ["DED"],
    "mussoorie": ["DED", "DEL"],
    "manali": ["KUU", "DEL"],
    "shimla": ["SLV", "DEL"],
    "goa": ["GOI", "GOX"],
    "panaji": ["GOI"],
    "jaipur": ["JAI"],
    "agra": ["AGR", "DEL"],
    "varanasi": ["VNS", "DEL"],
    "banaras": ["VNS"],
    "kolkata": ["CCU"],
    "calcutta": ["CCU"],
    "mumbai": ["BOM"],
    "bombay": ["BOM"],
    "bangalore": ["BLR"],
    "bengaluru": ["BLR"],
    "chennai": ["MAA"],
    "madras": ["MAA"],
    "hyderabad": ["HYD"],
    "pune": ["PNQ"],
    "kochi": ["COK"],
    "cochin": ["COK"],
    "new york": ["JFK", "LGA", "EWR"],
    "nyc": ["JFK", "LGA", "EWR"],
    "london": ["LHR", "LGW", "STN"],
    "paris": ["CDG", "ORY"],
    "tokyo": ["HND", "NRT"],
    "dubai": ["DXB", "DWC"],
    "singapore": ["SIN"],
    "bali": ["DPS"],
    "ubud": ["DPS"],
    "phuket": ["HKT"],
    "bangkok": ["BKK", "DMK"],
}


def _norm(city: str) -> str:
    return re.sub(r"\s+", " ", city.strip().lower().split(",")[0])


@lru_cache(maxsize=1)
def _openflights_index() -> dict[str, list[dict[str, str]]]:
    """city key -> airport records sorted by name."""
    by_city: dict[str, list[dict[str, str]]] = {}
    if not _DATA.exists():
        return by_city

    with _DATA.open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 14:
                continue
            iata = (row[4] or "").strip().upper()
            if not iata or iata == r"\N" or len(iata) != 3:
                continue
            name = row[1].strip()
            city = row[2].strip()
            if not city:
                continue
            rec = {"name": name, "iata": iata, "city": city, "note": ""}
            for key in {_norm(city), _norm(name.replace(" Airport", ""))}:
                by_city.setdefault(key, []).append(rec)

    return by_city


def lookup_iata_codes(city: str, *, max_codes: int = 2) -> list[str]:
    """Return up to *max_codes* IATA airport codes for a city or region."""
    if not city.strip():
        return []

    key = _norm(city)
    if key in _CITY_ALIASES:
        return _CITY_ALIASES[key][:max_codes]

    index = _openflights_index()
    if key in index:
        return [a["iata"] for a in index[key][:max_codes]]

    # Partial match: "delhi" in city name
    for city_key, airports in index.items():
        if key in city_key or city_key in key:
            return [a["iata"] for a in airports[:max_codes]]

    return []


def airport_records_for_codes(codes: list[str]) -> list[dict[str, str]]:
    """Build display records for IATA codes using OpenFlights names."""
    index = _openflights_index()
    by_iata: dict[str, dict[str, str]] = {}
    for airports in index.values():
        for a in airports:
            by_iata.setdefault(a["iata"], a)

    out: list[dict[str, str]] = []
    for code in codes:
        hit = by_iata.get(code)
        out.append(
            hit
            if hit
            else {"name": f"{code} Airport", "iata": code, "city": code, "note": ""}
        )
    return out


def resolve_airport_records(city: str, *, max_airports: int = 2) -> list[dict[str, str]]:
    """Sync resolver — alias map + OpenFlights."""
    codes = lookup_iata_codes(city, max_codes=max_airports)
    if codes:
        return airport_records_for_codes(codes)[:max_airports]
    slug = city.split(",")[0].strip()
    code = slug[:3].upper() if slug else "XXX"
    return [{"name": f"{slug} (unresolved)", "iata": code, "city": slug, "note": ""}]


def _iata_from_autocomplete_item(item: dict[str, Any]) -> str | None:
    for field in ("id", "code", "airport_id"):
        val = item.get(field)
        if isinstance(val, str) and len(val) == 3 and val.isalpha():
            return val.upper()
    name = str(item.get("name") or "")
    m = re.search(r"\(([A-Z]{3})\)", name)
    if m:
        return m.group(1)
    return None


async def resolve_iata_codes_with_fallback(city: str, *, serp_get) -> list[str]:
    """IATA codes with SerpAPI autocomplete fallback when local lookup fails."""
    codes = lookup_iata_codes(city, max_codes=2)
    if codes:
        return codes

    queries = [f"{city.split(',')[0].strip()} airport", city.split(",")[0].strip()]
    for q in queries:
        if not q.strip():
            continue
        data = await serp_get({"engine": "google_flights_autocomplete", "q": q.strip()})
        if data.get("available") is False:
            continue
        for bucket in ("airports", "cities", "places"):
            for item in data.get(bucket) or []:
                iata = _iata_from_autocomplete_item(item)
                if iata:
                    return [iata]
    return []
