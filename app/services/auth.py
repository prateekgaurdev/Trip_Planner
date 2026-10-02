"""Authentication and JWT session management for Wayfarer.

Features:
- Direct bcrypt password hashing with automatic salt generation
- Cryptographically signed JWT access tokens (HS256)
- FastAPI HTTPBearer security dependency for route protection
- User profile retrieval and creation via unified DB manager
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any
from urllib.parse import quote

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.services.db import execute, fetch_one, is_postgres

logger = logging.getLogger("wayfarer.auth")
security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    pw_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against bcrypt hash."""
    pw_bytes = plain_password.encode("utf-8")[:72]
    hash_bytes = hashed_password.encode("utf-8")
    try:
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except Exception:
        return False


def create_access_token(user_id: str, email: str, full_name: str) -> str:
    """Generate a signed JWT token."""
    settings = get_settings()
    expire = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=settings.jwt_expiration_hours)
    payload = {
        "sub": str(user_id),
        "email": email,
        "name": full_name,
        "exp": int(expire.timestamp()),
        "iat": int(dt.datetime.now(dt.timezone.utc).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT access token."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Retrieve user record by email."""
    tbl = "public.users" if is_postgres() else "users"
    return await fetch_one(f"SELECT * FROM {tbl} WHERE LOWER(email) = LOWER(%s);", (email.strip(),))


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    """Retrieve user record by UUID id."""
    tbl = "public.users" if is_postgres() else "users"
    return await fetch_one(f"SELECT * FROM {tbl} WHERE id = %s;", (str(user_id),))


async def create_user(
    email: str,
    password: str,
    full_name: str,
    bio: str = "",
    avatar_url: str = "",
) -> dict[str, Any]:
    """Register a new user."""
    clean_email = email.strip().lower()
    existing = await get_user_by_email(clean_email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user_id = str(uuid.uuid4())
    pw_hash = hash_password(password)
    clean_name = full_name.strip()
    if not avatar_url:
        avatar_url = f"https://ui-avatars.com/api/?name={quote(clean_name)}&background=0d9488&color=fff&bold=true"

    tbl = "public.users" if is_postgres() else "users"
    query = f"""
        INSERT INTO {tbl} (id, email, password_hash, full_name, avatar_url, bio)
        VALUES (%s, %s, %s, %s, %s, %s);
    """
    await execute(query, (user_id, clean_email, pw_hash, clean_name, avatar_url, bio.strip()))

    return {
        "id": user_id,
        "email": clean_email,
        "full_name": clean_name,
        "avatar_url": avatar_url,
        "bio": bio.strip(),
    }


async def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    """Verify credentials and return user profile if valid."""
    user = await get_user_by_email(email)
    if not user:
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    return user


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    """Dependency enforcing authentication."""
    if not creds or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(creds.credentials)
    if not payload or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid token. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found.",
        )
    return user


async def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any] | None:
    """Dependency providing user if authenticated, None otherwise."""
    if not creds or not creds.credentials:
        return None
    payload = decode_access_token(creds.credentials)
    if not payload or not payload.get("sub"):
        return None
    return await get_user_by_id(payload["sub"])
