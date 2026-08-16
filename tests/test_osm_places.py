"""Phase 5: Overpass parsing (recorded fixture), niche mapping, Places field
mask + daily cap. All offline."""
import pytest

from engine.providers.base import RawProspect
from engine.providers.osm import (build_query, parse_elements, tags_for_niche)
from engine.providers.places import (FIELD_MASK, PlacesCapReached,
                                     PlacesProvider, calls_today)

# Recorded (trimmed) Overpass response shape for amenity=restaurant in Giza.
OVERPASS_FIXTURE = {
    "elements": [
        {"type": "node", "id": 111, "tags": {
            "name": "Koshary El Zaeem", "amenity": "restaurant",
            "cuisine": "egyptian", "phone": "+20 2 3570 0000",
            "addr:street": "Al Haram", "addr:city": "Giza"}},
        {"type": "way", "id": 222, "tags": {
            "name": "Pizza Station Giza", "amenity": "restaurant",
            "website": "https://pizzastation.example",
            "contact:email": "hi@pizzastation.example"}},
        {"type": "node", "id": 333, "tags": {"amenity": "restaurant"}},  # no name
    ]
}


def test_parse_elements_maps_tags():
    rows = parse_elements(OVERPASS_FIXTURE["elements"], "restaurants", "giza")
    assert len(rows) == 2  # unnamed POI dropped
    first = rows[0]
    assert first.name == "Koshary El Zaeem"
    assert first.phone == "+20 2 3570 0000"
    assert first.category == "egyptian"
    assert first.city == "Giza"
    assert "openstreetmap.org/node/111" in first.maps_url
    second = rows[1]
    assert second.website == "https://pizzastation.example"
    assert second.emails == ["hi@pizzastation.example"]


def test_niche_mapping_and_fallback():
    tags, matched = tags_for_niche("restaurants")
    assert matched and 'node["amenity"="restaurant"]' in tags
    tags, matched = tags_for_niche("shisha lounges")
    assert not matched  # unknown niche -> name-substring fallback, flagged
    assert any('"name"~' in t for t in tags)


def test_build_query_shape():
    q = build_query(3600123456, ['node["amenity"="cafe"]'], limit=10)
    assert "area(3600123456)->.searchArea;" in q
    assert 'node["amenity"="cafe"](area.searchArea);' in q
    assert "[out:json]" in q


def test_osm_exclusion_applies(monkeypatch, tmp_path):
    import engine.providers.osm as osm

    monkeypatch.setattr(osm, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(osm, "_cache_get",
                        lambda key: OVERPASS_FIXTURE if key.startswith("overpass") else None)
    provider = osm.OsmProvider()
    known = RawProspect(name="Koshary El Zaeem", city="Giza").dedupe_key
    rows = provider.search("restaurants giza egypt", limit=10,
                           exclude_keys={known})
    assert provider.last_skipped_known == 1
    assert all(r.name != "Koshary El Zaeem" for r in rows)


def test_places_field_mask_is_lean():
    # the mask is the cost lever: no default payload, only needed fields
    assert "places.displayName" in FIELD_MASK
    assert "places.websiteUri" in FIELD_MASK
    assert "*" not in FIELD_MASK
    assert "photos" not in FIELD_MASK
    assert "reviews" not in FIELD_MASK


def test_places_requires_key(monkeypatch):
    # Hermetic: the developer's real .env may carry a key.
    monkeypatch.setenv("PLACES_API_KEY", "")
    from engine.config import get_settings
    get_settings.cache_clear()
    provider = PlacesProvider()
    with pytest.raises(ValueError, match="PLACES_API_KEY"):
        provider.search("hvac tampa", limit=5)


def test_places_daily_cap_trips(session, monkeypatch):
    monkeypatch.setenv("PLACES_API_KEY", "test-key")
    monkeypatch.setenv("PLACES_DAILY_CALL_CAP", "2")
    from engine.config import get_settings
    get_settings.cache_clear()

    import engine.providers.places as places

    monkeypatch.setattr(places, "_record_call",
                        lambda s, count=1: None)
    from engine.state import set_state
    set_state(session, places._today_key(), "2")  # already at cap

    provider = places.PlacesProvider()
    with pytest.raises(PlacesCapReached, match="cap reached"):
        provider.search("hvac tampa", limit=5)
    assert calls_today(session) == 2  # cap check never spent another call