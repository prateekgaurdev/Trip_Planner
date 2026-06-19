"""Web search client wrapping Tavily.

Tavily over Serper/Exa on purpose: it returns LLM-ready summarised snippets
rather than raw HTML, which cuts token usage and shrinks the prompt-injection
surface. Same swappable-stub pattern as the LLM client.
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings, stubs_enabled


class TavilyClient:
    def __init__(self) -> None:
        settings = get_settings()
        from tavily import TavilyClient as _Tavily

        self._client = _Tavily(api_key=settings.tavily_api_key)

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        from app.pipeline_log import api_end, api_start

        t0 = api_start("Tavily", "search", query=query[:80], max_results=max_results)
        # tavily-python is sync; run it without blocking the event loop.
        import anyio

        def _call() -> dict[str, Any]:
            return self._client.search(
                query=query, max_results=max_results, search_depth="basic"
            )

        try:
            raw = await anyio.to_thread.run_sync(_call)
            results = [
                {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
                for r in raw.get("results", [])
            ]
            api_end("Tavily", "search", t0, results=len(results))
            return results
        except Exception as exc:
            api_end("Tavily", "search", t0, ok=False, error=str(exc)[:120])
            raise


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
    if stubs_enabled():
        return StubSearch()
    return TavilyClient()
