"""SerpAPI integrations — optional flight & hotel price discovery.

Flight search resolves cities to IATA airport codes (OpenFlights + aliases)
and queries SerpAPI Google Flights. Results are informational only.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from app.config import stubs_enabled

_SERP_URL = "https://serpapi.com/search.json"

_GL_BY_CURRENCY = {"INR": "in", "USD": "us", "EUR": "de", "GBP": "gb", "AUD": "au", "CAD": "ca"}


async def _serp_get(params: dict[str, Any]) -> dict[str, Any]:
    from app.config import get_settings
    from app.pipeline_log import api_end, api_start

    engine = params.get("engine", "serp")
    t0 = api_start("SerpAPI", engine, q=str(params.get("q") or f"{params.get('departure_id', '')}->{params.get('arrival_id', '')}" or "")[:40])
    key = get_settings().serpapi_key
    if not key:
        api_end("SerpAPI", engine, t0, ok=False, reason="no key")
        return {"available": False, "reason": "SERPAPI_KEY not configured."}

    async with httpx.AsyncClient(timeout=30) as http:
        try:
            res = await http.get(_SERP_URL, params={**params, "api_key": key})
            res.raise_for_status()
            data = res.json()
            if data.get("error"):
                api_end("SerpAPI", engine, t0, ok=False, error=str(data["error"])[:80])
                return {"available": False, "reason": str(data["error"])}
            offers = len(data.get("properties") or data.get("best_flights") or data.get("other_flights") or [])
            api_end("SerpAPI", engine, t0, offers=offers if offers else "ok")
            return data
        except (httpx.HTTPError, ValueError) as exc:
            api_end("SerpAPI", engine, t0, ok=False, error=str(exc)[:80])
            return {"available": False, "reason": f"SerpAPI request failed: {exc}"}


async def _resolve_flight_location(query: str) -> str | None:
    """Resolve to an IATA airport code (SerpAPI accepts IATA directly)."""
    from app.services.airport_lookup import lookup_iata_codes, resolve_iata_codes_with_fallback

    codes = lookup_iata_codes(query, max_codes=1)
    if codes:
        return codes[0]
    resolved = await resolve_iata_codes_with_fallback(query, serp_get=_serp_get)
    return resolved[0] if resolved else None


def _extract_price(raw: Any) -> int | float | str | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("extracted_price") or raw.get("raw") or raw.get("lowest")
    return None


def _parse_flight_offers(
    data: dict[str, Any],
    *,
    dep_iata: str,
    arr_iata: str,
    dep_name: str,
    arr_name: str,
    limit: int = 4,
) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    for bucket in ("best_flights", "other_flights"):
        for item in (data.get(bucket) or [])[:limit]:
            if len(offers) >= limit:
                break
            legs = item.get("flights") or []
            first = legs[0] if legs else {}
            last = legs[-1] if legs else {}
            dep = first.get("departure_airport") or {}
            arr = last.get("arrival_airport") or {}
            dep_code = (dep.get("id") or dep_iata or "").upper()
            arr_code = (arr.get("id") or arr_iata or "").upper()
            airlines = ", ".join(
                dict.fromkeys(leg.get("airline", "") for leg in legs if leg.get("airline"))
            )
            offers.append(
                {
                    "price": _extract_price(item.get("price")),
                    "total_duration": item.get("total_duration"),
                    "airlines": airlines,
                    "departure": dep.get("name") or dep_name,
                    "departure_time": dep.get("time") or "",
                    "arrival": arr.get("name") or arr_name,
                    "arrival_time": arr.get("time") or "",
                    "stops": max(0, len(legs) - 1),
                    "type": item.get("type") or bucket.replace("_", " "),
                    "route_iata": f"{dep_code} -> {arr_code}",
                    "departure_iata": dep_code,
                    "arrival_iata": arr_code,
                }
            )
    return offers


def _price_sort_key(offer: dict[str, Any]) -> float:
    price = offer.get("price")
    if isinstance(price, (int, float)):
        return float(price)
    if isinstance(price, str):
        nums = re.findall(r"[\d,]+\.?\d*", price.replace(",", ""))
        if nums:
            try:
                return float(nums[0])
            except ValueError:
                pass
    return float("inf")


def _parse_hotel_offers(data: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    for item in (data.get("properties") or [])[:limit]:
        rate = item.get("rate_per_night") or {}
        price = rate.get("lowest") or rate.get("extracted_lowest") or item.get("price")
        images = item.get("images") or []
        thumb = None
        if images:
            thumb = images[0].get("thumbnail") or images[0].get("original_image")
        offers.append(
            {
                "name": item.get("name") or "Hotel",
                "price": price,
                "rating": item.get("overall_rating") or item.get("rating"),
                "reviews": item.get("reviews") or item.get("reviews_count"),
                "hotel_class": item.get("hotel_class") or item.get("class"),
                "link": item.get("link") or item.get("serpapi_property_details_link"),
                "thumbnail": thumb,
                "description": item.get("description") or "",
            }
        )
    return offers


def _stub_flights(
    origin_city: str,
    destination_city: str,
    origin_airports: list[dict[str, str]],
    dest_airports: list[dict[str, str]],
) -> dict[str, Any]:
    dep = origin_airports[0]
    arr = dest_airports[0]
    dep_iata, arr_iata = dep["iata"], arr["iata"]
    return {
        "available": True,
        "source": "stub",
        "origin_city": origin_city,
        "destination_city": destination_city,
        "airports": {"origin": origin_airports, "destination": dest_airports},
        "outbound_date": "",
        "return_date": "",
        "offers": [
            {
                "price": 420,
                "total_duration": 540,
                "airlines": "Demo Air",
                "departure": dep["name"],
                "departure_time": "08:30",
                "arrival": arr["name"],
                "arrival_time": "17:15",
                "stops": 1,
                "type": "best",
                "route_iata": f"{dep_iata} → {arr_iata}",
                "departure_iata": dep_iata,
                "arrival_iata": arr_iata,
            },
            {
                "price": 510,
                "total_duration": 480,
                "airlines": "Sample Airways",
                "departure": origin_airports[1]["name"] if len(origin_airports) > 1 else dep["name"],
                "departure_time": "14:00",
                "arrival": arr["name"],
                "arrival_time": "22:10",
                "stops": 0,
                "type": "other",
                "route_iata": f"{(origin_airports[1]['iata'] if len(origin_airports) > 1 else dep_iata)} → {arr_iata}",
                "departure_iata": origin_airports[1]["iata"] if len(origin_airports) > 1 else dep_iata,
                "arrival_iata": arr_iata,
            },
            {
                "price": 545,
                "total_duration": 600,
                "airlines": "Budget Fly",
                "departure": dep["name"],
                "departure_time": "06:15",
                "arrival": arr["name"],
                "arrival_time": "16:00",
                "stops": 2,
                "type": "other",
                "route_iata": f"{dep_iata} → {arr_iata}",
                "departure_iata": dep_iata,
                "arrival_iata": arr_iata,
            },
        ],
    }


def _stub_hotels(destination: str) -> dict[str, Any]:
    return {
        "available": True,
        "source": "stub",
        "destination": destination,
        "offers": [
            {
                "name": f"Central Inn {destination.split(',')[0]}",
                "price": "$89",
                "rating": 4.3,
                "reviews": 812,
                "hotel_class": "3-star",
                "link": "",
                "thumbnail": None,
                "description": "Walkable downtown stay — stub demo.",
            },
            {
                "name": f"Boutique Stay {destination.split(',')[0]}",
                "price": "$142",
                "rating": 4.7,
                "reviews": 456,
                "hotel_class": "4-star",
                "link": "",
                "thumbnail": None,
                "description": "Stylish rooms near main sights — stub demo.",
            },
        ],
    }


async def _fetch_iata_pair(
    dep_iata: str,
    arr_iata: str,
    outbound_date: str,
    return_date: str,
    *,
    adults: int,
    currency: str,
) -> list[dict[str, Any]]:
    gl = _GL_BY_CURRENCY.get(currency.upper(), "us")
    data = await _serp_get(
        {
            "engine": "google_flights",
            "departure_id": dep_iata,
            "arrival_id": arr_iata,
            "outbound_date": outbound_date,
            "return_date": return_date,
            "type": "1",
            "adults": max(1, adults),
            "currency": currency,
            "hl": "en",
            "gl": gl,
        }
    )
    if data.get("available") is False:
        return []

    index = _openflights_names()
    dep_name = index.get(dep_iata, dep_iata)
    arr_name = index.get(arr_iata, arr_iata)
    return _parse_flight_offers(
        data,
        dep_iata=dep_iata,
        arr_iata=arr_iata,
        dep_name=dep_name,
        arr_name=arr_name,
        limit=8,
    )


def _openflights_names() -> dict[str, str]:
    from app.services.airport_lookup import _openflights_index

    names: dict[str, str] = {}
    for airports in _openflights_index().values():
        for a in airports:
            names.setdefault(a["iata"], a["name"])
    return names


async def search_flights(
    origin_city: str,
    destination_city: str,
    outbound_date: str,
    return_date: str,
    *,
    adults: int = 1,
    currency: str = "USD",
) -> dict[str, Any]:
    """Round-trip flight offers via SerpAPI Google Flights (IATA airport codes)."""
    from app.services.airport_lookup import (
        airport_records_for_codes,
        resolve_iata_codes_with_fallback,
    )
    from app.services.travel_cache import get_cached, set_cached

    if not origin_city.strip() or not destination_city.strip():
        return {"available": False, "reason": "From and To cities are required for flights."}

    cache_key = {
        "origin": origin_city.strip().lower(),
        "destination": destination_city.strip().lower(),
        "outbound": outbound_date,
        "return": return_date,
        "adults": adults,
        "currency": currency.upper(),
    }
    if not stubs_enabled():
        cached = get_cached("flights", cache_key)
        if cached is not None:
            from app.pipeline_log import LOG
            LOG.debug("Travel cache hit | flights | %s -> %s", origin_city, destination_city)
            return cached

    if stubs_enabled():
        origin_airports = airport_records_for_codes(["DEL"] if "delhi" in origin_city.lower() else ["AAA"])
        dest_airports = airport_records_for_codes(["DED"] if "rishikesh" in destination_city.lower() else ["BBB"])
        result = _stub_flights(origin_city, destination_city, origin_airports, dest_airports)
        result["outbound_date"] = outbound_date
        result["return_date"] = return_date
        result["currency"] = currency
        return result

    origin_codes, dest_codes = await asyncio.gather(
        resolve_iata_codes_with_fallback(origin_city, serp_get=_serp_get),
        resolve_iata_codes_with_fallback(destination_city, serp_get=_serp_get),
    )

    if not origin_codes or not dest_codes:
        return {
            "available": False,
            "reason": "Could not resolve airports for the cities.",
            "origin_city": origin_city,
            "destination_city": destination_city,
        }

    pairs: list[tuple[str, str]] = [(origin_codes[0], dest_codes[0])]
    for alt in dest_codes[1:]:
        if alt != dest_codes[0] and alt != origin_codes[0]:
            pairs.append((origin_codes[0], alt))
            break

    # If primary route already returned offers, skip alternate pair (saves ~0.5-1s).
    primary = await _fetch_iata_pair(
        pairs[0][0], pairs[0][1], outbound_date, return_date, adults=adults, currency=currency
    )
    offers: list[dict[str, Any]] = list(primary)
    if len(pairs) > 1 and len(offers) < 3:
        secondary = await _fetch_iata_pair(
            pairs[1][0], pairs[1][1], outbound_date, return_date, adults=adults, currency=currency
        )
        batches = [primary, secondary]
    else:
        batches = [primary]
    seen: set[tuple[Any, ...]] = set()
    for batch in batches:
        for offer in batch:
            key = (
                offer.get("route_iata"),
                offer.get("price"),
                offer.get("airlines"),
                offer.get("departure_time"),
            )
            if key in seen:
                continue
            seen.add(key)
            offers.append(offer)

    offers.sort(key=_price_sort_key)

    origin_airports = airport_records_for_codes(origin_codes[:2])
    dest_airports = airport_records_for_codes(dest_codes[:2])

    if not offers:
        return {
            "available": False,
            "reason": "No flight offers returned for this route.",
            "origin_city": origin_city,
            "destination_city": destination_city,
            "airports": {"origin": origin_airports, "destination": dest_airports},
            "resolved_route": f"{origin_codes[0]} -> {dest_codes[0]}",
        }

    result = {
        "available": True,
        "source": "serpapi",
        "origin_city": origin_city,
        "destination_city": destination_city,
        "airports": {"origin": origin_airports, "destination": dest_airports},
        "resolved_route": f"{origin_codes[0]} -> {dest_codes[0]}",
        "outbound_date": outbound_date,
        "return_date": return_date,
        "currency": currency,
        "offers": offers[:8],
    }
    set_cached("flights", cache_key, result)
    return result


async def search_hotels(
    destination: str,
    check_in: str,
    check_out: str,
    *,
    adults: int = 2,
    currency: str = "USD",
) -> dict[str, Any]:
    """Hotel offers for the trip destination (optional price check)."""
    from app.services.travel_cache import get_cached, set_cached

    cache_key = {
        "destination": destination.strip().lower(),
        "check_in": check_in,
        "check_out": check_out,
        "adults": adults,
        "currency": currency.upper(),
    }
    if not stubs_enabled():
        cached = get_cached("hotels", cache_key)
        if cached is not None:
            from app.pipeline_log import LOG
            LOG.debug("Travel cache hit | hotels | %s", destination)
            return cached

    if stubs_enabled():
        return _stub_hotels(destination)

    if not destination.strip():
        return {"available": False, "reason": "Destination required for hotels."}

    dest_city = destination.split(",")[0].strip()
    gl = _GL_BY_CURRENCY.get(currency.upper(), "us")
    data = await _serp_get(
        {
            "engine": "google_hotels",
            "q": f"hotels in {dest_city}",
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": max(1, adults),
            "currency": currency,
            "gl": gl,
            "hl": "en",
        }
    )
    if data.get("available") is False:
        return data

    offers = _parse_hotel_offers(data)
    if not offers:
        return {
            "available": False,
            "reason": "No hotel offers returned.",
            "destination": destination,
        }

    result = {
        "available": True,
        "source": "serpapi",
        "destination": destination,
        "check_in": check_in,
        "check_out": check_out,
        "currency": currency,
        "offers": offers,
    }
    set_cached("hotels", cache_key, result)
    return result


async def fetch_travel_options(preferences: dict[str, Any]) -> dict[str, Any]:
    """Fetch flight/hotel data in parallel and attach budget-aware recommendations."""
    from app.pipeline_log import api_end, api_start
    from app.services.travel_picks import build_travel_recommendations

    origin = (preferences.get("origin") or "").strip()
    dest = preferences.get("destination", "")
    t0 = api_start(
        "Travel",
        "fetch_options",
        origin=origin or "(skip flights)",
        destination=dest,
    )
    trip_destination = preferences.get("destination", "")
    flight_to = (preferences.get("flight_destination") or "").strip()
    currency = preferences.get("currency") or "USD"
    travelers = int(preferences.get("travelers") or 1)
    start = preferences.get("start_date", "")
    end = preferences.get("end_date", "")

    result: dict[str, Any] = {"flights": None, "hotels": None, "recommendations": None}

    async def _hotels_task() -> dict[str, Any] | None:
        if not trip_destination:
            return None
        return await search_hotels(
            trip_destination, start, end, adults=travelers, currency=currency
        )

    async def _flights_task() -> dict[str, Any] | None:
        if not origin:
            return None
        dest = flight_to or trip_destination
        if not dest:
            return None
        return await search_flights(
            origin, dest, start, end, adults=travelers, currency=currency
        )

    hotels, flights = await asyncio.gather(_hotels_task(), _flights_task())
    result["hotels"] = hotels
    result["flights"] = flights
    result["recommendations"] = build_travel_recommendations(result, preferences)
    flight_n = len((flights or {}).get("offers") or []) if flights and flights.get("available") else 0
    hotel_n = len((hotels or {}).get("offers") or []) if hotels and hotels.get("available") else 0
    route = (flights or {}).get("resolved_route") if flights else None
    api_end("Travel", "fetch_options", t0, flights=flight_n, hotels=hotel_n, route=route or "-")
    return result
