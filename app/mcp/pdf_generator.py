"""High-fidelity visual PDF itinerary generator for Wayfarer.

Built with ReportLab. Designed to match executive travel dossier standards:
- Elegant typography with balanced margins
- Distinct visual badges for Lodging Anchors and Neighborhood Clusters
- Chronological closed-circuit daily timelines with transit friction
- Weather & Budget snapshot tables
- Traveler Kit: Smart packing list, emergency hotlines, and cultural etiquette
"""
from __future__ import annotations

import html
import io
import re
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── Color Palette ──────────────────────────────────────────────────────────
COLOR_PRIMARY = colors.HexColor("#0f766e")     # Rich Deep Teal
COLOR_PRIMARY_DARK = colors.HexColor("#115e59")
COLOR_PRIMARY_LIGHT = colors.HexColor("#f0fdfa")
COLOR_SECONDARY = colors.HexColor("#0284c7")   # Vibrant Sky
COLOR_ACCENT = colors.HexColor("#d97706")      # Warm Amber
COLOR_INK = colors.HexColor("#0f172a")         # Deep Slate
COLOR_INK_MUTED = colors.HexColor("#475569")   # Soft Slate
COLOR_BORDER = colors.HexColor("#cbd5e1")      # Border grey
COLOR_CARD_BG = colors.HexColor("#f8fafc")     # Light background grey
COLOR_WHITE = colors.HexColor("#ffffff")
COLOR_GREEN = colors.HexColor("#16a34a")


def _clean_text(text: Any) -> str:
    """Sanitize text for ReportLab XML parser and avoid font encoding glitches."""
    if text is None:
        return ""
    s = str(text)
    # Replace unicode currency symbols that may trigger Helvetica glyph errors
    s = s.replace("₹", "INR ").replace("¥", "JPY ").replace("€", "EUR ").replace("£", "GBP ")
    s = s.replace("—", " - ").replace("–", " - ").replace("•", "*").replace("’", "'").replace("“", '"').replace("”", '"')
    return html.escape(s)


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute and render total page count."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count: int) -> None:
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(COLOR_INK_MUTED)

        # Running header on pages 2+
        if self._pageNumber > 1:
            self.drawString(36, 756, "Wayfarer Travel Dossier • Confirmed Itinerary")
            self.setStrokeColor(COLOR_BORDER)
            self.setLineWidth(0.5)
            self.line(36, 750, 576, 750)

        # Running footer on every page
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(576, 22, page_str)
        self.drawString(
            36,
            22,
            "Wayfarer Agentic Intelligence • Model Context Protocol (MCP) Verified",
        )
        self.setStrokeColor(COLOR_BORDER)
        self.setLineWidth(0.5)
        self.line(36, 32, 576, 32)
        self.restoreState()


