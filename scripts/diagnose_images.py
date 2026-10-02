"""Diagnose image API responses (Wikipedia REST & Wikimedia Commons)."""
from __future__ import annotations

import asyncio
import sys

from app.tools import _wikipedia_rest_search, _wikimedia_thumbnail


async def main() -> int:
    print("=== Diagnosing Wikipedia & Wikimedia Image Resolution ===")

    wiki = await _wikipedia_rest_search(
        "Red Fort Delhi",
        activity_title="Red Fort",
        location="Red Fort",
        destination="Delhi, India",
    )
    print(f"\nWikipedia REST: {'hit' if wiki else 'miss'}")
    if wiki:
        print(f"  url={wiki[0][:80]}... conf={wiki[1]} source={wiki[2]}")

    media = await _wikimedia_thumbnail(
        "Red Fort Delhi",
        activity_title="Red Fort",
        location="Red Fort",
        destination="Delhi, India",
    )
    print(f"\nWikimedia Commons: {'hit' if media else 'miss'}")
    if media:
        print(f"  url={media[0][:80]}... conf={media[1]} source={media[2]}")

    return 0 if (wiki or media) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
