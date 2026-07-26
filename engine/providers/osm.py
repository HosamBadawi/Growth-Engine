"""OsmProvider: OpenStreetMap via the Overpass API.

Free, keyless, worldwide, open data (ODbL), no ToS conflict, no bot detection.
This is the provider for arbitrary niches in arbitrary cities: restaurants in
Cairo, pharmacies in Alexandria, cafes anywhere (places no license registry
covers).

Research note (repo rule 6): raw httpx against Overpass/Nominatim instead of
the `overpy`/`OSMPythonTools` wrappers. Both add dependency weight for two
POST requests, and neither handles our caching/rate-limit policy anyway.

Usage policy (https://dev.overpass-api.de/overpass-doc/en/preface/commons.html):
descriptive User-Agent with contact URL, at most one query every 2 seconds,
backoff on 429/504, 7-day response cache. Heavy users should self-host an
instance (documented in the README).
"""
import json
import logging
import re
import time
from pathlib import Path

import httpx

from engine.providers.base import ProspectProvider, RawProspect
from engine.util import utcnow

log = logging.getLogger("prospector.osm")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = ("GrowthEngine/2.1 (open-source outbound machine; "
              "https://github.com/HosamBadawi/growth-engine)")
CACHE_DIR = Path("data/osm")
CACHE_TTL_DAYS = 7
MIN_SECONDS_BETWEEN_QUERIES = 2.0
TIMEOUT = 90

# Human niche -> OSM tag filters. Extensible: add entries, or fall back to a
# name-substring match (logged) for unknown niches.
NICHE_TAGS: dict[str, list[str]] = {
    "restaurant": ['node["amenity"="restaurant"]', 'way["amenity"="restaurant"]'],
    "restaurants": ['node["amenity"="restaurant"]', 'way["amenity"="restaurant"]'],
    "cafe": ['node["amenity"="cafe"]', 'way["amenity"="cafe"]'],
    "cafes": ['node["amenity"="cafe"]', 'way["amenity"="cafe"]'],
    "coffee": ['node["amenity"="cafe"]', 'way["amenity"="cafe"]'],
    "fast food": ['node["amenity"="fast_food"]', 'way["amenity"="fast_food"]'],
    "bar": ['node["amenity"="bar"]', 'way["amenity"="bar"]'],
    "bars": ['node["amenity"="bar"]', 'way["amenity"="bar"]'],
    "pharmacy": ['node["amenity"="pharmacy"]', 'way["amenity"="pharmacy"]'],
    "pharmacies": ['node["amenity"="pharmacy"]', 'way["amenity"="pharmacy"]'],
    "dentist": ['node["amenity"="dentist"]', 'way["amenity"="dentist"]'],
    "dentists": ['node["amenity"="dentist"]', 'way["amenity"="dentist"]'],
    "doctor": ['node["amenity"="doctors"]', 'way["amenity"="doctors"]'],
    "doctors": ['node["amenity"="doctors"]', 'way["amenity"="doctors"]'],
    "hotel": ['node["tourism"="hotel"]', 'way["tourism"="hotel"]'],
    "hotels": ['node["tourism"="hotel"]', 'way["tourism"="hotel"]'],
    "gym": ['node["leisure"="fitness_centre"]', 'way["leisure"="fitness_centre"]'],
    "gyms": ['node["leisure"="fitness_centre"]', 'way["leisure"="fitness_centre"]'],
    "bakery": ['node["shop"="bakery"]', 'way["shop"="bakery"]'],
    "bakeries": ['node["shop"="bakery"]', 'way["shop"="bakery"]'],
    "supermarket": ['node["shop"="supermarket"]', 'way["shop"="supermarket"]'],
    "hairdresser": ['node["shop"="hairdresser"]', 'way["shop"="hairdresser"]'],
    "barber": ['node["shop"="hairdresser"]', 'way["shop"="hairdresser"]'],
    "car repair": ['node["shop"="car_repair"]', 'way["shop"="car_repair"]'],
    "mechanic": ['node["shop"="car_repair"]', 'way["shop"="car_repair"]'],
    "plumber": ['node["craft"="plumber"]', 'way["craft"="plumber"]'],
    "electrician": ['node["craft"="electrician"]', 'way["craft"="electrician"]'],
    "hvac": ['node["craft"="hvac"]', 'way["craft"="hvac"]'],
    "lawyer": ['node["office"="lawyer"]', 'way["office"="lawyer"]'],
    "accountant": ['node["office"="accountant"]', 'way["office"="accountant"]'],
    "estate agent": ['node["office"="estate_agent"]', 'way["office"="estate_agent"]'],
    "veterinary": ['node["amenity"="veterinary"]', 'way["amenity"="veterinary"]'],
    "vet": ['node["amenity"="veterinary"]', 'way["amenity"="veterinary"]'],
}

