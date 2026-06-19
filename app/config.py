"""Application settings, loaded from environment / .env via pydantic-settings.

Centralising config here means the rest of the app never touches os.environ
directly, and swapping providers (or flipping to stubs for tests) is a
one-line change.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    google_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"

    # Google Programmable Search Engine ID (image search fallback)
    # Create at https://programmablesearchengine.google.com/ — enable image search
    google_cse_id: str = ""

    # Web search (Tavily)
    tavily_api_key: str = ""

    # Routing (OpenRouteService) — https://openrouteservice.org/dev/
    openrouteservice_api_key: str = ""

    # SerpAPI — optional flight & hotel price search
    serpapi_key: str = ""

    # Persistence — SQLite file backing LangGraph's checkpointer.
    # On Vercel, the root filesystem is read-only, so we use /tmp.
    # Note: /tmp is ephemeral. For production Vercel, use PostgresSaver.
    checkpoint_db: str = "/tmp/checkpoints.sqlite" if os.environ.get("VERCEL") else "checkpoints.sqlite"

    # When true, the LLM and search calls are replaced with deterministic
    # stubs. Lets the e2e test (and offline demos) run with zero API keys.
    use_stubs: bool = False

    # When true, POST /plan and /review return immediately and the graph runs
    # in the background. Set false in tests for deterministic inline runs.
    graph_background: bool = True


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()


def stubs_enabled() -> bool:
    """Stubs are test-only — never used when running the real server."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return get_settings().use_stubs
    return False
