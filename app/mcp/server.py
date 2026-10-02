"""Wayfarer Travel Concierge — Standalone Model Context Protocol (MCP) Server.

Compliant with Anthropic's Model Context Protocol (MCP) 2024-11-05 standard.
Exposes travel concierge tools for AI agents and MCP clients:
- generate_itinerary_pdf: Generates high-fidelity visual PDF dossier
- generate_calendar_ics: Builds RFC 5545 iCalendar feed (.ics)
- fetch_travel_safety_and_etiquette: Emergency numbers, cultural rules, and tipping
- generate_smart_packing_list: Weather-informed packing recommendations
- convert_and_budget_currency: Local currency economics and cash/card guidance
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import re
import sys
import uuid
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

from app.mcp.pdf_generator import build_itinerary_pdf

logger = logging.getLogger("wayfarer.mcp_server")

try:
    server = FastMCP(
        name="wayfarer-travel-concierge",
        version="1.0.0",
        description="Wayfarer Travel Concierge MCP Server: PDF generation, calendar sync, travel advisories, and packing lists.",
    )
except TypeError:
    # FastMCP in mcp 1.x only accepts the name argument
    server = FastMCP("wayfarer-travel-concierge")

# ─── Tool 1: Visual PDF Itinerary ─────────────────────────────────────────────
@server.tool()
def generate_itinerary_pdf(plan_json: str) -> dict[str, Any]:
    """Generate a multi-page, publication-grade visual PDF itinerary from a JSON plan payload.
    
    Returns base64-encoded PDF bytes, filename, and byte size.
    """
    try:
        plan_data = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except Exception as exc:
        return {"error": f"Invalid plan JSON: {exc}", "success": False}

    dest = (
        plan_data.get("destination")
        or (plan_data.get("preferences") or {}).get("destination")
        or "Trip"
    )
    safe_dest = re.sub(r"[^\w\-]", "_", dest).strip("_")
    filename = f"Wayfarer_Itinerary_{safe_dest}.pdf"

    try:
        pdf_bytes = build_itinerary_pdf(plan_data)
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        return {
            "success": True,
            "filename": filename,
            "size_bytes": len(pdf_bytes),
            "pdf_base64": b64,
            "mimetype": "application/pdf",
        }
    except Exception as exc:
        logger.exception("Failed to generate PDF in MCP server")
        return {"error": f"PDF generation failed: {exc}", "success": False}


# ─── Tool 2: RFC 5545 iCalendar Sync ──────────────────────────────────────────
@server.tool()
def generate_calendar_ics(
    trip_title: str,
    start_date: str,
    days_json: str,
    lodging_name: str = "Curated Lodging",
) -> str:
    """Build an RFC 5545 compliant .ics calendar file for the entire trip itinerary.
    
    Creates scheduled calendar events for morning lodging departures, daily sight visits
    with estimated durations, and evening returns to base.
    """
    try:
        days = json.loads(days_json) if isinstance(days_json, str) else (days_json or [])
    except Exception:
        days = []

    # Parse start date
    base_date = dt.date.today()
    if start_date:
        try:
            base_date = dt.date.fromisoformat(start_date.split("T")[0])
        except ValueError:
            pass

    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cal_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Wayfarer AI//Travel Concierge MCP//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{trip_title} - Wayfarer Itinerary",
        "X-WR-TIMEZONE:UTC",
    ]

    for day_idx, d in enumerate(days):
        day_date = base_date + dt.timedelta(days=day_idx)
        date_str = day_date.strftime("%Y%m%d")

        cluster = d.get("neighborhood_cluster") or d.get("theme") or ""
        day_title = d.get("title") or f"Day {day_idx + 1}"

        # 1. Morning departure event
        uid_dep = f"dep-{date_str}-{uuid.uuid4().hex[:8]}@wayfarer.ai"
        cal_lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid_dep}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART:{date_str}T090000",
            f"DTEND:{date_str}T093000",
            f"SUMMARY:Depart Base ({lodging_name}) -> {cluster or day_title}",
            f"DESCRIPTION:Morning departure from lodging anchor. Head to daily cluster.",
            f"LOCATION:{lodging_name}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ])

        # 2. Daily activities
        acts = d.get("activities") or []
        current_hour = 10
        current_min = 0

        for act_idx, act in enumerate(acts):
            raw_time = str(act.get("time") or "")
            # Try to parse HH:MM
            m = re.search(r"(\d{1,2}):(\d{2})", raw_time)
            if m:
                act_hour = int(m.group(1))
                act_min = int(m.group(2))
            else:
                act_hour = current_hour
                act_min = current_min

            start_iso = f"{date_str}T{act_hour:02d}{act_min:02d}00"
            end_hour = min(23, act_hour + 2)
            end_iso = f"{date_str}T{end_hour:02d}{act_min:02d}00"

            act_title = act.get("title") or act.get("name") or f"Activity {act_idx + 1}"
            act_desc = act.get("description") or ""
            act_loc = act.get("location_name") or act.get("location") or cluster or ""

            uid_act = f"act-{date_str}-{act_idx}-{uuid.uuid4().hex[:8]}@wayfarer.ai"
            cal_lines.extend([
                "BEGIN:VEVENT",
                f"UID:{uid_act}",
                f"DTSTAMP:{now_utc}",
                f"DTSTART:{start_iso}",
                f"DTEND:{end_iso}",
                f"SUMMARY:{act_title}",
                f"DESCRIPTION:{act_desc}",
                f"LOCATION:{act_loc}",
                "STATUS:CONFIRMED",
                "END:VEVENT",
            ])

            current_hour = min(20, end_hour + 1)

        # 3. Evening return event
        uid_ret = f"ret-{date_str}-{uuid.uuid4().hex[:8]}@wayfarer.ai"
        cal_lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid_ret}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART:{date_str}T210000",
            f"DTEND:{date_str}T213000",
            f"SUMMARY:Return to Base ({lodging_name})",
            f"DESCRIPTION:Evening return to lodging base. Relax and recharge.",
            f"LOCATION:{lodging_name}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ])

    cal_lines.append("END:VCALENDAR")
    return "\r\n".join(cal_lines) + "\r\n"


# ─── Tool 3: Travel Safety, Emergency Numbers & Etiquette ─────────────────────
@server.tool()
def fetch_travel_safety_and_etiquette(destination: str) -> dict[str, Any]:
    """Fetch official safety hotlines, emergency numbers, visa & transit cards, and cultural etiquette.
    
    Provides local emergency contacts (police, medical, tourist helpline) and cultural norms
    (tipping etiquette, temple/church dress codes, transit customs).
    """
    dest_lower = destination.lower()

    if any(k in dest_lower for k in ("japan", "tokyo", "kyoto", "osaka")):
        return {
            "country": "Japan",
            "emergency_numbers": {
                "Police": "110",
                "Ambulance / Fire": "119",
                "Japan Helpline (English 24/7)": "0570-000-911",
                "Japan Visitor Hotline (JNTO)": "050-3816-2787",
            },
            "tipping_culture": "No tipping is customary or expected in Japan. Tipping in restaurants or taxis can cause polite confusion.",
            "cultural_etiquette": [
                "Remove shoes when entering traditional accommodations, temples, and homes.",
                "Keep phone conversations silent on commuter trains and express Shinkansen.",
                "Carry cash (JPY) for local street shrines, vending machines, and ramen counters.",
                "Stand on the left side of escalators in Tokyo, but on the right side in Osaka.",
            ],
            "transit_card_recommendation": "IC Card (Suica, Pasmo, or ICOCA on Apple Wallet) covers all subways, buses, and convenience stores.",
        }

    if any(k in dest_lower for k in ("india", "delhi", "mumbai", "jaipur", "rishikesh", "goa", "agra", "varanasi", "bengaluru", "kochi")):
        return {
            "country": "India",
            "emergency_numbers": {
                "Unified Emergency Hotline": "112",
                "Police": "100",
                "Ambulance": "102",
                "Tourist Helpline (Toll-Free)": "1363",
            },
            "tipping_culture": "5% to 10% is customary in sit-down restaurants unless a 'Service Charge' is already added to the bill.",
            "cultural_etiquette": [
                "Cover shoulders and knees when visiting temples, mosques, and gurudwaras. Remove footwear at entrances.",
                "Use UPI (QR codes) or debit cards for most dining and shopping, but carry small INR notes for auto-rickshaws and street markets.",
                "Drink bottled, RO-filtered, or sealed mineral water only.",
                "Respect photography prohibitions inside sacred sanctums.",
            ],
            "transit_card_recommendation": "Use ride-hailing apps (Uber, Ola) for transparent city transit; Metro smart cards save queuing time in Delhi and Bengaluru.",
        }

    if any(k in dest_lower for k in ("portugal", "lisbon", "porto")):
        return {
            "country": "Portugal",
            "emergency_numbers": {
                "European Emergency": "112",
                "National Police (PSP)": "21 358 8300",
                "Tourist Police (Lisbon)": "+351 213 421 634",
            },
            "tipping_culture": "Tipping is not mandatory. Leaving 5–10% for good service in restaurants is appreciated.",
            "cultural_etiquette": [
                "Wear comfortable shoes with rubber grip for historic cobblestone hills (calçada portuguesa).",
                "Appetizers brought to your table (bread, cheese, olives) are charged only if consumed.",
                "Greeting shopkeepers with 'Bom dia' or 'Boa tarde' is standard polite courtesy.",
            ],
            "transit_card_recommendation": "Viva Viagem / Navegante rechargeable card for Lisbon metro, trams, and ferries.",
        }

    if any(k in dest_lower for k in ("france", "paris")):
        return {
            "country": "France",
            "emergency_numbers": {
                "European Emergency": "112",
                "Medical Ambulance (SAMU)": "15",
                "Police Emergency": "17",
            },
            "tipping_culture": "Service compris is included by law (15%). Leaving 1–2 EUR in cash for friendly café/bistro service is polite.",
            "cultural_etiquette": [
                "Always say 'Bonjour Madame/Monsieur' when entering shops or restaurants before asking a question.",
                "Keep metro tickets or Navigo passes handy until exiting turnstiles; transit inspectors check frequently.",
            ],
            "transit_card_recommendation": "Navigo Easy card or digital phone pass for Paris metro & RER network.",
        }

    # Standard international fallback
    return {
        "country": destination,
        "emergency_numbers": {
            "Universal Emergency Number": "112 or 911",
            "Local Police": "112 / 911",
            "Medical Helpline": "112 / 911",
        },
        "tipping_culture": "Check local restaurant bills for service charges; 10% is customary for attentive hospitality service.",
        "cultural_etiquette": [
            "Keep digital and offline physical photocopies of passport and visa documents.",
            "Respect dress codes at religious monuments (cover shoulders and knees).",
            "Be mindful of personal belongings in crowded transit terminals and tourist plazas.",
        ],
        "transit_card_recommendation": "Tap-to-pay contactless debit/credit card or local metro smart card for transit.",
    }


# ─── Tool 4: Weather-Aware Smart Packing Checklist ────────────────────────────
@server.tool()
def generate_smart_packing_list(
    destination: str,
    weather_condition: str = "Fair",
    duration_days: int = 4,
    interests_json: str = "[]",
) -> list[str]:
    """Generate a weather-informed, destination-curated packing checklist.
    
    Dynamically includes rain gear, walking shoes, power adapters, and specialized items
    based on actual forecasted conditions and trip duration.
    """
    try:
        interests = json.loads(interests_json) if isinstance(interests_json, str) else (interests_json or [])
    except Exception:
        interests = []

    cond_lower = (weather_condition or "").lower()
    dest_lower = destination.lower()

    items = [
        "Passport / Photo ID and digital visa copies",
        f"Comfortable walking shoes (broken-in for {duration_days} days of transit)",
        "Compact 10,000mAh portable power bank",
    ]

    # Destination-specific plug adapter
    if any(k in dest_lower for k in ("japan", "us", "usa", "america")):
        items.append("Type A/B universal plug adapter (100–120V)")
    elif any(k in dest_lower for k in ("uk", "london", "scotland")):
        items.append("Type G plug adapter (3-pin rectangular)")
    elif any(k in dest_lower for k in ("europe", "portugal", "france", "italy", "spain", "germany")):
        items.append("Type C/F Europlug adapter (2-pin round)")
    elif any(k in dest_lower for k in ("india", "delhi", "mumbai", "jaipur")):
        items.append("Type D/M plug adapter (3-pin round) & mosquito repellent wipes")
    else:
        items.append("Universal international travel power adapter")

    # Weather-informed clothing
    if any(r in cond_lower for r in ("rain", "shower", "drizzle", "storm")):
        items.extend([
            "Compact windproof travel umbrella",
            "Waterproof lightweight rain shell jacket",
            "Quick-drying extra socks",
        ])
    elif any(s in cond_lower for s in ("sun", "clear", "hot", "warm")):
        items.extend([
            "UV sunglasses and broad-spectrum sunscreen (SPF 50)",
            "Breathable lightweight cotton / linen tops",
            "Reusable insulated water bottle",
        ])
    elif any(c in cond_lower for c in ("cold", "chill", "snow", "frost", "winter")):
        items.extend([
            "Thermal base layers (top and bottom)",
            "Insulated packable down jacket",
            "Fleece beanie and touchscreen-compatible gloves",
        ])
    else:
        items.extend([
            "Light cardigan or jacket for evening temperature drops",
            "Versatile smart-casual layers",
        ])

    # Interest-specific gear
    int_str = " ".join(str(i).lower() for i in interests)
    if "temple" in int_str or "history" in int_str or "culture" in int_str or "yoga" in int_str:
        items.append("Slip-on shoes for temple floors and modest shoulder/knee covering scarf")
    if "hike" in int_str or "nature" in int_str or "adventure" in int_str:
        items.append("Trail-ready shoes with good traction & compact daypack")
    if "food" in int_str or "dining" in int_str:
        items.append("Digestive enzymes / antacid tablets for street food adventures")

    return items


# ─── Tool 5: Currency Conversion & Cash/Card Economics ────────────────────────
@server.tool()
def convert_and_budget_currency(
    amount: float,
    base_currency: str,
    destination: str,
) -> dict[str, Any]:
    """Convert budget and provide local currency guidelines and cash vs card recommendations."""
    dest_lower = destination.lower()
    base_upper = base_currency.upper()

    # Currency heuristics and approximate peg
    if any(k in dest_lower for k in ("japan", "tokyo", "kyoto", "osaka")):
        local_curr = "JPY"
        rate_from_usd = 152.0
        cash_preferred = True
        advice = "Japan is remarkably card-friendly in hotels and department stores, but cash (1,000 and 10,000 JPY notes) is essential for shrines, temples, local noodle shops, and vending machines. 7-Eleven ATMs accept international cards."
    elif any(k in dest_lower for k in ("india", "delhi", "mumbai", "jaipur", "rishikesh", "goa")):
        local_curr = "INR"
        rate_from_usd = 86.5
        cash_preferred = False
        advice = "UPI (QR code scanning) and Visa/Mastercard credit cards dominate dining and retail. Carry small INR 100/200/500 notes for street markets, auto-rickshaws, and monument shoe-keeping counters."
    elif any(k in dest_lower for k in ("portugal", "france", "italy", "spain", "germany", "europe")):
        local_curr = "EUR"
        rate_from_usd = 0.92
        cash_preferred = False
        advice = "Contactless Visa and Mastercard credit/debit cards are accepted everywhere, even for small café purchases. Carry 20–50 EUR in pocket cash for public restrooms and small gratuities."
    elif any(k in dest_lower for k in ("uk", "london", "scotland")):
        local_curr = "GBP"
        rate_from_usd = 0.78
        cash_preferred = False
        advice = "The UK is virtually cashless. Contactless cards and Apple Pay / Google Pay work across the Underground, black cabs, pubs, and markets."
    else:
        local_curr = "USD"
        rate_from_usd = 1.0
        cash_preferred = False
        advice = "Major credit cards with no foreign transaction fees are recommended; carry a small amount of local currency for taxis and incidentals."

    # Convert assuming USD base or approximate cross-rate
    est_rate = rate_from_usd
    if base_upper == "EUR":
        est_rate = rate_from_usd / 0.92
    elif base_upper == "GBP":
        est_rate = rate_from_usd / 0.78
    elif base_upper == "INR":
        est_rate = rate_from_usd / 86.5

    converted_amount = round(amount * est_rate, 2)

    return {
        "base_currency": base_upper,
        "base_amount": amount,
        "target_currency": local_curr,
        "estimated_local_amount": converted_amount,
        "exchange_rate": round(est_rate, 4),
        "cash_vs_card_advice": advice,
        "cash_preferred": cash_preferred,
    }


if __name__ == "__main__":
    import asyncio

    # Run as a standard stdio MCP server for external clients (Claude Desktop, Cursor, or MCP inspector)
    asyncio.run(server.run_stdio_async())