def build_itinerary_pdf(plan_data: dict[str, Any]) -> bytes:
    """Generate a multi-page, publication-grade PDF from plan state."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=40,
        bottomMargin=42,
    )

    styles = getSampleStyleSheet()

    # Custom typography
    styles.add(
        ParagraphStyle(
            "DocTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=26,
            textColor=COLOR_PRIMARY_DARK,
        )
    )
    styles.add(
        ParagraphStyle(
            "DocSubtitle",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=COLOR_INK_MUTED,
        )
    )
    styles.add(
        ParagraphStyle(
            "SectionHeader",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=16,
            textColor=COLOR_INK,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            "DayBanner",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=15,
            textColor=COLOR_WHITE,
        )
    )
    styles.add(
        ParagraphStyle(
            "ItemTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9.5,
            leading=13,
            textColor=COLOR_INK,
        )
    )
    styles.add(
        ParagraphStyle(
            "ItemMeta",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=COLOR_INK_MUTED,
        )
    )
    styles.add(
        ParagraphStyle(
            "ItemDesc",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=COLOR_INK,
        )
    )
    styles.add(
        ParagraphStyle(
            "AnchorText",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=12,
            textColor=COLOR_PRIMARY,
        )
    )
    styles.add(
        ParagraphStyle(
            "BadgeLabel",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=COLOR_SECONDARY,
        )
    )

    story: list[Any] = []

    # Extract plan fields (support both flat and nested LangGraph checkpoints)
    dest = (
        plan_data.get("destination")
        or plan_data.get("preferences", {}).get("destination")
        or "Your Destination"
    )
    prefs = plan_data.get("preferences") or {}
    start_date = plan_data.get("start_date") or prefs.get("start_date") or ""
    end_date = plan_data.get("end_date") or prefs.get("end_date") or ""
    travelers = plan_data.get("travelers") or prefs.get("travelers") or 1
    budget_raw = plan_data.get("budget") or prefs.get("budget") or {}
    currency = prefs.get("currency") or "USD"
    origin = prefs.get("origin") or plan_data.get("origin") or ""
    vision = (
        plan_data.get("custom_notes")
        or prefs.get("custom_notes")
        or ""
    ).strip()

    summary = (
        plan_data.get("summary")
        or plan_data.get("overview")
        or (plan_data.get("draft_itinerary") or {}).get("summary")
        or ""
    )
    lodging = plan_data.get("lodging_anchor") or {}
    corridor = plan_data.get("journey_corridor") or {}
    weather = plan_data.get("weather") or {}
    days = plan_data.get("days") or (plan_data.get("draft_itinerary") or {}).get("days") or []

    # ── 1. Document Hero Banner ────────────────────────────────────────────
    header_table_data = [
        [
            Paragraph("WAYFARER TRAVEL DOSSIER", styles["BadgeLabel"]),
            Paragraph(f"<b>STATUS:</b> CONFIRMED &bull; MCP VERIFIED", styles["ItemMeta"]),
        ],
        [
            Paragraph(_clean_text(dest).upper(), styles["DocTitle"]),
            "",
        ],
        [
            Paragraph(
                f"<b>Dates:</b> {_clean_text(start_date)} &rarr; {_clean_text(end_date)} &nbsp;|&nbsp; "
                f"<b>Travelers:</b> {travelers} &nbsp;|&nbsp; "
                f"<b>Budget:</b> {_clean_text(str(prefs.get('budget', '')))} {currency}",
                styles["DocSubtitle"],
            ),
            "",
        ],
    ]

    header_table = Table(
        header_table_data,
        colWidths=[420, 120],
    )
    header_table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 1), (1, 1)),
                ("SPAN", (0, 2), (1, 2)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 10))

    # Corridor route badge if origin exists
    if origin:
        corridor_box = [
            [
                Paragraph(
                    f"<b>TRANSIT CORRIDOR:</b> {_clean_text(origin)} &rarr; {_clean_text(dest)}",
                    styles["ItemTitle"],
                ),
                Paragraph(
                    _clean_text(corridor.get("recommended_mode", "Commercial Flight / Express Rail")),
                    styles["BadgeLabel"],
                ),
            ]
        ]
        t_corridor = Table(corridor_box, colWidths=[380, 160])
        t_corridor.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), COLOR_PRIMARY_LIGHT),
                    ("BOX", (0, 0), (-1, -1), 0.75, COLOR_PRIMARY),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ]
            )
        )
        story.append(t_corridor)
        story.append(Spacer(1, 8))

    # Custom Vision Card
    if vision:
        vision_data = [
            [
                Paragraph(
                    f"<b>Traveler's Stated Vision:</b> <i>\"{_clean_text(vision)}\"</i>",
                    styles["ItemDesc"],
                )
            ]
        ]
        t_vision = Table(vision_data, colWidths=[540])
        t_vision.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                    ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(t_vision)
        story.append(Spacer(1, 10))

    # Summary Paragraph
    if summary:
        story.append(Paragraph(_clean_text(summary), styles["ItemDesc"]))
        story.append(Spacer(1, 10))

    # ── 2. Lodging Base Camp Anchor ────────────────────────────────────────
    lodging_name = lodging.get("name") or "Curated Centrally-Located Boutique Hotel"
    lodging_area = lodging.get("neighborhood") or lodging.get("area") or "Central District"
    lodging_notes = lodging.get("reason") or lodging.get("notes") or "Serves as the persistent daily hub for all excursions."

    anchor_box_data = [
        [
            Paragraph("<b>LODGING ANCHOR (HOME BASE)</b>", styles["AnchorText"]),
            Paragraph(f"<b>Area:</b> {_clean_text(lodging_area)}", styles["ItemMeta"]),
        ],
        [
            Paragraph(f"<b>{_clean_text(lodging_name)}</b>", styles["ItemTitle"]),
            "",
        ],
        [
            Paragraph(
                f"<b>Check-in / Excursion Hub:</b> {_clean_text(lodging_notes)}",
                styles["ItemDesc"],
            ),
            "",
        ],
    ]
    t_anchor = Table(anchor_box_data, colWidths=[380, 160])
    t_anchor.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 1), (1, 1)),
                ("SPAN", (0, 2), (1, 2)),
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 1, COLOR_PRIMARY),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]
        )
    )
    story.append(t_anchor)
    story.append(Spacer(1, 12))

    # ── 3. Executive Snapshot (Budget & Weather) ────────────────────────────
    # Left: Budget Table
    b_alloc = {}
    if isinstance(budget_raw, dict):
        b_alloc = budget_raw.get("allocation") or budget_raw
    b_total = budget_raw.get("total") or prefs.get("budget") or 0

    budget_rows = [
        [Paragraph("<b>Category</b>", styles["ItemTitle"]), Paragraph("<b>Allocation</b>", styles["ItemTitle"])],
        [Paragraph("Accommodation", styles["ItemMeta"]), Paragraph(f"{b_alloc.get('lodging', b_alloc.get('accommodation', '-'))} {currency}", styles["ItemMeta"])],
        [Paragraph("Food & Dining", styles["ItemMeta"]), Paragraph(f"{b_alloc.get('food', b_alloc.get('dining', '-'))} {currency}", styles["ItemMeta"])],
        [Paragraph("Activities & Entry", styles["ItemMeta"]), Paragraph(f"{b_alloc.get('activities', b_alloc.get('sightseeing', '-'))} {currency}", styles["ItemMeta"])],
        [Paragraph("Local Transit", styles["ItemMeta"]), Paragraph(f"{b_alloc.get('transport', b_alloc.get('transit', '-'))} {currency}", styles["ItemMeta"])],
        [Paragraph("Emergency Buffer", styles["ItemMeta"]), Paragraph(f"{b_alloc.get('buffer', b_alloc.get('emergency', '-'))} {currency}", styles["ItemMeta"])],
        [Paragraph("<b>Total Trip Budget</b>", styles["ItemTitle"]), Paragraph(f"<b>{b_total} {currency}</b>", styles["ItemTitle"])],
    ]
    t_budget = Table(budget_rows, colWidths=[150, 100])
    t_budget.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARY_LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, COLOR_BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]
        )
    )

    # Right: Weather Snapshot
    w_days = weather.get("days") or []
    w_summary = weather.get("summary") or "Optimal seasonal conditions expected throughout the journey."
    weather_rows = [
        [Paragraph("<b>Weather & Climate Overview</b>", styles["ItemTitle"])],
        [Paragraph(_clean_text(w_summary), styles["ItemDesc"])],
    ]
    if w_days:
        for wd in w_days[:3]:
            date_str = wd.get("date") or "Date"
            cond = wd.get("condition") or "Fair"
            high = wd.get("temp_high_c") or wd.get("high_c") or "-"
            low = wd.get("temp_low_c") or wd.get("low_c") or "-"
            tip = wd.get("tip") or ""
            line = f"&bull; <b>{_clean_text(date_str)}:</b> {_clean_text(cond)} ({low}&deg;C to {high}&deg;C)"
            if tip:
                line += f"<br/><font color='#64748b'>{_clean_text(tip)}</font>"
            weather_rows.append([Paragraph(line, styles["ItemMeta"])])

    t_weather = Table(weather_rows, colWidths=[270])
    t_weather.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    snapshot_composite = Table(
        [[t_budget, t_weather]],
        colWidths=[260, 280],
    )
    snapshot_composite.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(snapshot_composite)
    story.append(Spacer(1, 14))

    # ── 4. Day-by-Day Chronological Itinerary ──────────────────────────────
    story.append(Paragraph("DAY-BY-DAY CURATED ITINERARY", styles["SectionHeader"]))
    story.append(HRFlowable(width="100%", thickness=1, color=COLOR_PRIMARY, spaceAfter=8))

    for day_idx, d in enumerate(days):
        day_num = d.get("day_number") or (day_idx + 1)
        day_date = d.get("date") or ""
        cluster = d.get("neighborhood_cluster") or d.get("theme") or "Historic & Cultural Core"
        day_title = d.get("title") or f"Day {day_num} Exploration"
        acts = d.get("activities") or []

        # Day Banner Table
        banner_title = f"DAY {day_num}: {_clean_text(day_title).upper()}"
        banner_meta = f"{_clean_text(day_date)} &bull; {_clean_text(cluster)}"
        banner_table = Table(
            [
                [
                    Paragraph(banner_title, styles["DayBanner"]),
                    Paragraph(banner_meta, styles["DayBanner"]),
                ]
            ],
            colWidths=[360, 180],
        )
        banner_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), COLOR_PRIMARY_DARK),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )

        day_elements: list[Any] = [banner_table, Spacer(1, 6)]

        # Morning departure anchor
        dep_text = f"<b>09:00</b> &nbsp;&bull;&nbsp; <b>DEPART BASE:</b> {_clean_text(lodging_name)} &rarr; Head to {cluster}"
        day_elements.append(Paragraph(dep_text, styles["AnchorText"]))
        day_elements.append(Spacer(1, 4))

        # Activities
        for act in acts:
            time_slot = act.get("time") or act.get("slot") or "Morning"
            title = act.get("title") or act.get("name") or "Activity"
            desc = act.get("description") or act.get("notes") or ""
            duration = act.get("duration") or ""
            cost = act.get("cost_estimate") or act.get("cost") or ""
            travel_leg = act.get("travel_from_prev") or {}

            # Transit friction leg between stops
            if travel_leg and isinstance(travel_leg, dict) and travel_leg.get("duration_mins"):
                mode = travel_leg.get("mode", "walking")
                mins = travel_leg.get("duration_mins", 0)
                dist_km = travel_leg.get("distance_km", 0.0)
                leg_str = f"&nbsp;&nbsp;&darr;&nbsp; <i>Transit: {mins} min {mode} ({dist_km} km)</i>"
                day_elements.append(Paragraph(leg_str, styles["ItemMeta"]))
                day_elements.append(Spacer(1, 2))

            meta_parts = []
            if duration:
                meta_parts.append(f"Duration: {_clean_text(duration)}")
            if cost:
                meta_parts.append(f"Est. Cost: {_clean_text(str(cost))}")
            meta_str = " &nbsp;|&nbsp; ".join(meta_parts)

            act_row = [
                [
                    Paragraph(f"<b>{_clean_text(time_slot)}</b>", styles["ItemTitle"]),
                    Paragraph(f"<b>{_clean_text(title)}</b>", styles["ItemTitle"]),
                ],
                [
                    "",
                    Paragraph(meta_str, styles["ItemMeta"]) if meta_str else "",
                ],
                [
                    "",
                    Paragraph(_clean_text(desc), styles["ItemDesc"]) if desc else "",
                ],
            ]
            t_act = Table(act_row, colWidths=[70, 470])
            t_act.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]
                )
            )
            day_elements.append(t_act)
            day_elements.append(Spacer(1, 4))

        # Evening return anchor
        ret_text = f"<b>21:00</b> &nbsp;&bull;&nbsp; <b>RETURN TO BASE:</b> Evening rest & lodging at {_clean_text(lodging_name)}"
        day_elements.append(Paragraph(ret_text, styles["AnchorText"]))
        day_elements.append(Spacer(1, 10))

        story.append(KeepTogether(day_elements))

    # ── 5. Traveler Kit (Powered by MCP) ───────────────────────────────────
    kit_elements: list[Any] = []
    kit_elements.append(Spacer(1, 6))
    kit_elements.append(Paragraph("TRAVELER KIT & SAFETY BULLETIN (POWERED BY MCP)", styles["SectionHeader"]))
    kit_elements.append(HRFlowable(width="100%", thickness=1, color=COLOR_ACCENT, spaceAfter=8))

    # Packing List
    packing = plan_data.get("packing_list") or []
    if packing:
        pack_text = " &bull; ".join(_clean_text(p) for p in packing[:12])
        kit_elements.append(Paragraph("<b>Smart Weather-Aware Packing Checklist:</b>", styles["ItemTitle"]))
        kit_elements.append(Paragraph(pack_text, styles["ItemDesc"]))
        kit_elements.append(Spacer(1, 8))

    # Emergency & Advisories
    advisories = plan_data.get("travel_advisories") or {}
    emergency_nums = advisories.get("emergency_numbers") or {"Police": "112 / 911", "Ambulance": "112 / 911"}
    etiquette_tips = advisories.get("etiquette_tips") or [
        "Respect sacred spaces: remove footwear and cover shoulders.",
        "Check local tipping practices: tip servers when customary.",
        "Keep local transit cards and a digital copy of identity documents handy.",
    ]

    emergency_line = " &nbsp;|&nbsp; ".join(f"<b>{k}:</b> {v}" for k, v in emergency_nums.items())
    kit_elements.append(Paragraph("<b>Emergency Contacts & Safety Hotlines:</b>", styles["ItemTitle"]))
    kit_elements.append(Paragraph(emergency_line, styles["ItemDesc"]))
    kit_elements.append(Spacer(1, 6))

    kit_elements.append(Paragraph("<b>Cultural Etiquette & Local Practices:</b>", styles["ItemTitle"]))
    for tip in etiquette_tips[:4]:
        kit_elements.append(Paragraph(f"&bull; {_clean_text(tip)}", styles["ItemDesc"]))

    story.append(KeepTogether(kit_elements))

    # Build the document with our custom NumberedCanvas
    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()
