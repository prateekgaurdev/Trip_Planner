"""Community trip sharing, structured reviews, and AI Remix engine.

Supports:
- Public publishing of AI-crafted itineraries
- Structured community reviews ("What I liked" & "What to add to make it more wonderful")
- Interactive upvotes and rating calculations
- AI Community Remix: Synthesizes traveler suggestions into an enhanced plan
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.schemas import (
    CommunityReviewCreateRequest,
    CommunityTripCreateRequest,
)
from app.services.db import execute, fetch_all, fetch_one, is_postgres
from app.services.llm import get_llm

logger = logging.getLogger("wayfarer.community")


def _tbl(name: str) -> str:
    return f"public.{name}" if is_postgres() else name


async def list_community_trips(
    destination: str | None = None,
    tag: str | None = None,
    sort_by: str = "likes",
    limit: int = 40,
    current_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Query published trips with search, tag filtering, and like status."""
    trips_tbl = _tbl("community_trips")
    likes_tbl = _tbl("community_trip_likes")

    where_clauses: list[str] = []
    params: list[Any] = []

    if destination and destination.strip():
        op = "ILIKE" if is_postgres() else "LIKE"
        where_clauses.append(f"(destination {op} %s OR title {op} %s)")
        term = f"%{destination.strip()}%"
        params.extend([term, term])

    if tag and tag.strip():
        op = "ILIKE" if is_postgres() else "LIKE"
        where_clauses.append(f"tags::text {op} %s" if is_postgres() else f"tags {op} %s")
        params.append(f"%{tag.strip()}%")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if sort_by == "rating":
        order_sql = "ORDER BY average_rating DESC, reviews_count DESC, created_at DESC"
    elif sort_by == "newest":
        order_sql = "ORDER BY created_at DESC"
    else:  # default 'likes'
        order_sql = "ORDER BY likes_count DESC, created_at DESC"

    user_join = ""
    user_select = "FALSE as user_has_liked"
    if current_user_id:
        user_join = f"LEFT JOIN {likes_tbl} ul ON t.id = ul.trip_id AND ul.user_id = %s"
        user_select = "CASE WHEN ul.user_id IS NOT NULL THEN TRUE ELSE FALSE END as user_has_liked"
        params.insert(0, str(current_user_id))

    query = f"""
        SELECT 
            t.id, t.user_id, t.author_name, t.author_avatar, t.destination,
            t.title, t.description, t.origin, t.duration_days, t.budget,
            t.currency, t.travelers, t.tags, t.likes_count, t.reviews_count,
            t.average_rating, t.created_at, {user_select}
        FROM {trips_tbl} t
        {user_join}
        {where_sql}
        {order_sql}
        LIMIT {int(limit)};
    """

    rows = await fetch_all(query, params)
    for r in rows:
        if isinstance(r.get("tags"), str):
            try:
                r["tags"] = json.loads(r["tags"])
            except Exception:
                r["tags"] = []
        r["user_has_liked"] = bool(r.get("user_has_liked", False))
    return rows


async def get_community_trip(trip_id: str, current_user_id: str | None = None) -> dict[str, Any] | None:
    """Fetch full trip detail including full itinerary_data and reviews."""
    trips_tbl = _tbl("community_trips")
    reviews_tbl = _tbl("community_trip_reviews")
    likes_tbl = _tbl("community_trip_likes")

    trip_query = f"SELECT * FROM {trips_tbl} WHERE id = %s;"
    trip = await fetch_one(trip_query, (str(trip_id),))
    if not trip:
        return None

    # Check if user liked it
    user_has_liked = False
    if current_user_id:
        like_query = f"SELECT 1 FROM {likes_tbl} WHERE trip_id = %s AND user_id = %s;"
        liked_row = await fetch_one(like_query, (str(trip_id), str(current_user_id)))
        user_has_liked = bool(liked_row)

    # Fetch reviews
    reviews_query = f"""
        SELECT id, trip_id, user_id, user_name, user_avatar, rating, liked_aspects, suggested_additions, comment, created_at
        FROM {reviews_tbl}
        WHERE trip_id = %s
        ORDER BY created_at DESC;
    """
    reviews = await fetch_all(reviews_query, (str(trip_id),))

    trip["user_has_liked"] = user_has_liked
    trip["reviews"] = reviews
    if isinstance(trip.get("tags"), str):
        try:
            trip["tags"] = json.loads(trip["tags"])
        except Exception:
            trip["tags"] = []

    if isinstance(trip.get("itinerary_data"), str):
        try:
            trip["itinerary_data"] = json.loads(trip["itinerary_data"])
        except Exception:
            pass

    return trip


