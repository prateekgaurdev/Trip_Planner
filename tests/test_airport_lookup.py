"""Tests for IATA airport resolution."""
from app.services.airport_lookup import lookup_iata_codes, resolve_airport_records


def test_delhi_resolves_to_del():
    codes = lookup_iata_codes("Delhi")
    assert "DEL" in codes


def test_rishikesh_resolves_to_ded():
    codes = lookup_iata_codes("Rishikesh")
    assert codes[0] == "DED"
    assert "DEL" in codes


def test_resolve_airport_records_has_iata():
    recs = resolve_airport_records("Mumbai")
    assert recs[0]["iata"] == "BOM"
