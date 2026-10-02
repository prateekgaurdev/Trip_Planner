"""Global location and airport auto-suggestion engine.

Provides instant search suggestions for cities, regions, and airports worldwide,
identifies the closest commercial airport, and computes Haversine distance in km.
"""
from __future__ import annotations

import csv
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent.parent / "data" / "airports.dat"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points on Earth in kilometers."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


# Popular tourist escapes / regions with exact center coordinates
DESTINATION_HUBS: list[dict[str, Any]] = [
    {"name": "Rishikesh, India", "city": "Rishikesh", "country": "India", "lat": 30.0869, "lon": 78.2676, "type": "destination", "alias": "DED"},
    {"name": "Amalfi Coast, Italy", "city": "Amalfi Coast", "country": "Italy", "lat": 40.6340, "lon": 14.6027, "type": "destination", "alias": "NAP"},
    {"name": "Zermatt, Switzerland", "city": "Zermatt", "country": "Switzerland", "lat": 45.9765, "lon": 7.7491, "type": "destination", "alias": "ZRH"},
    {"name": "Kyoto, Japan", "city": "Kyoto", "country": "Japan", "lat": 35.0116, "lon": 135.7681, "type": "destination", "alias": "KIX"},
    {"name": "Santorini, Greece", "city": "Santorini", "country": "Greece", "lat": 36.3932, "lon": 25.4615, "type": "destination", "alias": "JTR"},
    {"name": "Bali, Indonesia", "city": "Bali", "country": "Indonesia", "lat": -8.4095, "lon": 115.1889, "type": "destination", "alias": "DPS"},
    {"name": "Goa, India", "city": "Goa", "country": "India", "lat": 15.2993, "lon": 74.1240, "type": "destination", "alias": "GOI"},
    {"name": "Jaipur, India", "city": "Jaipur", "country": "India", "lat": 26.9124, "lon": 75.7873, "type": "destination", "alias": "JAI"},
    {"name": "Varanasi, India", "city": "Varanasi", "country": "India", "lat": 25.3176, "lon": 82.9739, "type": "destination", "alias": "VNS"},
    {"name": "Agra, India", "city": "Agra", "country": "India", "lat": 27.1767, "lon": 78.0081, "type": "destination", "alias": "AGR"},
    {"name": "Manali, India", "city": "Manali", "country": "India", "lat": 32.2432, "lon": 77.1892, "type": "destination", "alias": "KUU"},
    {"name": "Shimla, India", "city": "Shimla", "country": "India", "lat": 31.1048, "lon": 77.1734, "type": "destination", "alias": "SLV"},
    {"name": "Haridwar, India", "city": "Haridwar", "country": "India", "lat": 29.9457, "lon": 78.1642, "type": "destination", "alias": "DED"},
    {"name": "Darjeeling, India", "city": "Darjeeling", "country": "India", "lat": 27.0410, "lon": 88.2663, "type": "destination", "alias": "IXB"},
    {"name": "Ooty, India", "city": "Ooty", "country": "India", "lat": 11.4102, "lon": 76.6950, "type": "destination", "alias": "CJB"},
    {"name": "Munnar, India", "city": "Munnar", "country": "India", "lat": 10.0889, "lon": 77.0595, "type": "destination", "alias": "COK"},
    {"name": "Udaipur, India", "city": "Udaipur", "country": "India", "lat": 24.5854, "lon": 73.7125, "type": "destination", "alias": "UDR"},
    {"name": "Leh Ladakh, India", "city": "Leh Ladakh", "country": "India", "lat": 34.1526, "lon": 77.5771, "type": "destination", "alias": "IXL"},
    {"name": "Srinagar, India", "city": "Srinagar", "country": "India", "lat": 34.0837, "lon": 74.7973, "type": "destination", "alias": "SXR"},
    {"name": "Interlaken, Switzerland", "city": "Interlaken", "country": "Switzerland", "lat": 46.6863, "lon": 7.8632, "type": "destination", "alias": "BRN"},
    {"name": "Lucerne, Switzerland", "city": "Lucerne", "country": "Switzerland", "lat": 47.0502, "lon": 8.3093, "type": "destination", "alias": "ZRH"},
    {"name": "Lake Como, Italy", "city": "Lake Como", "country": "Italy", "lat": 45.9904, "lon": 9.2572, "type": "destination", "alias": "MXP"},
    {"name": "Cinque Terre, Italy", "city": "Cinque Terre", "country": "Italy", "lat": 44.1461, "lon": 9.6439, "type": "destination", "alias": "PSA"},
    {"name": "Dubrovnik, Croatia", "city": "Dubrovnik", "country": "Croatia", "lat": 42.6507, "lon": 18.0944, "type": "destination", "alias": "DBV"},
]


