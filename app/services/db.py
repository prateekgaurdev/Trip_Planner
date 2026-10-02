"""Unified database access layer supporting Supabase PostgreSQL with SQLite fallback.

Features:
- Live Supabase connection pooling via psycopg 3 (with PgBouncer transaction mode compatibility)
- Automatic schema creation on startup (users, community_trips, reviews, likes)
- Resilient SQLite fallback for hermetic offline testing (USE_STUBS=true) and CI/CD
- Unified query execution with dictionary row mapping and JSON serialization
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.config import get_settings, stubs_enabled
from app.pipeline_log import api_end, api_start

logger = logging.getLogger("wayfarer.db")

_SQLITE_PATH = "community.sqlite"
_is_initialized = False
_init_lock = asyncio.Lock()


_psycopg_warning_logged = False

def is_postgres() -> bool:
    """Return True if a PostgreSQL DATABASE_URL is configured, stubs are not enabled, and psycopg is installed."""
    global _psycopg_warning_logged
    if stubs_enabled():
        return False
    url = (get_settings().database_url or "").strip()
    if not (url.startswith("postgresql://") or url.startswith("postgres://")):
        return False
        
    try:
        import psycopg
        return True
    except ImportError:
        if not _psycopg_warning_logged:
            logger.warning("DATABASE_URL is set but 'psycopg' is not installed. Falling back to SQLite.")
            _psycopg_warning_logged = True
        return False
    url = (get_settings().database_url or "").strip()
    if not (url.startswith("postgresql://") or url.startswith("postgres://")):
        return False
        
    try:
        import psycopg
        return True
    except ImportError:
        logger.warning("DATABASE_URL is set but 'psycopg' is not installed. Falling back to SQLite.")
        return False


@asynccontextmanager
async def get_connection() -> AsyncIterator[Any]:
    """Yield an active database connection (psycopg AsyncConnection or sqlite3 connection)."""
    settings = get_settings()
    if is_postgres():
        import psycopg
        from psycopg.rows import dict_row

        # prepare_threshold=None is mandatory for PgBouncer / Supabase transaction pooler
        conn = await psycopg.AsyncConnection.connect(
            settings.database_url,
            prepare_threshold=None,
            row_factory=dict_row,
            autocommit=True,
        )
        try:
            yield conn
        finally:
            await conn.close()
    else:
        # SQLite connection
        conn = sqlite3.connect(_SQLITE_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


async def init_db() -> None:
    """Idempotently create tables for users, community trips, reviews, and likes."""
    global _is_initialized
    if _is_initialized:
        return

    async with _init_lock:
        if _is_initialized:
            return

        t0 = api_start("DB", "init_schema", mode="postgres" if is_postgres() else "sqlite")
        async with get_connection() as conn:
            if is_postgres():
                async with conn.cursor() as cur:
                    await cur.execute("""
                        CREATE TABLE IF NOT EXISTS public.users (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            email VARCHAR(255) UNIQUE NOT NULL,
                            password_hash VARCHAR(255) NOT NULL,
                            full_name VARCHAR(100) NOT NULL,
                            avatar_url VARCHAR(500),
                            bio TEXT,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );

                        CREATE TABLE IF NOT EXISTS public.community_trips (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            user_id UUID REFERENCES public.users(id) ON DELETE SET NULL,
                            author_name VARCHAR(100) NOT NULL,
                            author_avatar VARCHAR(500),
                            destination VARCHAR(200) NOT NULL,
                            title VARCHAR(300) NOT NULL,
                            description TEXT,
                            origin VARCHAR(200),
                            duration_days INT DEFAULT 1,
                            budget NUMERIC(10, 2),
                            currency VARCHAR(10) DEFAULT 'USD',
                            travelers INT DEFAULT 1,
                            tags JSONB DEFAULT '[]'::jsonb,
                            itinerary_data JSONB NOT NULL,
                            likes_count INT DEFAULT 0,
                            reviews_count INT DEFAULT 0,
                            average_rating NUMERIC(3, 2) DEFAULT 5.0,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        );

                        CREATE TABLE IF NOT EXISTS public.community_trip_reviews (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            trip_id UUID REFERENCES public.community_trips(id) ON DELETE CASCADE,
                            user_id UUID REFERENCES public.users(id) ON DELETE SET NULL,
                            user_name VARCHAR(100) NOT NULL,
                            user_avatar VARCHAR(500),
                            rating INT NOT NULL CHECK (rating >= 1 AND rating <= 5),
                            liked_aspects TEXT NOT NULL,
                            suggested_additions TEXT NOT NULL,
                            comment TEXT,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );

                        CREATE TABLE IF NOT EXISTS public.community_trip_likes (
                            trip_id UUID REFERENCES public.community_trips(id) ON DELETE CASCADE,
                            user_id UUID REFERENCES public.users(id) ON DELETE CASCADE,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            PRIMARY KEY (trip_id, user_id)
                        );

                        CREATE INDEX IF NOT EXISTS idx_community_trips_destination ON public.community_trips(destination);
                        CREATE INDEX IF NOT EXISTS idx_community_trips_created ON public.community_trips(created_at DESC);
                        CREATE INDEX IF NOT EXISTS idx_trip_reviews_trip_id ON public.community_trip_reviews(trip_id);
                    """)
            else:
                # SQLite DDL
                cur = conn.cursor()
                cur.executescript("""
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        full_name TEXT NOT NULL,
                        avatar_url TEXT,
                        bio TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS community_trips (
                        id TEXT PRIMARY KEY,
                        user_id TEXT,
                        author_name TEXT NOT NULL,
                        author_avatar TEXT,
                        destination TEXT NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT,
                        origin TEXT,
                        duration_days INTEGER DEFAULT 1,
                        budget REAL,
                        currency TEXT DEFAULT 'USD',
                        travelers INTEGER DEFAULT 1,
                        tags TEXT DEFAULT '[]',
                        itinerary_data TEXT NOT NULL,
                        likes_count INTEGER DEFAULT 0,
                        reviews_count INTEGER DEFAULT 0,
                        average_rating REAL DEFAULT 5.0,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS community_trip_reviews (
                        id TEXT PRIMARY KEY,
                        trip_id TEXT NOT NULL,
                        user_id TEXT,
                        user_name TEXT NOT NULL,
                        user_avatar TEXT,
                        rating INTEGER NOT NULL,
                        liked_aspects TEXT NOT NULL,
                        suggested_additions TEXT NOT NULL,
                        comment TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS community_trip_likes (
                        trip_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (trip_id, user_id)
                    );
                """)
                conn.commit()

        _is_initialized = True
        api_end("DB", "init_schema", t0, ok=True)


def _convert_params_for_sqlite(query: str, params: tuple | list) -> tuple[str, list]:
    """Convert %s placeholders to ? for SQLite and serialize JSON/UUID parameters."""
    sqlite_query = query.replace("%s", "?")
    clean_params = []
    for p in params:
        if isinstance(p, (dict, list)):
            clean_params.append(json.dumps(p))
        elif isinstance(p, uuid.UUID):
            clean_params.append(str(p))
        else:
            clean_params.append(p)
    return sqlite_query, clean_params


def _convert_params_for_postgres(params: tuple | list) -> list:
    """Format dict/list parameters for PostgreSQL psycopg jsonb handling."""
    import psycopg
    clean_params = []
    for p in params:
        if isinstance(p, (dict, list)):
            clean_params.append(psycopg.types.json.Jsonb(p))
        else:
            clean_params.append(p)
    return clean_params


def _dict_from_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        d = dict(row)
    else:
        # sqlite3.Row
        d = dict(row)

    # Automatically parse JSON strings if needed
    for json_col in ("tags", "itinerary_data"):
        if json_col in d and isinstance(d[json_col], str):
            try:
                d[json_col] = json.loads(d[json_col])
            except Exception:
                pass
    return d


async def fetch_one(query: str, params: tuple | list = ()) -> dict[str, Any] | None:
    """Execute query and return the first row as a dictionary."""
    await init_db()
    async with get_connection() as conn:
        if is_postgres():
            clean_p = _convert_params_for_postgres(params)
            async with conn.cursor() as cur:
                await cur.execute(query, clean_p)
                row = await cur.fetchone()
                return _dict_from_row(row)
        else:
            q, p = _convert_params_for_sqlite(query, params)
            cur = conn.cursor()
            cur.execute(q, p)
            row = cur.fetchone()
            return _dict_from_row(row)


async def fetch_all(query: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    """Execute query and return all matching rows as dictionaries."""
    await init_db()
    async with get_connection() as conn:
        if is_postgres():
            clean_p = _convert_params_for_postgres(params)
            async with conn.cursor() as cur:
                await cur.execute(query, clean_p)
                rows = await cur.fetchall()
                return [_dict_from_row(r) for r in rows if r]
        else:
            q, p = _convert_params_for_sqlite(query, params)
            cur = conn.cursor()
            cur.execute(q, p)
            rows = cur.fetchall()
            return [_dict_from_row(r) for r in rows if r]


async def execute(query: str, params: tuple | list = ()) -> None:
    """Execute an INSERT, UPDATE, or DELETE statement."""
    await init_db()
    async with get_connection() as conn:
        if is_postgres():
            clean_p = _convert_params_for_postgres(params)
            async with conn.cursor() as cur:
                await cur.execute(query, clean_p)
        else:
            q, p = _convert_params_for_sqlite(query, params)
            cur = conn.cursor()
            cur.execute(q, p)
            conn.commit()
