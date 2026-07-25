"""PlacesProvider — Google Places API (New) Text Search.

The legitimate, supported, terms-compliant way to get Google's business data.
Off by default: requires PLACES_API_KEY, and every call costs money, so a hard
persisted daily cap (default 200) stops the spend rather than silently
continuing. Every call is logged with its SKU tier so cost is auditable from
the Activity page.

Cost control: the field mask below requests ONLY what the engine needs. The
mask is the single biggest cost lever on this API — never request the default
full payload. Pricing moves; the operator must confirm the current free
allowance in their own Cloud Console before enabling this provider (README).
"""
import logging
from datetime import datetime, timezone

import httpx

from engine.config import get_settings
from engine.providers.base import ProspectProvider, RawProspect, parse_city_state

log = logging.getLogger("prospector.places")

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Basic-tier fields only (displayName/address/website/phone/rating live in the
# Pro tier of Text Search; this mask keeps the call in one predictable SKU).
FIELD_MASK = ",".join([
    "places.displayName",
    "places.formattedAddress",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.rating",
    "places.userRatingCount",
    "places.googleMapsUri",
    "places.primaryType",
])
SKU_LABEL = "Places TextSearch (fieldmask: display/address/website/phone/rating)"
_CAP_STATE_PREFIX = "places_calls_"  # + YYYYMMDD, persisted in engine_state


def _today_key() -> str:
    return _CAP_STATE_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%d")


def calls_today(session) -> int:
    from engine.state import get_state

    return int(get_state(session, _today_key(), "0") or 0)


def _record_call(session, count: int = 1) -> int:
    from engine.events import log_event
    from engine.state import get_state, set_state

    used = int(get_state(session, _today_key(), "0") or 0) + count
    set_state(session, _today_key(), str(used))
    log_event(session, "places",
              f"Places API call {used}/{get_settings().places_daily_call_cap} "
              f"today — SKU: {SKU_LABEL}")
    return used


class PlacesCapReached(RuntimeError):
    pass


class PlacesProvider(ProspectProvider):
    name = "places"

    def __init__(self) -> None:
        self.last_skipped_known = 0
        self.last_exhausted = False

    def search(self, query: str, limit: int,
               exclude_keys: set[str] | None = None,
               progress=None) -> list[RawProspect]:
        settings = get_settings()
        if not settings.places_api_key:
            raise ValueError(
                "Google Places provider is unavailable: PLACES_API_KEY is not set. "
                "Add it in .env, or use the free 'osm'/'registry' providers."
            )
        exclude_keys = exclude_keys or set()
        self.last_skipped_known = 0
        self.last_exhausted = False

        from db.session import new_session

        session = new_session()
        try:
            used = calls_today(session)
            if used >= settings.places_daily_call_cap:
                raise PlacesCapReached(
                    f"Places daily call cap reached ({used}/"
                    f"{settings.places_daily_call_cap}). Stopping rather than "
                    "spending more; the counter resets at midnight UTC."
                )

            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": settings.places_api_key,
                "X-Goog-FieldMask": FIELD_MASK,
            }
            body = {"textQuery": query, "pageSize": min(max(limit, 1), 20)}
            with httpx.Client(timeout=30) as client:
                resp = client.post(SEARCH_URL, json=body, headers=headers)
                _record_call(session)
                resp.raise_for_status()
                data = resp.json()

            results: list[RawProspect] = []
            for place in data.get("places", []):
                name = ((place.get("displayName") or {}).get("text") or "").strip()
                if not name:
                    continue
                address = place.get("formattedAddress", "")
                city, state = parse_city_state(address)
                raw = RawProspect(
                    name=name,
                    category=place.get("primaryType", ""),
                    phone=place.get("nationalPhoneNumber", ""),
                    website=place.get("websiteUri", ""),
                    address=address,
                    city=city,
                    state=state,
                    rating=place.get("rating"),
                    review_count=place.get("userRatingCount"),
                    maps_url=place.get("googleMapsUri", ""),
                    source="places",
                )
                if raw.dedupe_key in exclude_keys:
                    self.last_skipped_known += 1
                    continue
                results.append(raw)
                if len(results) >= limit:
                    break
            log.info("Places: %d returned, %d skipped known (call %d/%d today)",
                     len(results), self.last_skipped_known,
                     calls_today(session), settings.places_daily_call_cap)
            return results
        finally:
            session.close()