@lru_cache(maxsize=1)
def load_airports() -> list[dict[str, Any]]:
    """Parse airports.dat into a list of commercial airports with valid coordinates."""
    airports: list[dict[str, Any]] = []
    if not _DATA.exists():
        return airports

    with _DATA.open(encoding="utf-8", errors="ignore") as f:
        for row in csv.reader(f):
            if len(row) < 14:
                continue
            iata = (row[4] or "").strip().upper()
            if not iata or iata == r"\N" or len(iata) != 3:
                continue
            name = row[1].strip()
            city = row[2].strip()
            country = row[3].strip()
            if not city or not name:
                continue
            try:
                lat = float(row[6])
                lon = float(row[7])
            except ValueError:
                continue

            airports.append({
                "name": name,
                "city": city,
                "country": country,
                "iata": iata,
                "lat": lat,
                "lon": lon,
            })
    return airports


def find_closest_airport(lat: float, lon: float, exclude_iata: str | None = None) -> tuple[dict[str, Any] | None, int]:
    """Find the nearest commercial airport and return (airport_dict, distance_in_km)."""
    airports = load_airports()
    best: dict[str, Any] | None = None
    best_dist = float("inf")

    for a in airports:
        if exclude_iata and a["iata"] == exclude_iata:
            continue
        d = haversine(lat, lon, a["lat"], a["lon"])
        if d < best_dist:
            best_dist = d
            best = a

    return best, round(best_dist) if best else 0


@lru_cache(maxsize=256)
def suggest_places(query: str, for_type: str = "all", limit: int = 8) -> list[dict[str, Any]]:
    """Return place suggestions matching *query*, enriched with closest airport and km distance."""
    q = query.strip().lower()
    if not q:
        # Default trending escapes
        trending_keys = ["delhi", "mumbai", "rishikesh", "goa", "kyoto", "amalfi coast", "paris", "london", "zermatt", "tokyo", "new york"]
        results: list[dict[str, Any]] = []
        for dest in DESTINATION_HUBS:
            if dest["city"].lower() in trending_keys:
                ap, dist = find_closest_airport(dest["lat"], dest["lon"])
                results.append({
                    "name": dest["name"],
                    "city": dest["city"],
                    "country": dest["country"],
                    "iata": dest.get("alias") or (ap["iata"] if ap else ""),
                    "type": dest["type"],
                    "closest_airport_name": ap["name"] if ap else "",
                    "closest_airport_iata": ap["iata"] if ap else "",
                    "distance_km": dist,
                    "sub": f"✈ Nearest: {ap['name']} ({ap['iata']}) · {dist} km" if ap else "",
                })
        return results[:limit]

    airports = load_airports()
    scored_candidates: list[tuple[float, dict[str, Any]]] = []
    seen_keys: set[str] = set()

    # 1. Match against curated destinations/escapes
    for dest in DESTINATION_HUBS:
        city_lower = dest["city"].lower()
        name_lower = dest["name"].lower()
        alias_lower = (dest.get("alias") or "").lower()

        score = 0.0
        if city_lower == q or alias_lower == q:
            score = 100.0
        elif city_lower.startswith(q):
            score = 85.0 - len(city_lower) * 0.5
        elif bool(re.search(r"\b" + re.escape(q), name_lower)):
            score = 75.0
        elif q in name_lower:
            score = 60.0

        if score > 0:
            ap, dist = find_closest_airport(dest["lat"], dest["lon"])
            rec = {
                "name": dest["name"],
                "city": dest["city"],
                "country": dest["country"],
                "iata": dest.get("alias") or (ap["iata"] if ap else ""),
                "type": "destination",
                "closest_airport_name": ap["name"] if ap else "",
                "closest_airport_iata": ap["iata"] if ap else "",
                "distance_km": dist,
                "sub": f"✈ Closest: {ap['name']} ({ap['iata']}) · {dist} km" if ap else "",
            }
            key = f"{dest['city']}_{dest['country']}".lower()
            if key not in seen_keys:
                seen_keys.add(key)
                scored_candidates.append((score, rec))

    # 2. Match against global OpenFlights airports database (5,560 commercial airports worldwide)
    for a in airports:
        city_lower = a["city"].lower()
        name_lower = a["name"].lower()
        iata_lower = a["iata"].lower()

        score = 0.0
        if iata_lower == q:
            score = 98.0
        elif city_lower == q:
            score = 92.0
        elif iata_lower.startswith(q):
            score = 88.0
        elif city_lower.startswith(q):
            score = 80.0 - len(city_lower) * 0.4
        elif bool(re.search(r"\b" + re.escape(q), name_lower)):
            score = 70.0
        elif q in city_lower or q in name_lower:
            score = 50.0

        if score > 0:
            key = f"{a['city']}_{a['country']}_{a['iata']}".lower()
            if key not in seen_keys:
                seen_keys.add(key)
                rec = {
                    "name": f"{a['city']}, {a['country']}",
                    "city": a["city"],
                    "country": a["country"],
                    "iata": a["iata"],
                    "type": "airport",
                    "closest_airport_name": a["name"],
                    "closest_airport_iata": a["iata"],
                    "distance_km": 0,
                    "sub": f"✈ {a['name']} ({a['iata']}) · Direct Airport Hub",
                }
                scored_candidates.append((score, rec))

    # Sort descending by relevance score
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored_candidates[:limit]]
