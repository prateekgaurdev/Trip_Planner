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
    """Return up to *top_n* flight offers ranked by budget fit."""
    if not flights or not flights.get("available"):
        return []
    offers = flights.get("offers") or []
    if not offers:
        return []

    flight_cap = total_budget * 0.40
    scored: list[tuple[float, dict[str, Any], str]] = []

    for offer in offers:
        price = _parse_price(offer.get("price"))
        if price is None:
            continue
        stops = int(offer.get("stops") or 0)
        duration = int(offer.get("total_duration") or 600)
        price_score = max(0.0, 100.0 - (price / flight_cap * 100)) if flight_cap > 0 else 40.0
        stop_score = max(0.0, 24.0 - stops * 10.0)
        duration_score = max(0.0, 18.0 - duration / 45.0)
        within = price <= flight_cap
        total = price_score + stop_score + duration_score + (10.0 if within else 0.0)
        blurb = "Best value for your budget." if within else "Competitive fare."
        scored.append(
            (
                total,
                {**offer, "price_numeric": price, "within_budget": within},
                blurb,
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    picks: list[dict[str, Any]] = []
    labels = {1: "Top pick", 2: "#2", 3: "#3"}
    for rank, (_, offer, blurb) in enumerate(scored[:top_n], start=1):
        picks.append({"rank": rank, "offer": offer, "reason": blurb, "label": labels.get(rank, f"#{rank}")})
    return picks


def pick_best_flight(
    flights: dict[str, Any] | None,
    *,
    total_budget: float,
    currency: str = "USD",
) -> dict[str, Any] | None:
    """Return the single best flight offer for the traveler's budget."""
    if not flights or not flights.get("available"):
        return None
    offers = flights.get("offers") or []
    if not offers:
        return None

    flight_cap = total_budget * 0.40
    scored: list[tuple[float, dict[str, Any]]] = []

    for offer in offers:
        price = _parse_price(offer.get("price"))
        if price is None:
            continue
        stops = int(offer.get("stops") or 0)
        duration = int(offer.get("total_duration") or 600)

        price_score = max(0.0, 100.0 - (price / flight_cap * 100)) if flight_cap > 0 else 40.0
        stop_score = max(0.0, 24.0 - stops * 10.0)
        duration_score = max(0.0, 18.0 - duration / 45.0)
        within = price <= flight_cap
        bonus = 25.0 if within else -15.0
        total = price_score + stop_score + duration_score + bonus

        scored.append(
            (
                total,
                {
                    **offer,
                    "price_numeric": price,
                    "within_budget": within,
                    "budget_share_pct": round(price / total_budget * 100, 1) if total_budget else None,
                },
            )
        )

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    within = best.get("within_budget")
    cap_pct = round(flight_cap / total_budget * 100) if total_budget else 40

    if within:
        reason = (
            f"Best balance of price, stops, and duration for your {currency} "
            f"{total_budget:,.0f} budget (uses ~{best.get('budget_share_pct')}% of total)."
        )
    else:
        reason = (
            f"Cheapest strong option found. Slightly above the typical {cap_pct}% "
            f"flight slice of your {currency} {total_budget:,.0f} budget."
        )

    return {
        "offer": best,
        "reason": reason,
        "label": "Best flight for your budget",
    }


def pick_top_hotels(
    hotels: dict[str, Any] | None,
    *,
    total_budget: float,
    nights: int,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Return up to *top_n* hotel picks ranked by rating and value vs budget."""
    if not hotels or not hotels.get("available"):
        return []
    offers = hotels.get("offers") or []
    if not offers:
        return []

    per_night_cap = (total_budget * 0.35) / max(nights, 1)
    ranked: list[tuple[float, dict[str, Any], str]] = []

    for offer in offers:
        price = _parse_price(offer.get("price"))
        rating = float(offer.get("rating") or 3.5)
        reviews = int(offer.get("reviews") or 0)
        review_bonus = min(8.0, reviews / 200.0)

        if price is not None and per_night_cap > 0:
            value = rating * 22.0 + review_bonus - (price / per_night_cap) * 12.0
            within = price <= per_night_cap
        else:
            value = rating * 22.0 + review_bonus
            within = True

        if within and rating >= 4.0:
            blurb = "Great rating within your hotel budget."
        elif within:
            blurb = "Solid value for your nightly budget."
        elif price is not None:
            blurb = "Higher-end pick — premium stay worth considering."
        else:
            blurb = "Highly rated option in the area."

        ranked.append((value + (10.0 if within else 0.0), {**offer, "price_numeric": price, "within_budget": within}, blurb))

    ranked.sort(key=lambda x: x[0], reverse=True)
    picks: list[dict[str, Any]] = []
    for rank, (_, offer, blurb) in enumerate(ranked[:top_n], start=1):
        picks.append(
            {
                "rank": rank,
                "offer": offer,
                "reason": blurb,
                "label": {1: "Top pick", 2: "Runner-up", 3: "Also great"}.get(rank, f"#{rank}"),
            }
        )
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
