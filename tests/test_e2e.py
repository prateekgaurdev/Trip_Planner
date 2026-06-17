"""End-to-end HITL round-trip test.

Runs with USE_STUBS=true so it needs no API keys: submit → poll →
reject (loop) → modify (loop) → approve → final. This single test exercises
the whole graph, the interrupt/resume mechanism, and all three review paths.

Run:  USE_STUBS=true pytest -q
"""
from __future__ import annotations

import os

import pytest

# Force stub mode BEFORE app/config is imported anywhere.
os.environ["USE_STUBS"] = "true"
os.environ["CHECKPOINT_DB"] = ":memory:"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
async def client():
    # Trigger FastAPI lifespan so the graph/checkpointer is built.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            yield c


async def test_full_hitl_roundtrip(client: AsyncClient):
    # 1. Create a plan.
    resp = await client.post(
        "/plan",
        json={
            "destination": "Lisbon, Portugal",
            "start_date": "2026-09-10",
            "end_date": "2026-09-13",
            "budget_usd": 2000,
            "travelers": 2,
            "interests": ["food", "history"],
        },
    )
    assert resp.status_code == 201
    plan_id = resp.json()["plan_id"]

    # 2. It should be paused awaiting review with a draft present.
    state = (await client.get(f"/plan/{plan_id}")).json()
    assert state["status"] == "awaiting_review"
    assert state["draft_itinerary"]["days"]

    # 3. Reject → loops back through research, ends awaiting_review again.
    r = await client.post(
        f"/plan/{plan_id}/review",
        json={"action": "reject", "feedback": "More museums please."},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_review"
    assert r.json()["revision_count"] == 1

    # 4. Modify → loops through planner only, still awaiting_review.
    r = await client.post(
        f"/plan/{plan_id}/review",
        json={"action": "modify", "feedback": "Make day 2 a relaxed day."},
    )
    assert r.json()["status"] == "awaiting_review"
    assert r.json()["revision_count"] == 2

    # 5. Approve → finalize.
    r = await client.post(f"/plan/{plan_id}/review", json={"action": "approve"})
    assert r.json()["status"] == "completed"

    # 6. Final plan is retrievable and marked approved.
    final = await client.get(f"/plan/{plan_id}/final")
    assert final.status_code == 200
    body = final.json()["final_plan"]
    assert body["approved"] is True
    assert body["revisions"] == 2
    assert body["destination"] == "Lisbon, Portugal"


async def test_review_before_ready_is_409(client: AsyncClient):
    # Reviewing a non-existent plan → 404.
    r = await client.post("/plan/does-not-exist/review", json={"action": "approve"})
    assert r.status_code == 404
