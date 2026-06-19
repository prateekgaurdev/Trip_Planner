"""Tests for selective Wikipedia image matching."""
from app.tools import _image_match_confidence, enrich_activities_with_images


def test_image_match_confidence_accepts_landmark():
    score = _image_match_confidence(
        "Evening Ganga Aarti",
        "Parmarth Niketan",
        "Parmarth Niketan Ashram",
        "Rishikesh, India",
    )
    assert score >= 0.5


def test_image_match_confidence_rejects_unrelated():
    score = _image_match_confidence(
        "White water rafting",
        "Ganges River",
        "Eiffel Tower Paris",
        "Rishikesh, India",
    )
    assert score < 0.5


async def test_enrich_skips_when_stubs():
    days = [{"activities": [{"title": "Test Place", "location_name": "Test Place"}]}]
    out = await enrich_activities_with_images("Rishikesh", days)
    assert out[0]["activities"][0].get("image_url") is None