async def create_community_trip(
    req: CommunityTripCreateRequest,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish an itinerary to the public community feed."""
    trips_tbl = _tbl("community_trips")
    trip_id = str(uuid.uuid4())

    author_name = user["full_name"] if user else "Traveler"
    author_avatar = (
        user.get("avatar_url")
        if user
        else "https://ui-avatars.com/api/?name=Traveler&background=0d9488&color=fff"
    )
    user_id = user["id"] if user else None

    # Default tags if none provided
    tags = req.tags or ["Classic", "Sightseeing"]

    query = f"""
        INSERT INTO {trips_tbl} (
            id, user_id, author_name, author_avatar, destination, title,
            description, origin, duration_days, budget, currency, travelers,
            tags, itinerary_data, likes_count, reviews_count, average_rating
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, 5.0);
    """
    await execute(
        query,
        (
            trip_id,
            user_id,
            author_name,
            author_avatar,
            req.destination.strip(),
            req.title.strip(),
            req.description.strip(),
            req.origin.strip() if req.origin else None,
            req.duration_days,
            req.budget,
            req.currency,
            req.travelers,
            tags,
            req.itinerary_data,
        ),
    )

    return await get_community_trip(trip_id) or {"id": trip_id, "title": req.title}


async def add_trip_review(
    trip_id: str,
    req: CommunityReviewCreateRequest,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a structured review with ratings, highlights, and pro tips to make the trip wonderful."""
    reviews_tbl = _tbl("community_trip_reviews")
    trips_tbl = _tbl("community_trips")
    review_id = str(uuid.uuid4())

    user_name = user["full_name"] if user else "Verified Explorer"
    user_avatar = (
        user.get("avatar_url")
        if user
        else "https://ui-avatars.com/api/?name=Explorer&background=0284c7&color=fff"
    )
    user_id = user["id"] if user else None

    insert_sql = f"""
        INSERT INTO {reviews_tbl} (
            id, trip_id, user_id, user_name, user_avatar, rating,
            liked_aspects, suggested_additions, comment
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    await execute(
        insert_sql,
        (
            review_id,
            str(trip_id),
            user_id,
            user_name,
            user_avatar,
            int(req.rating),
            req.liked_aspects.strip(),
            req.suggested_additions.strip(),
            req.comment.strip() if req.comment else "",
        ),
    )

    # Recalculate average rating and review count
    calc_sql = f"""
        SELECT COUNT(*) as count, AVG(rating) as avg_rating
        FROM {reviews_tbl}
        WHERE trip_id = %s;
    """
    stats = await fetch_one(calc_sql, (str(trip_id),))
    cnt = stats["count"] if stats else 1
    avg_r = round(float(stats["avg_rating"]), 2) if stats and stats.get("avg_rating") else float(req.rating)

    update_sql = f"""
        UPDATE {trips_tbl}
        SET reviews_count = %s, average_rating = %s, updated_at = NOW()
        WHERE id = %s;
    """ if is_postgres() else f"""
        UPDATE {trips_tbl}
        SET reviews_count = %s, average_rating = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s;
    """
    await execute(update_sql, (cnt, avg_r, str(trip_id)))

    return {
        "id": review_id,
        "trip_id": str(trip_id),
        "user_id": user_id,
        "user_name": user_name,
        "user_avatar": user_avatar,
        "rating": req.rating,
        "liked_aspects": req.liked_aspects,
        "suggested_additions": req.suggested_additions,
        "comment": req.comment,
    }


async def toggle_trip_like(trip_id: str, user_id: str) -> dict[str, Any]:
    """Toggle upvote/like on a community trip."""
    likes_tbl = _tbl("community_trip_likes")
    trips_tbl = _tbl("community_trips")

    existing = await fetch_one(
        f"SELECT 1 FROM {likes_tbl} WHERE trip_id = %s AND user_id = %s;",
        (str(trip_id), str(user_id)),
    )

    if existing:
        # Unlike
        await execute(
            f"DELETE FROM {likes_tbl} WHERE trip_id = %s AND user_id = %s;",
            (str(trip_id), str(user_id)),
        )
        await execute(
            f"UPDATE {trips_tbl} SET likes_count = GREATEST(0, likes_count - 1) WHERE id = %s;"
            if is_postgres()
            else f"UPDATE {trips_tbl} SET likes_count = MAX(0, likes_count - 1) WHERE id = %s;",
            (str(trip_id),),
        )
        liked = False
    else:
        # Like
        await execute(
            f"INSERT INTO {likes_tbl} (trip_id, user_id) VALUES (%s, %s);",
            (str(trip_id), str(user_id)),
        )
        await execute(
            f"UPDATE {trips_tbl} SET likes_count = likes_count + 1 WHERE id = %s;",
            (str(trip_id),),
        )
        liked = True

    updated_trip = await fetch_one(f"SELECT likes_count FROM {trips_tbl} WHERE id = %s;", (str(trip_id),))
    likes_count = updated_trip["likes_count"] if updated_trip else 0

    return {"liked": liked, "likes_count": likes_count}


async def remix_trip_with_community_suggestions(
    trip_id: str,
    custom_instruction: str = "",
) -> dict[str, Any]:
    """AI Remix: Ingest community tips and upgrade the itinerary."""
    trip = await get_community_trip(trip_id)
    if not trip:
        raise ValueError(f"Community trip {trip_id} not found.")

    reviews = trip.get("reviews") or []
    suggestions = [r["suggested_additions"] for r in reviews if r.get("suggested_additions")]
    liked_points = [r["liked_aspects"] for r in reviews if r.get("liked_aspects")]

    sug_text = "\n".join(f"- {s}" for s in suggestions[:8]) or "None yet."
    liked_text = "\n".join(f"- {l}" for l in liked_points[:8]) or "Well-balanced itinerary."

    prompt_user = (
        f"Trip Destination: {trip.get('destination')}\n"
        f"Trip Title: {trip.get('title')}\n"
        f"Original Itinerary Overview: {trip.get('description')}\n"
        f"What the community loved:\n{liked_text}\n"
        f"Community suggestions & insider tips to make it more wonderful:\n{sug_text}\n"
        f"Traveler's custom note: '{custom_instruction}'\n\n"
        "Synthesize these real community recommendations into an upgraded 3-4 sentence remix pitch. "
        "Explain specifically which community recommendations were adopted to enhance the itinerary."
    )

    llm = get_llm()
    remixed_summary = await llm.complete(
        system="You are an expert AI travel concierge synthesizing community wisdom into an upgraded travel plan.",
        user=prompt_user,
    )

    new_plan_id = str(uuid.uuid4())
    return {
        "new_plan_id": new_plan_id,
        "message": f"Successfully remixed '{trip.get('title')}' with {len(suggestions)} community recommendations!",
        "remixed_summary": remixed_summary,
        "incorporated_suggestions": suggestions[:5],
    }



async def seed_initial_community_trips() -> None:
    """Seed initial high-quality community itineraries and reviews from JSON."""
    import os
    import json
    from pathlib import Path
    from app.services.auth import create_user, get_user_by_email
    from app.services.db import fetch_one

    trips_tbl = _tbl("community_trips")
    row = await fetch_one(f"SELECT COUNT(*) as count FROM {trips_tbl};")
    if row and row.get("count", 0) > 0:
        return  # Already seeded

    # Seed Users
    users = [
        {"email": "elena@example.com", "name": "Elena Rostova", "bio": "Cultural photographer & slow travel enthusiast.", "avatar": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=150&auto=format&fit=crop&q=80"},
        {"email": "aarav@example.com", "name": "Aarav Sharma", "bio": "Transit logistics nerd & Himalayan trekker.", "avatar": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=150&auto=format&fit=crop&q=80"},
        {"email": "priya@example.com", "name": "Priya Patel", "bio": "Yoga instructor and sacred spaces explorer.", "avatar": "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=150&auto=format&fit=crop&q=80"}
    ]
    
    user_map = {}
    for u in users:
        db_user = await get_user_by_email(u["email"])
        if not db_user:
            db_user = await create_user(
                email=u["email"], password="Password123!",
                full_name=u["name"], bio=u["bio"], avatar_url=u["avatar"]
            )
        user_map[u["name"]] = db_user

    # Load trips from JSON
    json_path = Path(__file__).parent.parent / "data" / "community" / "community_itineraries.json"
    if not json_path.exists():
        logger.warning(f"Community itineraries JSON not found at {json_path}")
        return
        
    with open(json_path, "r") as f:
        trips_data = json.load(f)
        
    for td in trips_data:
        author = user_map.get(td.get("author_name"))
        if not author:
            author = list(user_map.values())[0] # fallback
            
        req = CommunityTripCreateRequest(
            destination=td.get("destination", td.get("title", "Destination")),
            title=td["title"],
            description=td["summary"],
            itinerary_data=td,
            tags=td["tags"]
        )
        await create_community_trip(req, author)
        
    logger.info(f"Successfully seeded {len(trips_data)} realistic community trips from JSON.")


    trips_tbl = _tbl("community_trips")
    row = await fetch_one(f"SELECT COUNT(*) as count FROM {trips_tbl};")
    if row and row.get("count", 0) > 0:
        return

    # Seed User 1: Elena
    elena = await get_user_by_email("elena@example.com")
    if not elena:
        elena = await create_user(
            email="elena@example.com",
            password="Password123!",
            full_name="Elena Rostova",
            bio="Cultural photographer & slow travel enthusiast.",
            avatar_url="https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=150&auto=format&fit=crop&q=80",
        )

    # Seed User 2: Aarav
    aarav = await get_user_by_email("aarav@example.com")
    if not aarav:
        aarav = await create_user(
            email="aarav@example.com",
            password="Password123!",
            full_name="Aarav Sharma",
            bio="Transit logistics nerd & Himalayan trekker.",
            avatar_url="https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=150&auto=format&fit=crop&q=80",
        )

    # Seed User 3: Priya
    priya = await get_user_by_email("priya@example.com")
    if not priya:
        priya = await create_user(
            email="priya@example.com",
            password="Password123!",
            full_name="Priya Patel",
            bio="Yoga instructor and sacred spaces explorer.",
            avatar_url="https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=150&auto=format&fit=crop&q=80",
        )

    # Seed Trip 1: Kyoto
    kyoto_itinerary = {
        "destination": "Kyoto, Japan",
        "summary": "A peaceful 4-day autumn journey through Kyoto's historic shrines, bamboo groves, and Nishiki Market.",
        "lodging_anchor": {
            "name": "Boutique Gion Ryokan",
            "neighborhood": "Gion & Higashiyama",
            "reason": "Authentic historic base with walking access to temples.",
        },
        "days": [
            {
                "day_number": 1,
                "title": "Arrival & Historic Higashiyama",
                "neighborhood_cluster": "Higashiyama",
                "activities": [
                    {"time": "14:00", "title": "Check-in at Boutique Gion Ryokan", "location_name": "Gion Ryokan"},
                    {"time": "15:30", "title": "Kiyomizu-dera Wooden Terrace", "location_name": "Kiyomizu-dera"},
                    {"time": "18:00", "title": "Lantern Walk through Ninenzaka & Sannenzaka", "location_name": "Ninenzaka"},
                    {"time": "20:00", "title": "Traditional Kaiseki Dinner in Pontocho Alley", "location_name": "Pontocho"},
                ],
            },
            {
                "day_number": 2,
                "title": "Arashiyama Bamboo & Sagano Romance",
                "neighborhood_cluster": "Arashiyama",
                "activities": [
                    {"time": "08:30", "title": "Arashiyama Bamboo Grove Walk", "location_name": "Arashiyama Bamboo Grove"},
                    {"time": "10:30", "title": "Tenryu-ji Zen Garden", "location_name": "Tenryu-ji"},
                    {"time": "13:00", "title": "Tofu Feast overlooking Togetsukyo Bridge", "location_name": "Togetsukyo Bridge"},
                ],
            },
        ],
    }
    trip_1 = await create_community_trip(
        CommunityTripCreateRequest(
            destination="Kyoto, Japan",
            title="Kyoto Autumn Heritage & Tea Culture",
            description="Our 4-day honeymoon in Kyoto! Clustered strictly by neighborhood so you never waste hours traveling between sights.",
            origin="Tokyo, Japan",
            duration_days=4,
            budget=2500,
            currency="USD",
            travelers=2,
            tags=["Culture", "Food & Dining", "Temples", "Honeymoon"],
            itinerary_data=kyoto_itinerary,
        ),
        user=elena,
    )

    if trip_1 and trip_1.get("id"):
        await add_trip_review(
            trip_1["id"],
            CommunityReviewCreateRequest(
                rating=5,
                liked_aspects="The sequence of Gion tea houses and the wooden terrace at Kiyomizu-dera at sunset was completely magical. Pacing was very realistic.",
                suggested_additions="On Day 3, book Gion Duck Noodles 2 days in advance! Also, rent an e-bike in Arashiyama to zip between bamboo groves without sweating on hills.",
                comment="Best romantic itinerary we have experienced.",
            ),
            user=aarav,
        )

    # Seed Trip 2: Rishikesh
    rishikesh_itinerary = {
        "destination": "Rishikesh, India",
        "summary": "A 5-day spiritual yoga retreat and riverfront expedition from Delhi to the Himalayan foothills.",
        "lodging_anchor": {
            "name": "Tapovan Riverside Boutique Stay",
            "neighborhood": "Tapovan & Laxman Jhula",
            "reason": "Quiet riverside bluffs with sunrise Himalayan views.",
        },
        "days": [
            {
                "day_number": 1,
                "title": "Delhi Corridor Transit & Parmarth Aarti",
                "neighborhood_cluster": "Tapovan",
                "activities": [
                    {"time": "06:45", "title": "Vande Bharat Express: New Delhi to Haridwar", "location_name": "New Delhi Railway Station"},
                    {"time": "12:30", "title": "Scenic transfer & Check-in at Tapovan Riverside", "location_name": "Tapovan"},
                    {"time": "17:30", "title": "Sacred Ganga Aarti at Parmarth Niketan", "location_name": "Parmarth Niketan"},
                ],
            }
        ],
    }
    trip_2 = await create_community_trip(
        CommunityTripCreateRequest(
            destination="Rishikesh, India",
            title="Rishikesh Spiritual Yoga & Sacred River Corridor",
            description="Starting from Delhi, took the express train corridor and spent 5 serene days along the holy Ganges. Perfect yoga and vegan food pacing.",
            origin="Delhi, India",
            duration_days=5,
            budget=45000,
            currency="INR",
            travelers=2,
            tags=["Wellness", "Yoga", "Adventure", "Corridor"],
            itinerary_data=rishikesh_itinerary,
        ),
        user=aarav,
    )

    # Add reviews to Rishikesh
    if trip_2 and trip_2.get("id"):
        await add_trip_review(
            trip_2["id"],
            CommunityReviewCreateRequest(
                rating=5,
                liked_aspects="The door-to-door corridor transit on Day 1 using Vande Bharat was genius. Ganga Aarti at sunset gave me goosebumps.",
                suggested_additions="Definitely book an early morning cab to Kunjapuri Temple at 05:00 AM for the sunrise over the snow peaks. It is only 45 minutes away!",
                comment="Best spiritual itinerary I have followed.",
            ),
            user=priya,
        )

