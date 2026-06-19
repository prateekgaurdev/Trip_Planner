"""Diagnose image API responses (no secrets printed)."""
from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import get_settings
from app.tools import _google_image_search, _wikipedia_rest_search


async def main() -> int:
    s = get_settings()
    print(f"google_api_key: {'set' if s.google_api_key else 'MISSING'}")
    print(f"google_cse_id:  {'set' if s.google_cse_id else 'MISSING'}")

    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": s.google_api_key,
                "cx": s.google_cse_id,
                "q": "Red Fort Delhi",
                "searchType": "image",
                "num": 3,
            },
        )
        print(f"\nGoogle CSE HTTP: {r.status_code}")
        data = r.json()
        if "error" in data:
            err = data["error"]
            print(f"  error: {err.get('code')} — {err.get('message', '')[:200]}")
        else:
            items = data.get("items") or []
            print(f"  items returned: {len(items)}")
            for item in items[:3]:
                print(f"  - {(item.get('title') or '?')[:70]}")

    wiki = await _wikipedia_rest_search(
        "Red Fort Delhi",
        activity_title="Red Fort",
        location="Red Fort",
        destination="Delhi, India",
    )
    print(f"\nWikipedia REST: {'hit' if wiki else 'miss'}")
    if wiki:
        print(f"  conf={wiki[1]} source={wiki[2]}")

    google = await _google_image_search(
        "Red Fort Delhi landmark",
        activity_title="Red Fort",
        location="Red Fort",
        destination="Delhi, India",
    )
    print(f"Google (scored): {'hit' if google else 'miss'}")
    if google:
        print(f"  conf={google[1]} source={google[2]}")

    return 0 if (wiki or google) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
