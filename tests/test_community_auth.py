"""Integration tests for Supabase Authentication and Community Itinerary Platform."""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.db import init_db
from app.services.community import seed_initial_community_trips


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()
    await seed_initial_community_trips()


@pytest.mark.asyncio
async def test_user_signup_login_and_me():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        unique_email = f"traveler_{uuid.uuid4().hex[:8]}@example.com"

        # 1. Signup
        signup_res = await ac.post(
            "/auth/signup",
            json={
                "email": unique_email,
                "password": "SecurePassword123!",
                "full_name": "Maya Lin",
                "bio": "Backpacker & food enthusiast",
            },
        )
        assert signup_res.status_code == 201
        data = signup_res.json()
        assert "access_token" in data
        assert data["user"]["email"] == unique_email
        assert data["user"]["full_name"] == "Maya Lin"
        token = data["access_token"]

        # 2. Get Me with Bearer token
        me_res = await ac.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me_res.status_code == 200
        me_data = me_res.json()
        assert me_data["email"] == unique_email

        # 3. Login with correct password
        login_res = await ac.post(
            "/auth/login",
            json={"email": unique_email, "password": "SecurePassword123!"},
        )
        assert login_res.status_code == 200
        assert "access_token" in login_res.json()

        # 4. Login with invalid password
        bad_login = await ac.post(
            "/auth/login",
            json={"email": unique_email, "password": "WrongPassword!"},
        )
        assert bad_login.status_code == 401


@pytest.mark.asyncio
async def test_community_trips_feed():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Feed
        res = await ac.get("/community/trips")
        assert res.status_code == 200
        trips = res.json()
        assert isinstance(trips, list)
        assert len(trips) >= 1

        # Search by destination
        search_res = await ac.get("/community/trips?destination=Kyoto")
        assert search_res.status_code == 200
        matching = search_res.json()
        for t in matching:
            assert "kyoto" in t["destination"].lower() or "kyoto" in t["title"].lower()


@pytest.mark.asyncio
async def test_community_publish_review_and_like():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Signup a user to create and review
        unique_email = f"author_{uuid.uuid4().hex[:8]}@example.com"
        auth_res = await ac.post(
            "/auth/signup",
            json={
                "email": unique_email,
                "password": "Password123!",
                "full_name": "Oliver Queen",
            },
        )
        token = auth_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Publish a trip
        trip_payload = {
            "destination": "Goa, India",
            "title": "Sunsets & Heritage in South Goa",
            "description": "Relaxing beach corridors and Portuguese villas.",
            "duration_days": 4,
            "budget": 800,
            "currency": "USD",
            "travelers": 2,
            "tags": ["Nature", "Food & Dining", "Relaxed"],
            "itinerary_data": {
                "destination": "Goa, India",
                "days": [
                    {
                        "day_number": 1,
                        "title": "Palolem Beach & Seafood",
                        "activities": [
                            {"time": "16:00", "title": "Palolem Beach Sunset Walk", "location_name": "Palolem Beach"}
                        ],
                    }
                ],
            },
        }

        pub_res = await ac.post("/community/trips", json=trip_payload, headers=headers)
        assert pub_res.status_code == 201
        trip = pub_res.json()
        trip_id = trip["id"]
        assert trip["title"] == "Sunsets & Heritage in South Goa"

        # 3. Post structured review with 'liked_aspects' and 'suggested_additions'
        review_payload = {
            "rating": 5,
            "liked_aspects": "The South Goa beaches were pristine and far less crowded.",
            "suggested_additions": "Add a morning kayaking excursion in the Sal backwaters!",
            "comment": "Incredible relaxing journey.",
        }
        rev_res = await ac.post(
            f"/community/trips/{trip_id}/reviews",
            json=review_payload,
            headers=headers,
        )
        assert rev_res.status_code == 201
        rev = rev_res.json()
        assert rev["rating"] == 5
        assert "South Goa" in rev["liked_aspects"]
        assert "kayaking" in rev["suggested_additions"]

        # 4. Fetch trip detail to verify review was attached
        detail_res = await ac.get(f"/community/trips/{trip_id}", headers=headers)
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["reviews_count"] >= 1
        assert len(detail["reviews"]) >= 1

        # 5. Toggle Like
        like_res = await ac.post(f"/community/trips/{trip_id}/like", headers=headers)
        assert like_res.status_code == 200
        like_data = like_res.json()
        assert like_data["liked"] is True
        assert like_data["likes_count"] >= 1

        # Toggle Unlike
        unlike_res = await ac.post(f"/community/trips/{trip_id}/like", headers=headers)
        assert unlike_res.status_code == 200
        unlike_data = unlike_res.json()
        assert unlike_data["liked"] is False

        # 6. Test AI Remix
        remix_res = await ac.post(
            f"/community/trips/{trip_id}/remix-ai",
            json={"custom_instruction": "We also love heritage churches."},
        )
        assert remix_res.status_code == 200
        remix_data = remix_res.json()
        assert "remixed_summary" in remix_data
        assert "new_plan_id" in remix_data
