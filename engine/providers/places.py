"""PlacesProvider: Google Places API (New) Text Search.

The legitimate, supported, terms-compliant way to get Google's business data.
Off by default: requires PLACES_API_KEY, and every call costs money, so a hard
persisted daily cap (default 200) stops the spend rather than silently
continuing. Every call is logged with its SKU tier so cost is auditable from
the Activity page.

Cost control: the field mask below requests ONLY what the engine needs. The
mask is the single biggest cost lever on this API. Never request the default
full payload.

READ THIS BEFORE CHANGING THE MASK. Text Search (New) bills by the highest SKU
any requested field belongs to. displayName / formattedAddress / googleMapsUri /
primaryType are Pro-tier, but websiteUri / nationalPhoneNumber / rating /
userRatingCount are ENTERPRISE-tier, so every call this provider makes bills at
the Enterprise rate. That is unavoidable, not a misconfiguration: website and
phone are the whole point of the provider and no cheaper tier exposes them.
Dropping to Pro would mean giving up contact data entirely.

Because Enterprise carries the smallest free monthly allowance of the three
tiers, the daily cap defaults low (30). Pricing and allowances move and public
sources disagree, so the operator must confirm current figures in their own
Cloud Console before enabling this provider (see README).
"""
import logging
from datetime import datetime, timezone

import httpx

from engine.config import get_settings
from engine.providers.base import (ProspectProvider, RawProspect,
                                   country_from_address, parse_city_state)

log = logging.getLogger("prospector.places")

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# ENTERPRISE SKU: see the module docstring. websiteUri/nationalPhoneNumber/
# rating/userRatingCount are Enterprise fields and set the billing tier for the
# whole call; the rest are Pro. nextPageToken is free to request.
FIELD_MASK = ",".join([
    "places.displayName",       # Pro
    "places.formattedAddress",  # Pro
    "places.googleMapsUri",     # Pro
    "places.primaryType",       # Pro
    "places.websiteUri",        # Enterprise
    "places.nationalPhoneNumber",  # Enterprise
    "places.rating",            # Enterprise
    "places.userRatingCount",   # Enterprise
    "nextPageToken",
])
SKU_LABEL = "Places TextSearch (ENTERPRISE tier: website/phone/rating requested)"
PAGE_SIZE = 20        # Google's per-page maximum
MAX_RESULTS = 60      # Google's hard ceiling: 3 pages x 20
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
              f"today, SKU: {SKU_LABEL}")
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
            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": settings.places_api_key,
                "X-Goog-FieldMask": FIELD_MASK,
            }
            wanted = min(max(limit, 1), MAX_RESULTS)
            if limit > MAX_RESULTS:
                log.warning(
                    "Places: asked for %d but the API ceiling is %d results "
                    "(3 pages x %d). Returning at most %d. This is an API "
                    "limit, NOT exhaustion of the area.",
                    limit, MAX_RESULTS, PAGE_SIZE, MAX_RESULTS)

            results: list[RawProspect] = []
            page_token = ""
            cap_hit = False
            with httpx.Client(timeout=30) as client:
                while len(results) < wanted:
                    # Every page is a separately billed call: check before each.
                    used = calls_today(session)
                    if used >= settings.places_daily_call_cap:
                        cap_hit = True
                        if results:
                            log.warning(
                                "Places daily cap reached (%d/%d) mid-run; "
                                "returning %d results found so far.",
                                used, settings.places_daily_call_cap, len(results))
                            break
                        raise PlacesCapReached(
                            f"Places daily call cap reached ({used}/"
                            f"{settings.places_daily_call_cap}). Stopping rather "
                            "than spending more; the counter resets at midnight UTC."
                        )

                    body = {"textQuery": query,
                            "pageSize": min(wanted - len(results), PAGE_SIZE)}
                    if page_token:
                        body["pageToken"] = page_token
                    resp = client.post(SEARCH_URL, json=body, headers=headers)
                    _record_call(session)  # record even on error: Google billed it
                    resp.raise_for_status()
                    data = resp.json()

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
                            country=country_from_address(address),
                            source="places",
                        )
                        if raw.dedupe_key in exclude_keys:
                            self.last_skipped_known += 1
                            continue
                        results.append(raw)
                        if len(results) >= wanted:
                            break

                    page_token = data.get("nextPageToken") or ""
                    if not page_token:
                        # No more pages: the query itself is exhausted, which is
                        # different from hitting our cap or Google's ceiling.
                        self.last_exhausted = len(results) < limit and not cap_hit
                        break

            log.info("Places: %d returned, %d skipped known (%d calls used today "
                     "of %d cap)%s", len(results), self.last_skipped_known,
                     calls_today(session), settings.places_daily_call_cap,
                     ", CAP REACHED" if cap_hit else "")
            return results
        finally:
            session.close()