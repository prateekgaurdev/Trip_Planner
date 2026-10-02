"""Scrape and ingest comprehensive travel guides for India, US, and international hubs into ChromaDB.

Pulls clean travel intelligence from Wikivoyage APIs, segments sections into
semantic passages with domain metadata, and indexes them into ChromaDB's persistent
vector store for hybrid dense-sparse retrieval.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import chromadb
import httpx

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_JSON_DATA_PATH = _PROJECT_ROOT / "app" / "data" / "travel_knowledge.json"
_DEFAULT_CHROMA_DIR = "/tmp/wayfarer_chroma_db"
_COLLECTION_NAME = "wayfarer_travel_hybrid_rag"

DESTINATIONS = [
    # ── India ──────────────────────────────────────────
    {"destination": "Delhi, India", "city": "delhi", "country": "India", "page": "Delhi"},
    {"destination": "Mumbai, India", "city": "mumbai", "country": "India", "page": "Mumbai"},
    {"destination": "Jaipur, India", "city": "jaipur", "country": "India", "page": "Jaipur"},
    {"destination": "Rishikesh, India", "city": "rishikesh", "country": "India", "page": "Rishikesh"},
    {"destination": "Goa, India", "city": "goa", "country": "India", "page": "Goa"},
    {"destination": "Agra, India", "city": "agra", "country": "India", "page": "Agra"},
    {"destination": "Varanasi, India", "city": "varanasi", "country": "India", "page": "Varanasi"},
    {"destination": "Bengaluru, India", "city": "bengaluru", "country": "India", "page": "Bengaluru"},
    {"destination": "Kochi, India", "city": "kochi", "country": "India", "page": "Kochi"},

    # ── United States ──────────────────────────────────
    {"destination": "New York City, USA", "city": "new york", "country": "United States", "page": "New_York_City"},
    {"destination": "San Francisco, USA", "city": "san francisco", "country": "United States", "page": "San_Francisco"},
    {"destination": "Los Angeles, USA", "city": "los angeles", "country": "United States", "page": "Los_Angeles"},
    {"destination": "Chicago, USA", "city": "chicago", "country": "United States", "page": "Chicago"},
    {"destination": "Las Vegas, USA", "city": "las vegas", "country": "United States", "page": "Las_Vegas"},
    {"destination": "Washington D.C., USA", "city": "washington", "country": "United States", "page": "Washington,_D.C."},
    {"destination": "Miami, USA", "city": "miami", "country": "United States", "page": "Miami"},
    {"destination": "Honolulu, USA", "city": "honolulu", "country": "United States", "page": "Honolulu"},
    {"destination": "Seattle, USA", "city": "seattle", "country": "United States", "page": "Seattle"},

    # ── Global Hubs ────────────────────────────────────
    {"destination": "Lisbon, Portugal", "city": "lisbon", "country": "Portugal", "page": "Lisbon"},
    {"destination": "Kyoto, Japan", "city": "kyoto", "country": "Japan", "page": "Kyoto"},
    {"destination": "Paris, France", "city": "paris", "country": "France", "page": "Paris"},
    {"destination": "Tokyo, Japan", "city": "tokyo", "country": "Japan", "page": "Tokyo"},
    {"destination": "London, UK", "city": "london", "country": "United Kingdom", "page": "London"},
    {"destination": "Rome, Italy", "city": "rome", "country": "Italy", "page": "Rome"},
]


def clean_wikitext(text: str) -> str:
    """Strip wiki-style artifacts and clean whitespace."""
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Remove citation brackets like [1], [citation needed]
    text = re.sub(r"\[\d+\]", "", text)
    # Remove multiple spaces/newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def parse_wikivoyage_sections(raw_text: str) -> dict[str, str]:
    """Split Wikivoyage raw text into major sections."""
    lines = raw_text.split("\n")
    sections: dict[str, list[str]] = {"Overview": []}
    current_section = "Overview"

    section_header_pattern = re.compile(r"^==\s*([^=]+?)\s*==$")

    for line in lines:
        match = section_header_pattern.match(line.strip())
        if match:
            current_section = match.group(1).strip()
            sections[current_section] = []
        else:
            sections[current_section].append(line)

    return {k: clean_wikitext("\n".join(v)) for k, v in sections.items() if "\n".join(v).strip()}


def categorize_section(section_name: str) -> str:
    s = section_name.lower()
    if any(k in s for k in ("sleep", "lodging", "hotel", "accommodation", "where to stay")):
        return "lodging"
    if any(k in s for k in ("get around", "transit", "metro", "bus", "transport")):
        return "transit"
    if any(k in s for k in ("district", "neighborhood", "areas", "boroughs", "orientation")):
        return "neighborhood"
    if any(k in s for k in ("eat", "drink", "cuisine", "food", "restaurants")):
        return "dining"
    if any(k in s for k in ("see", "do", "attraction", "visit", "museum", "landmarks")):
        return "attraction"
    if any(k in s for k in ("understand", "history", "culture", "climate", "stay safe", "customs", "tips")):
        return "culture"
    return "overview"


def chunk_text(text: str, target_size: int = 380, overlap: int = 60) -> list[str]:
    """Split text into semantic paragraphs/sentences of ~350-450 characters."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []

    for para in paragraphs:
        para = para.strip()
        if not para or len(para) < 40:
            continue
        if len(para) <= target_size + 100:
            chunks.append(para)
        else:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf = ""
            for sentence in sentences:
                if len(buf) + len(sentence) < target_size:
                    buf += (" " if buf else "") + sentence
                else:
                    if buf:
                        chunks.append(buf.strip())
                    buf = sentence
            if buf and len(buf.strip()) > 30:
                chunks.append(buf.strip())
    return chunks


