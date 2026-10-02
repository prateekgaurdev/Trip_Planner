"""Rank flights and hotels against trip budget — no user picking required."""
from __future__ import annotations

import re
from datetime import date
from typing import Any


def _parse_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        nums = re.findall(r"[\d,]+\.?\d*", value.replace(",", ""))
        if nums:
            try:
                return float(nums[0])
            except ValueError:
                return None
    return None


def _trip_nights(start: str, end: str) -> int:
    try:
        s = date.fromisoformat(start[:10])
        e = date.fromisoformat(end[:10])
        return max(1, (e - s).days)
    except (TypeError, ValueError):
        return 1


def pick_top_flights(
    flights: dict[str, Any] | None,
    *,
    total_budget: float,
    currency: str = "USD",
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Return up to *top_n* distinct flight offers ranked by budget, speed, and airline diversity."""
    if not flights or not flights.get("available"):
        return []
    offers = flights.get("offers") or []
    if not offers:
        return []

    flight_cap = total_budget * 0.40 if total_budget > 0 else 1000.0
    scored: list[tuple[float, dict[str, Any], str]] = []
    seen_signatures: set[str] = set()

    for offer in offers:
        price = _parse_price(offer.get("price"))
        if price is None:
            continue
        airline = (offer.get("airlines") or "Flight").strip()
        route = (offer.get("route_iata") or "").strip()
        duration = int(offer.get("total_duration") or 600)
        stops = int(offer.get("stops") or 0)

        # 1. Deduplication: skip identical airline, route, price, duration, and stops
        sig = f"{airline.lower()}|{route.lower()}|{price}|{duration}|{stops}"
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)

        within = price <= flight_cap if flight_cap > 0 else True
        price_score = max(0.0, 100.0 - (price / flight_cap * 100)) if flight_cap > 0 else 50.0
        # Direct non-stop flights get heavy priority over multi-stop routes
        stop_score = 30.0 if stops == 0 else max(0.0, 15.0 - stops * 10.0)
        duration_score = max(0.0, 25.0 - (duration / 40.0))
        total = price_score + stop_score + duration_score + (15.0 if within else -15.0)

        if stops == 0 and within:
            blurb = f"Non-stop direct flight within budget."
        elif stops == 0:
            blurb = "Fastest non-stop connection."
        else:
            blurb = f"{stops} stop(s) · competitive fare."

        scored.append((
            total,
            {**offer, "price_numeric": price, "within_budget": within},
            blurb,
        ))

    scored.sort(key=lambda x: x[0], reverse=True)

    # 2. Diversity filter: avoid duplicate cards from the exact same airline in top picks
    selected: list[tuple[float, dict[str, Any], str]] = []
    seen_airlines: set[str] = set()

    # Pass 1: choose the best flight from each unique airline
    for item in scored:
        a_name = item[1].get("airlines", "Flight").strip().lower()
        if a_name not in seen_airlines:
            seen_airlines.add(a_name)
            selected.append(item)
            if len(selected) >= top_n:
                break

    # Pass 2: if fewer than top_n airlines operate, fill with next best distinct flights
    if len(selected) < top_n:
        for item in scored:
            if item not in selected:
                selected.append(item)
                if len(selected) >= top_n:
                    break

    picks: list[dict[str, Any]] = []
    for rank, (_, offer, blurb) in enumerate(selected[:top_n], start=1):
        stops_tag = "Non-stop" if offer.get("stops") == 0 else f"{offer.get('stops')} stop"
        if rank == 1:
            lbl = f"Top pick · Best Fare" if offer.get("within_budget") else f"Top pick · {stops_tag}"
        elif rank == 2:
            lbl = f"#2 · Alternative"
        else:
            lbl = f"#{rank} · Great Option"
        picks.append({"rank": rank, "offer": offer, "reason": blurb, "label": lbl})
    return picks


def pick_best_flight(
    flights: dict[str, Any] | None,
    *,
    total_budget: float,
    currency: str = "USD",
) -> dict[str, Any] | None:
    """Return the single best flight offer for the traveler's budget."""
    top = pick_top_flights(flights, total_budget=total_budget, currency=currency, top_n=1)
    if not top:
        return None
    best = top[0]["offer"]
    return {
        "offer": best,
        "reason": top[0]["reason"],
        "label": "Best flight for your budget",
    }


def pick_top_hotels(
    hotels: dict[str, Any] | None,
    *,
    total_budget: float,
    nights: int,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Return up to *top_n* hotel picks strictly ranked by high rating > cheap.
    
    Rule: Higher guest rating is prioritized. Verified cheap/affordable hotels
    get a value bonus, while unpriced hotels or low-rated options are deprioritized.
    """
    if not hotels or not hotels.get("available"):
        return []
    offers = hotels.get("offers") or []
    if not offers:
        return []

    per_night_cap = (total_budget * 0.35) / max(nights, 1) if total_budget > 0 else 150.0
    scored: list[tuple[float, dict[str, Any], str, str]] = []
    seen_names: set[str] = set()

    for offer in offers:
        raw_name = offer.get("name") or "Hotel"
        norm_name = re.sub(r"[^a-z0-9]", "", raw_name.lower())
        if norm_name in seen_names:
            continue
        seen_names.add(norm_name)

        price = _parse_price(offer.get("price"))
        rating = float(offer.get("rating") or 0.0)
        reviews = int(offer.get("reviews") or 0)

        # 1. Rating is primary: high rating > cheap
        # 4.6★ gives 184 pts; 4.2★ gives 168 pts (16 pt gap)
        rating_score = rating * 40.0
        review_bonus = min(8.0, reviews / 50.0)

        # 2. Price and budget feasibility
        if price is not None and per_night_cap > 0:
            within = price <= per_night_cap
            verified_price_bonus = 15.0
            if within:
                # Value bonus: affordable price relative to budget
                savings_ratio = max(0.0, 1.0 - (price / per_night_cap))
                value_bonus = savings_ratio * 15.0
                budget_penalty = 0.0
            else:
                value_bonus = 0.0
                budget_penalty = -25.0
        else:
            # Unpriced hotel (missing price) — penalty so it never beats a verified 4.6★ hotel
            within = False
            verified_price_bonus = 0.0
            value_bonus = 0.0
            budget_penalty = -40.0

        total_score = rating_score + review_bonus + verified_price_bonus + value_bonus + budget_penalty

        # User requirement: "tell the recommended one which is high rating > cheap"
        if rating >= 4.5 and price is not None and within:
            label_tag = f"Top Pick · Highest Rated ({rating}★)"
            blurb = f"Highest guest rating ({rating}★) & verified great price."
        elif rating >= 4.5 and price is not None:
            label_tag = f"Top Rated ({rating}★)"
            blurb = f"Exceptional {rating}★ guest rating."
        elif rating >= 4.2 and price is not None and within:
            label_tag = f"Best Value ({rating}★)"
            blurb = f"Strong {rating}★ rating at an affordable nightly rate."
        elif price is not None and within:
            label_tag = f"Budget Friendly"
            blurb = f"Great price within your nightly hotel budget."
        else:
            label_tag = f"Popular Hotel"
            blurb = f"{rating}★ stay (live pricing upon request)."

        scored.append((
            total_score,
            {**offer, "price_numeric": price, "within_budget": within},
            blurb,
            label_tag,
        ))

    scored.sort(key=lambda x: x[0], reverse=True)
    picks: list[dict[str, Any]] = []

    for rank, (_, offer, blurb, label_tag) in enumerate(scored[:top_n], start=1):
        if rank == 1:
            lbl = label_tag if "Top" in label_tag else f"Top pick · {label_tag}"
        elif rank == 2:
            lbl = f"Runner-up · {label_tag}" if "Top" not in label_tag else f"Runner-up ({offer.get('rating', '')}★)"
        else:
            lbl = f"Also great · {label_tag}" if "Top" not in label_tag else f"Option 3 ({offer.get('rating', '')}★)"
        picks.append({
            "rank": rank,
            "offer": offer,
            "reason": blurb,
            "label": lbl,
        })
    return picks


def build_selected_travel(
    selections: dict[str, Any] | None,
    travel_options: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build a summary block when the user picked flight and/or hotel at review."""
    if not selections:
        return None
    flight = selections.get("flight")
    hotel = selections.get("hotel")
    if not flight and not hotel:
        return None

    lines: list[str] = []
    if flight and isinstance(flight, dict):
        route = flight.get("route_iata") or ""
        if not route:
            dep = flight.get("departure_iata") or flight.get("departure", "")
            arr = flight.get("arrival_iata") or flight.get("arrival", "")
            route = f"{dep} → {arr}".strip(" →")
        airline = flight.get("airlines") or "Flight"
        dep_name = flight.get("departure") or ""
        arr_name = flight.get("arrival") or ""
        lines.append(
            f"Fly {route} ({airline})"
            + (f" — {dep_name} to {arr_name}" if dep_name or arr_name else "")
        )

    if hotel and isinstance(hotel, dict):
        name = hotel.get("name") or "Hotel"
        lines.append(f"Stay at {name}")

    airports = (travel_options or {}).get("flights", {}).get("airports") if travel_options else None

    return {
        "flight": flight,
        "hotel": hotel,
        "summary": "; ".join(lines) + "." if lines else "",
        "airports": airports,
        "note": "Your selections — not added to the local sightseeing route map.",
    }


def build_travel_recommendations(
    travel_options: dict[str, Any],
    preferences: dict[str, Any],
) -> dict[str, Any]:
    """Curated flight + hotel picks from raw SerpAPI results."""
    budget = float(preferences.get("budget") or 0)
    currency = preferences.get("currency") or "USD"
    nights = _trip_nights(preferences.get("start_date", ""), preferences.get("end_date", ""))

    flight_picks = pick_top_flights(
        travel_options.get("flights"), total_budget=budget, currency=currency, top_n=3
    )
    hotel_picks = pick_top_hotels(
        travel_options.get("hotels"),
        total_budget=budget,
        nights=nights,
        top_n=3,
    )

    return {
        "flights": flight_picks,
        "hotels": hotel_picks,
        "has_picks": bool(flight_picks or hotel_picks),
    }
