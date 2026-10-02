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
    gemini_model: str = "gemini-3.8-flash"

    # Web search (Tavily)
    tavily_api_key: str = ""

    # Routing (OpenRouteService) — https://openrouteservice.org/dev/
    openrouteservice_api_key: str = ""

    # SerpAPI — optional flight & hotel price discovery (Google Flights / Google Hotels)
    serpapi_key: str = ""
    
    # CARTO Cloud Platform & Spatial Intelligence (Primary Enterprise Provider)
    carto_api_key: str = ""
    carto_base_url: str = "https://gcp-us-east1.api.carto.com"
    carto_account_id: str = "ac_5v2ld53m"
    carto_mcp_url: str = "https://gcp-us-east1.api.carto.com/mcp/ac_5v2ld53m"
    carto_routing_enabled: bool = True
    carto_geocoding_enabled: bool = True

    # LangSmith Observability & Tracing (optional, zero-overhead if unset)
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "wayfarer-travel-planner"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # RAG (Retrieval-Augmented Generation) & Knowledge Base
    rag_enabled: bool = True
    rag_top_k: int = 5

    # MCP (Model Context Protocol) Integration
    mcp_enabled: bool = True

    # Supabase PostgreSQL & Auth
    database_url: str = ""
    jwt_secret: str = "wayfarer-enterprise-secret-key-change-in-prod-2026"
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 168

    # Logging & Runtime
    log_level: str = "INFO"
    is_vercel: bool = False

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
            self.is_vercel = True
            db = (self.checkpoint_db or "").strip()
            if not db.startswith("/tmp/") and db != ":memory:":
                self.checkpoint_db = _VERCEL_CHECKPOINT
        prepare_checkpoint_path(self.checkpoint_db)

        # Sync LangSmith telemetry env variables if configured
        if self.langsmith_api_key or self.langsmith_tracing:
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            if self.langsmith_api_key:
                os.environ["LANGCHAIN_API_KEY"] = self.langsmith_api_key
            if self.langsmith_project:
                os.environ["LANGCHAIN_PROJECT"] = self.langsmith_project
            if self.langsmith_endpoint:
                os.environ["LANGCHAIN_ENDPOINT"] = self.langsmith_endpoint

        return self

    def masked_key(self, key_name: str) -> str:
        """Safely display status of secrets without revealing secret material."""
        val = getattr(self, key_name, "")
        if not val:
            return "UNSET"
        if len(val) <= 8:
            return "****"
        return f"{val[:3]}...{val[-4:]}"


def graph_runs_in_background() -> bool:
    """Background asyncio tasks die when a Vercel function returns."""
    settings = get_settings()
    if settings.is_vercel or os.environ.get("VERCEL"):
        return False
    return settings.graph_background


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()


def stubs_enabled() -> bool:
    """Stubs are test-only — never used when running the real server."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        if os.environ.get("USE_STUBS") == "true":
            return True
        return get_settings().use_stubs
    return False