def fetch_destination_guide(http_client: httpx.Client, page: str) -> str:
    """Fetch raw guide text from Wikivoyage API with retry on 429."""
    url = "https://en.wikivoyage.org/w/api.php"
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "titles": page,
        "format": "json",
    }
    for attempt in range(4):
        try:
            res = http_client.get(url, params=params)
            if res.status_code == 429:
                wait_sec = 2.0 * (attempt + 1)
                print(f"   [Rate limit 429] Waiting {wait_sec:.1f}s before retry…")
                time.sleep(wait_sec)
                continue
            res.raise_for_status()
            data = res.json()
            pages = data.get("query", {}).get("pages", {})
            if not pages:
                return ""
            page_data = list(pages.values())[0]
            return page_data.get("extract", "")
        except Exception as exc:
            if attempt < 3:
                time.sleep(1.5)
                continue
            print(f"  Error fetching {page}: {exc}")
            return ""
    return ""


def main() -> None:
    import json
    print(f"=== Wayfarer Travel Intelligence Ingestion ===")
    print(f"Target ChromaDB directory: {_DEFAULT_CHROMA_DIR}")
    print(f"Target JSON data path: {_JSON_DATA_PATH}")

    os.makedirs(_DEFAULT_CHROMA_DIR, exist_ok=True)
    _JSON_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=_DEFAULT_CHROMA_DIR)
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    print(f"Connected to collection '{_COLLECTION_NAME}'. Existing documents: {collection.count()}")

    headers = {"User-Agent": "WayfarerTravelPlanner/1.0 (travel demonstration project)"}
    http_client = httpx.Client(timeout=25, headers=headers)

    all_ingested_records: list[dict[str, Any]] = []
    existing_destinations: set[str] = set()
    if _JSON_DATA_PATH.exists():
        try:
            with open(_JSON_DATA_PATH, "r", encoding="utf-8") as f:
                all_ingested_records = json.load(f)
            existing_destinations = set(r.get("destination") for r in all_ingested_records if r.get("destination"))
            print(f"Loaded {len(all_ingested_records)} existing records across {len(existing_destinations)} destinations.")
        except Exception as e:
            print(f"Note loading existing JSON: {e}")

    total_chunks_added = 0

    for item in DESTINATIONS:
        dest_name = item["destination"]
        page = item["page"]
        city = item["city"]
        country = item["country"]

        if dest_name in existing_destinations:
            print(f">> Destination '{dest_name}' already indexed, skipping.")
            continue

        print(f"\n>> Scraping Wikivoyage guide for: {dest_name} ({page})…")
        raw_text = fetch_destination_guide(http_client, page)
        if not raw_text:
            print(f"   [!] Failed or empty extract for {page}")
            continue

        print(f"   Fetched {len(raw_text):,} characters. Parsing sections…")
        sections = parse_wikivoyage_sections(raw_text)

        target_categories = {"neighborhood", "lodging", "transit", "culture", "dining", "attraction", "overview"}

        docs_to_add: list[str] = []
        ids_to_add: list[str] = []
        metas_to_add: list[dict[str, Any]] = []

        chunk_idx = 0
        for sec_name, sec_content in sections.items():
            category = categorize_section(sec_name)
            if category not in target_categories:
                continue

            chunks = chunk_text(sec_content)
            # Limit to top chunks per section to keep index high-signal
            selected_chunks = chunks[:12] if category in ("lodging", "neighborhood", "transit") else chunks[:6]

            for c in selected_chunks:
                doc_id = f"{city}_{category}_{chunk_idx}"
                docs_to_add.append(c)
                ids_to_add.append(doc_id)
                meta = {
                    "destination": dest_name,
                    "city": city,
                    "country": country,
                    "category": category,
                    "section": sec_name[:50],
                    "source": f"Wikivoyage: {dest_name} ({sec_name})",
                }
                metas_to_add.append(meta)
                all_ingested_records.append({
                    "id": doc_id,
                    "content": c,
                    **meta,
                })
                chunk_idx += 1

        if docs_to_add:
            print(f"   Upserting {len(docs_to_add)} chunks into ChromaDB…")
            for b_idx in range(0, len(docs_to_add), 100):
                b_docs = docs_to_add[b_idx : b_idx + 100]
                b_ids = ids_to_add[b_idx : b_idx + 100]
                b_metas = metas_to_add[b_idx : b_idx + 100]
                collection.upsert(ids=b_ids, documents=b_docs, metadatas=b_metas)
            total_chunks_added += len(docs_to_add)
            print(f"   [OK] Indexed {len(docs_to_add)} passages for {dest_name}")

        time.sleep(1.2)  # Politeness delay to prevent rate limiting

    # Save to JSON file as canonical versioned knowledge
    print(f"\nWriting {len(all_ingested_records)} records to {_JSON_DATA_PATH}…")
    with open(_JSON_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(all_ingested_records, f, indent=2, ensure_ascii=False)
    print(f"[OK] Saved travel knowledge dataset ({_JSON_DATA_PATH.stat().st_size / 1024:.1f} KB).")

    print(f"\n=== Ingestion Complete! ===")
    print(f"Total new chunks indexed: {total_chunks_added}")
    print(f"Total documents in ChromaDB collection: {collection.count()}")


if __name__ == "__main__":
    main()
