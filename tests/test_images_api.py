"""HTTP tests for parallel-friendly activity image lookup."""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ["USE_STUBS"] = "true"
os.environ["CHECKPOINT_DB"] = ":memory:"
os.environ["GRAPH_BACKGROUND"] = "false"

from app.main import app  # noqa: E402


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            yield c


async def test_images_lookup_endpoint(client: AsyncClient):
    resp = await client.post(
        "/images/lookup",
        json={
            "destination": "Rishikesh, India",
            "title": "Evening Aarti",
            "location_name": "Parmarth Niketan",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "image_url" in body