_last_query_ts = 0.0


def _pace() -> None:
    """At most one Overpass/Nominatim query every 2 seconds, per policy."""
    global _last_query_ts
    wait = MIN_SECONDS_BETWEEN_QUERIES - (time.monotonic() - _last_query_ts)
    if wait > 0:
        time.sleep(wait)
    _last_query_ts = time.monotonic()


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^a-z0-9]+", "_", key.lower())[:120]
    return CACHE_DIR / f"{safe}.json"


def _cache_get(key: str):
    path = _cache_path(key)
    if not path.exists():
        return None
    age_days = (utcnow().timestamp() - path.stat().st_mtime) / 86400
    if age_days > CACHE_TTL_DAYS:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_put(key: str, data) -> None:
    import os

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _cache_path(key).with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, _cache_path(key))


def _request_json(client: httpx.Client, method: str, url: str, *,
                  attempts: int = 3, **kw):
    """One paced request with backoff on 429/504 per the usage policy."""
    delay = 10
    for attempt in range(attempts):
        _pace()
        try:
            resp = client.request(method, url, timeout=TIMEOUT, **kw)
            if resp.status_code in (429, 504):
                log.warning("Overpass/Nominatim %s, backing off %ss", resp.status_code, delay)
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            if attempt == attempts - 1:
                raise
            log.warning("OSM request failed (%s), retrying in %ss", exc, delay)
            time.sleep(delay)
            delay *= 2
    return None


def resolve_area(client: httpx.Client, place: str) -> tuple[int | None, str]:
    """City/region name -> (Overpass area id, ISO country code), cached 7 days.

    The country code drives country-aware domain guessing downstream: without
    it, a Brazilian restaurant only ever gets probed at `.com`.
    """
    cached = _cache_get(f"nominatim2_{place}")
    if isinstance(cached, dict):
        return (cached.get("area_id") or None), (cached.get("country") or "")
    data = _request_json(
        client, "GET", NOMINATIM_URL,
        params={"q": place, "format": "json", "limit": 5, "addressdetails": 1},
        headers={"User-Agent": USER_AGENT},
    )
    area_id, country = None, ""
    for hit in data or []:
        osm_type, osm_id = hit.get("osm_type"), hit.get("osm_id")
        code = ((hit.get("address") or {}).get("country_code") or "").upper()
        if osm_type == "relation":
            area_id, country = 3600000000 + int(osm_id), code or country
            break
        if osm_type == "way" and area_id is None:
            area_id, country = 2400000000 + int(osm_id), code or country
    _cache_put(f"nominatim2_{place}", {"area_id": area_id or 0, "country": country})
    return area_id, country


def resolve_area_id(client: httpx.Client, place: str) -> int | None:
    """Back-compat shim for callers that only need the area id."""
    return resolve_area(client, place)[0]


