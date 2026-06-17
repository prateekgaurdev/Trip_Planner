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

    # LLM (Google Gemini)
    google_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Web search (Tavily)
    tavily_api_key: str = ""

    # Persistence — SQLite file backing LangGraph's checkpointer.
    # On Vercel, the root filesystem is read-only, so we use /tmp.
    # Note: /tmp is ephemeral. For production Vercel, use PostgresSaver.
    checkpoint_db: str = "/tmp/checkpoints.sqlite" if os.environ.get("VERCEL") else "checkpoints.sqlite"

    # When true, the LLM and search calls are replaced with deterministic
    # stubs. Lets the e2e test (and offline demos) run with zero API keys.
    use_stubs: bool = False


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()
