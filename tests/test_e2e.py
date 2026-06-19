"""End-to-end HITL round-trip test.

Runs with USE_STUBS=true so it needs no API keys: submit → poll →
reject (loop) → modify (loop) → approve → final. This single test exercises
the whole graph, the interrupt/resume mechanism, and all three review paths.

Run:  USE_STUBS=true pytest -q
"""
from __future__ import annotations

import asyncio
import os

import pytest

# Force stub mode BEFORE app/config is imported anywhere.
os.environ["USE_STUBS"] = "true"
os.environ["CHECKPOINT_DB"] = ":memory:"
os.environ["GRAPH_BACKGROUND"] = "false"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            yield c


async def _wait_for_status(
    client: AsyncClient,
    plan_id: str,
    status: str,
    *,
    min_revision: int | None = None,
    timeout: float = 30.0,
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/plan/{plan_id}")
        if resp.status_code == 200:
            body = resp.json()
            if body.get("status") == status:
                if min_revision is None or body.get("revision_count", 0) >= min_revision:
                    return body
        await asyncio.sleep(0.05)
    pytest.fail(f"Timed out waiting for status={status!r} on plan {plan_id}")


async def test_full_hitl_roundtrip(client: AsyncClient):
    resp = await client.post(
        "/plan",
        json={
            "destination": "Lisbon, Portugal",
            "start_date": "2026-09-10",
            "end_date": "2026-09-13",
            "budget": 2000,
            "currency": "USD",
            "travelers": 2,
            "interests": ["food", "history"],
        },
    )
    assert resp.status_code == 201
    plan_id = resp.json()["plan_id"]

    state = await _wait_for_status(client, plan_id, "awaiting_review")
    assert state["draft_itinerary"]["days"]

    r = await client.post(
        f"/plan/{plan_id}/review",
        json={"action": "reject", "feedback": "More museums please."},
    )
    assert r.status_code == 200
    state = await _wait_for_status(client, plan_id, "awaiting_review", min_revision=1)

    r = await client.post(
        f"/plan/{plan_id}/review",
        json={"action": "modify", "feedback": "Make day 2 a relaxed day."},
    )
    assert r.status_code == 200
    state = await _wait_for_status(client, plan_id, "awaiting_review", min_revision=2)

    r = await client.post(f"/plan/{plan_id}/review", json={"action": "approve"})
    assert r.status_code == 200
    await _wait_for_status(client, plan_id, "completed")

    final = await client.get(f"/plan/{plan_id}/final")
    assert final.status_code == 200
    body = final.json()["final_plan"]
    assert body["approved"] is True
    assert body["revisions"] == 2
    assert body["destination"] == "Lisbon, Portugal"
    assert body.get("route_map", {}).get("available") is True
    assert body["route_map"]["totals"]["distance_km"] > 0


async def test_review_before_ready_is_409(client: AsyncClient):
    r = await client.post("/plan/does-not-exist/review", json={"action": "approve"})
    assert r.status_code == 404