def tags_for_niche(niche: str) -> tuple[list[str], bool]:
    """(tag filters, matched). Unknown niches fall back to name matching."""
    key = niche.strip().lower()
    if key in NICHE_TAGS:
        return NICHE_TAGS[key], True
    for known, tags in NICHE_TAGS.items():
        if known in key or key in known:
            return tags, True
    # Fallback: any named business whose name contains the niche string.
    escaped = re.sub(r'(["\\])', r"\\\1", niche.strip())
    return [f'node["name"~"{escaped}",i]', f'way["name"~"{escaped}",i]'], False


def build_query(area_id: int, tag_filters: list[str], limit: int) -> str:
    body = "\n".join(f"  {f}(area.searchArea);" for f in tag_filters)
    return (f"[out:json][timeout:{TIMEOUT - 5}];\n"
            f"area({area_id})->.searchArea;\n"
            f"(\n{body}\n);\n"
            f"out center tags {max(limit * 3, 60)};")


def parse_elements(elements: list[dict], niche: str, city: str,
                   country: str = "") -> list[RawProspect]:
    out = []
    for el in elements or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        address = ", ".join(filter(None, [
            " ".join(filter(None, [tags.get("addr:housenumber"),
                                   tags.get("addr:street")])),
            tags.get("addr:city"),
            tags.get("addr:postcode"),
        ]))
        website = (tags.get("website") or tags.get("contact:website") or "").strip()
        phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
        category = tags.get("cuisine") or tags.get("amenity") or tags.get("shop") \
            or tags.get("craft") or tags.get("office") or niche
        out.append(RawProspect(
            name=name,
            category=str(category),
            phone=phone,
            website=website,
            address=address,
            city=(tags.get("addr:city") or city).title(),
            state=tags.get("addr:state", ""),
            maps_url=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
            country=(tags.get("addr:country") or country or "").strip().upper()[:2],
            emails=[tags["email"]] if tags.get("email") else (
                [tags["contact:email"]] if tags.get("contact:email") else []),
            source="osm",
        ))
    return out


class OsmProvider(ProspectProvider):
    name = "osm"

    def __init__(self) -> None:
        self.last_skipped_known = 0
        self.last_exhausted = False

    def search(self, query: str, limit: int,
               exclude_keys: set[str] | None = None,
               progress=None) -> list[RawProspect]:
        """Query format: '<niche> <city...>' e.g. 'restaurants giza egypt'."""
        exclude_keys = exclude_keys or set()
        self.last_skipped_known = 0
        self.last_exhausted = False

        parts = query.strip().split()
        if len(parts) < 2:
            raise ValueError("OSM query needs '<niche> <city>', e.g. "
                             "'restaurants giza egypt'")
        # Greedy niche match: try the longest known niche prefix first.
        niche, city = parts[0], " ".join(parts[1:])
        two_word = " ".join(parts[:2]).lower()
        if two_word in NICHE_TAGS and len(parts) >= 3:
            niche, city = two_word, " ".join(parts[2:])

        tag_filters, matched = tags_for_niche(niche)
        if not matched:
            log.info("OSM: unknown niche '%s', falling back to name-substring match",
                     niche)

        cache_key = f"overpass_{niche}_{city}"
        data = _cache_get(cache_key)
        country = ""
        with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
            area_id, country = resolve_area(client, city)  # cached; also gives country
            if data is None:
                if not area_id:
                    raise ValueError(f"OSM could not resolve area for '{city}'")
                overpass_query = build_query(area_id, tag_filters, limit)
                data = _request_json(client, "POST", OVERPASS_URL,
                                     data={"data": overpass_query})
                _cache_put(cache_key, data)

        candidates = parse_elements((data or {}).get("elements"), niche, city, country)
        results: list[RawProspect] = []
        for raw in candidates:
            if raw.dedupe_key in exclude_keys:
                self.last_skipped_known += 1
                continue
            results.append(raw)
            if len(results) >= limit:
                break
        else:
            self.last_exhausted = True
        log.info("OSM: %d candidates, %d returned, %d skipped known%s",
                 len(candidates), len(results), self.last_skipped_known,
                 ", exhausted" if self.last_exhausted else "")
        return results
