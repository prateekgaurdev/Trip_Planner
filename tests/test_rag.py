"""Tests for the Travel Intelligence Hybrid RAG Engine (ChromaDB + BM25)."""
from __future__ import annotations

import pytest
from app.services.rag import ChromaHybridRAGEngine, get_rag_engine


def test_chroma_hybrid_rag_initialization():
    engine = get_rag_engine()
    assert engine._chroma_ready is True
    assert engine._collection is not None
    assert engine._collection.count() >= 1000


def test_hybrid_rag_query_india_destination():
    engine = get_rag_engine()
    # Query Delhi lodging & transit
    delhi_results = engine.retrieve("Delhi, India", "lodging hotel sleep area Connaught Place", top_k=3, category="lodging")
    assert len(delhi_results) > 0
    assert any("Delhi" in r["source"] or "Delhi" in r["content"] for r in delhi_results)
    assert delhi_results[0]["relevance_score"] > 0

    # Query Rishikesh attractions
    rishi_results = engine.retrieve("Rishikesh, India", "Ganga Aarti Parmarth Niketan temple yoga", top_k=3)
    assert len(rishi_results) > 0
    assert any("Rishikesh" in r["source"] or "Rishikesh" in r["content"] for r in rishi_results)


def test_hybrid_rag_query_usa_destination():
    engine = get_rag_engine()
    # Query NYC lodging
    nyc_results = engine.retrieve("New York City, USA", "Manhattan accommodations hotel stay", top_k=3, category="lodging")
    assert len(nyc_results) > 0
    assert any("New York" in r["source"] or "Manhattan" in r["content"] or "New York" in r["content"] for r in nyc_results)

    # Query San Francisco transit
    sf_results = engine.retrieve("San Francisco, USA", "cable car transit get around BART MUNI", top_k=3)
    assert len(sf_results) > 0
    assert any("San Francisco" in r["source"] or "San Francisco" in r["content"] for r in sf_results)


def test_rag_engine_live_search_ingestion():
    engine = get_rag_engine()
    mock_search = [
        {
            "title": "Secret Lisbon Viewpoints",
            "url": "https://example.com/lisbon-secrets",
            "content": (
                "Miradouro da Senhora do Monte offers the highest panorama in Lisbon. "
                "Visit at golden hour for stunning light over São Jorge castle and the Tagus river. "
                "Take a small tuk-tuk or walk up from Graça neighborhood."
            ),
        }
    ]
    added = engine.ingest_live_search("Lisbon, Portugal", mock_search)
    assert added >= 1

    # Search for the ingested viewpoint
    results = engine.retrieve("Lisbon, Portugal", "Senhora do Monte viewpoint panorama")
    assert len(results) > 0
    assert any("Senhora do Monte" in r["content"] for r in results)


def test_rag_lodging_anchor_recommendation():
    engine = get_rag_engine()
    anchor = engine.get_lodging_anchor_recommendation("Delhi, India", budget_tier="moderate")
    assert anchor is not None
    assert "name" in anchor
    assert "neighborhood" in anchor
    assert anchor["neighborhood"] != ""
    assert anchor["estimated_nightly_rate"] is not None


def test_rag_neighborhood_clusters():
    engine = get_rag_engine()
    clusters = engine.get_neighborhood_clusters("Delhi, India")
    assert len(clusters) >= 2
    assert all("name" in c and "vibe" in c for c in clusters)
