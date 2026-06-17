"""Web search client wrapping Tavily.

Tavily over Serper/Exa on purpose: it returns LLM-ready summarised snippets
rather than raw HTML, which cuts token usage and shrinks the prompt-injection
surface. Same swappable-stub pattern as the LLM client.
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings


class TavilyClient:
    def __init__(self) -> None:
        settings = get_settings()
        from tavily import TavilyClient as _Tavily

        self._client = _Tavily(api_key=settings.tavily_api_key)

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        # tavily-python is sync; run it without blocking the event loop.
        import anyio

        def _call() -> dict[str, Any]:
            return self._client.search(
                query=query, max_results=max_results, search_depth="basic"
            )

        raw = await anyio.to_thread.run_sync(_call)
        return [
            {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
            for r in raw.get("results", [])
        ]


class StubSearch:
    """Deterministic search results for offline / test runs."""

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        return [
            {
                "title": f"Top things to do — {query}",
                "url": "https://example.com/guide",
                "content": (
                    "Popular attractions include the old town, a famous food "
                    "market, riverside parks, and several museums. Best visited "
                    "in spring and autumn."
                ),
            }
        ][:max_results]


def get_search() -> TavilyClient | StubSearch:
    if get_settings().use_stubs:
        return StubSearch()
    return TavilyClient()
