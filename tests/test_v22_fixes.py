"""v2.2 Phase 1: the four v2.1 defects, each with a test that fails on the old code."""
import contextlib
import logging

import pytest

from engine.providers.base import (country_from_address, tlds_for_country)


@contextlib.contextmanager
def capture_logs(logger_name: str):
    """Collect records from one logger via its own handler.

    Deliberately not caplog: caplog depends on root-logger level/propagation,
    which other tests in this suite mutate (setup_logging), so it passes alone
    and fails in a full run. A dedicated handler is immune to that.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collector(level=logging.DEBUG)
    previous_level, previous_disabled = logger.level, logging.root.manager.disable
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logging.disable(previous_disabled)


# ── 1.1 VALID_PROVIDERS omitted osm/places, so saving either was discarded ──

def test_valid_providers_covers_every_registered_provider():
    from engine.providers import PROVIDERS
    from engine.prospector_settings import valid_providers

    assert set(valid_providers()) == set(PROVIDERS)
    assert "osm" in valid_providers()
    assert "places" in valid_providers()


def test_saving_osm_and_places_persists(session):
    import engine.prospector_settings as ps

    for name in ("osm", "places"):
        ps.save_overrides(session, {"provider": name})
        ps.invalidate()
        assert ps.eff_provider() == name, f"{name} was silently discarded"


def test_invalid_provider_is_rejected_loudly(session):
    import engine.prospector_settings as ps

    ps.save_overrides(session, {"provider": "registry"})
    ps.invalidate()
    with capture_logs("prospector.settings") as records:
        stored = ps.save_overrides(session, {"provider": "not_a_provider"})
    assert "provider" not in stored          # rejected...
    assert any("not_a_provider" in r.getMessage() for r in records)  # ...and logged


# ── 1.3 the 'no website' filter destroyed 100% of international results ──────

def test_require_website_false_keeps_websiteless_prospects():
    from engine.prospector import _filter_reason
    from engine.providers.base import RawProspect

    raw = RawProspect(name="Norte Grill", city="Rio de Janeiro", country="BR")
    assert _filter_reason(raw, require_website=True) == "no website"
    assert _filter_reason(raw, require_website=False) is None


def test_require_website_setting_roundtrip(session):
    import engine.prospector_settings as ps

    assert ps.eff_require_website() is True          # default preserves v2.1
    ps.save_overrides(session, {"require_website": False})
    ps.invalidate()
    assert ps.eff_require_website() is False


# ── 1.4 guess_domain() hardcoded .com and could not work outside the US ──────

def test_country_tlds():
    assert tlds_for_country("BR")[0] == "com.br"
    assert tlds_for_country("EG")[0] == "com.eg"
    assert tlds_for_country("GB")[0] == "co.uk"
    assert "com" in tlds_for_country("BR")     # universal fallback always present
    assert tlds_for_country(None) == ["com"]   # unknown country still works
    assert tlds_for_country("ZZ") == ["com"]


def test_country_from_address():
    assert country_from_address("R. X, 12, Rio de Janeiro, Brazil") == "BR"
    assert country_from_address("1 Main St, Tampa, FL 33601, USA") == "US"
    assert country_from_address("Giza, Egypt") == "EG"
    assert country_from_address("") == ""
    assert country_from_address("nowhere in particular") == ""


def test_domain_candidates_are_country_aware_and_capped():
    from engine.providers.registry import MAX_DOMAIN_PROBES, domain_candidates

    br = domain_candidates("Norte Grill", "BR")
    assert br[0] == "nortegrill.com.br", br
    assert len(br) <= MAX_DOMAIN_PROBES
    assert any(h.endswith(".com") for h in br)   # .com fallback still reachable

    us = domain_candidates("Norte Grill", "US")
    assert us[0] == "nortegrill.com"

    assert domain_candidates("LLC Inc Co", "BR") == []  # all stopwords -> nothing


def test_guess_domain_probes_country_tld_first_and_stops_at_cap():
    """The .com.br host must be tried before .com, and never more than 4 probes."""
    import httpx

    from engine.providers.registry import MAX_DOMAIN_PROBES, guess_domain

    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(str(request.url))
        if request.url.host == "nortegrill.com.br":
            return httpx.Response(200, text="<html>Norte Grill churrascaria</html>")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert guess_domain(client, "Norte Grill", "BR") == "https://nortegrill.com.br"
    assert tried[0].rstrip("/") == "https://nortegrill.com.br"

    tried.clear()
    assert guess_domain(client, "Nonexistent Bakery Place", "BR") == ""
    assert len(tried) <= MAX_DOMAIN_PROBES


# ── 1.5 Places: pagination, per-page billing, cap, cost documentation ────────

def test_places_field_mask_documents_enterprise_tier():
    from engine.providers import places

    assert "nextPageToken" in places.FIELD_MASK
    assert "ENTERPRISE" in places.SKU_LABEL.upper()
    assert "Basic-tier fields only" not in places.__doc__
    assert "ENTERPRISE" in places.__doc__.upper()


def test_places_cap_default_lowered():
    from engine.config import Settings

    assert Settings().places_daily_call_cap == 30


def test_places_paginates_and_bills_every_page(session, monkeypatch):
    """45 requested -> 3 pages -> 3 recorded calls, results concatenated."""
    import httpx

    from engine.providers import places

    monkeypatch.setenv("PLACES_API_KEY", "test-key")
    monkeypatch.setenv("PLACES_DAILY_CALL_CAP", "10")
    from engine.config import get_settings
    get_settings.cache_clear()

    pages = [
        {"places": [{"displayName": {"text": f"Biz {i}"},
                     "formattedAddress": f"{i} Rua X, Rio de Janeiro, Brazil",
                     "websiteUri": f"https://biz{i}.com.br"} for i in range(20)],
         "nextPageToken": "tok2"},
        {"places": [{"displayName": {"text": f"Biz {i}"},
                     "formattedAddress": f"{i} Rua X, Rio de Janeiro, Brazil"}
                    for i in range(20, 40)],
         "nextPageToken": "tok3"},
        {"places": [{"displayName": {"text": f"Biz {i}"},
                     "formattedAddress": f"{i} Rua X, Rio de Janeiro, Brazil"}
                    for i in range(40, 50)]},
    ]
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        seen_tokens.append(body.get("pageToken", ""))
        return httpx.Response(200, json=pages[len(seen_tokens) - 1])

    real_client = httpx.Client  # capture before patching, else infinite recursion
    monkeypatch.setattr(places.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))

    provider = places.PlacesProvider()
    results = provider.search("restaurants rio de janeiro", limit=45)

    assert len(results) == 45
    assert seen_tokens == ["", "tok2", "tok3"]        # followed the tokens
    assert places.calls_today(session) == 3           # every page billed
    assert results[0].country == "BR"                 # country parsed from address


def test_places_stops_cleanly_at_cap_mid_run(session, monkeypatch):
    """Cap reached between pages: keep the partial result, do not spend more."""
    import httpx

    from engine.providers import places

    monkeypatch.setenv("PLACES_API_KEY", "test-key")
    monkeypatch.setenv("PLACES_DAILY_CALL_CAP", "1")
    from engine.config import get_settings
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "places": [{"displayName": {"text": f"Biz {i}"},
                        "formattedAddress": "Rio de Janeiro, Brazil"}
                       for i in range(20)],
            "nextPageToken": "tok2"})

    real_client = httpx.Client  # capture before patching, else infinite recursion
    monkeypatch.setattr(places.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))

    results = places.PlacesProvider().search("restaurants rio de janeiro", limit=45)
    assert len(results) == 20                    # one page, then the cap stopped it
    assert places.calls_today(session) == 1


def test_places_raises_when_cap_already_exhausted(session, monkeypatch):
    from engine.providers import places
    from engine.state import set_state

    monkeypatch.setenv("PLACES_API_KEY", "test-key")
    monkeypatch.setenv("PLACES_DAILY_CALL_CAP", "1")
    from engine.config import get_settings
    get_settings.cache_clear()

    set_state(session, places._today_key(), "5")
    with pytest.raises(places.PlacesCapReached):
        places.PlacesProvider().search("restaurants rio de janeiro", limit=10)


def test_places_over_ceiling_warns_not_exhausted(session, monkeypatch):
    import httpx

    from engine.providers import places

    monkeypatch.setenv("PLACES_API_KEY", "test-key")
    monkeypatch.setenv("PLACES_DAILY_CALL_CAP", "10")
    from engine.config import get_settings
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "places": [{"displayName": {"text": f"B{i}"},
                        "formattedAddress": "Rio de Janeiro, Brazil"}
                       for i in range(20)],
            "nextPageToken": "more"})

    real_client = httpx.Client  # capture before patching, else infinite recursion
    monkeypatch.setattr(places.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))

    with capture_logs("prospector.places") as records:
        provider = places.PlacesProvider()
        results = provider.search("restaurants rio", limit=100)
    assert len(results) == 60                     # Google's ceiling
    assert not provider.last_exhausted            # a ceiling is NOT exhaustion
    assert any("ceiling" in r.getMessage() for r in records)
