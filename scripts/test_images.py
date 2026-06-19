"""Quick test for activity image fetching (Wiki + optional Google CSE)."""
from __future__ import annotations

import asyncio
import sys

from app.tools import fetch_activity_image


async def main() -> int:
    samples = [
        ("Delhi, India", {"title": "Red Fort", "location_name": "Red Fort", "image_keyword": "fort"}),
        ("Paris, France", {"title": "Eiffel Tower", "location_name": "Eiffel Tower", "image_keyword": "tower"}),
        ("Kyoto, Japan", {"title": "Fushimi Inari", "location_name": "Fushimi Inari Shrine", "image_keyword": "shrine"}),
    ]
    found = 0
    for dest, act in samples:
        act = dict(act)
        url = await fetch_activity_image(dest, act)
        src = act.get("image_source", "-")
        conf = act.get("image_confidence", 0)
        status = "OK" if url else "MISS"
        if url:
            found += 1
        print(f"[{status}] {act['title']} ({dest}) | source={src} conf={conf}")
        if url:
            print(f"       {url[:90]}...")
    print(f"\n{found}/{len(samples)} locations got images")
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
