"""Application settings, loaded from environment / .env via pydantic-settings.

Centralising config here means the rest of the app never touches os.environ
directly, and swapping providers (or flipping to stubs for tests) is a
one-line change.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_VERCEL_CHECKPOINT = "/tmp/wayfarer-checkpoints.sqlite"


def prepare_checkpoint_path(path: str) -> str:
    """Ensure the SQLite parent directory exists (required on Vercel /tmp)."""
    if path == ":memory:":
        return path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore"
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
    # On Vercel the project filesystem is read-only; use /tmp (see validator).
    checkpoint_db: str = "checkpoints.sqlite"

    # When true, the LLM and search calls are replaced with deterministic
    # stubs. Lets the e2e test (and offline demos) run with zero API keys.
    use_stubs: bool = False

    # When true, POST /plan and /review return immediately and the graph runs
    # in the background. Disabled automatically on Vercel (serverless).
    graph_background: bool = True

    @model_validator(mode="after")
    def _normalize_checkpoint_db(self) -> "Settings":
        if os.environ.get("VERCEL"):
            db = (self.checkpoint_db or "").strip()
            if not db.startswith("/tmp/") and db != ":memory:":
                self.checkpoint_db = _VERCEL_CHECKPOINT
        prepare_checkpoint_path(self.checkpoint_db)
        return self


def graph_runs_in_background() -> bool:
    """Background asyncio tasks die when a Vercel function returns."""
    if os.environ.get("VERCEL"):
        return False
    return get_settings().graph_background


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()


def stubs_enabled() -> bool:
    """Stubs are test-only — never used when running the real server."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return get_settings().use_stubs
    return False
