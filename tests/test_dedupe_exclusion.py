"""Phase 1: exclusion set filters BEFORE website discovery; dedupe_key correctness."""
from unittest.mock import MagicMock

import pytest

from db.models import Prospect, ProspectStatus
from engine.providers.base import RawProspect, make_dedupe_key


def test_dedupe_key_prefers_license_then_name_city():
    assert make_dedupe_key("CAC123", "Acme AC", "Tampa") == "cac123"
    assert make_dedupe_key("", "Acme AC", "Tampa") == "acme ac|tampa"
    assert make_dedupe_key(None, "Acme AC", "Tampa") == "acme ac|tampa"
    # RawProspect property matches the helper
    raw = RawProspect(name="Acme AC", city="Tampa", license_no="CAC123")
    assert raw.dedupe_key == "cac123"


def test_known_dedupe_keys_query(session):
    from engine.prospector import known_dedupe_keys

    session.add_all([
        Prospect(name="A", dedupe_key="cac1", status=ProspectStatus.NEW),
        Prospect(name="B", dedupe_key="cfc2", status=ProspectStatus.NEW),
        Prospect(name="C", dedupe_key=None, status=ProspectStatus.NEW),
    ])
    session.commit()
    assert known_dedupe_keys(session) == {"cac1", "cfc2"}


def test_registry_excludes_before_discovery(monkeypatch):
    """The core Phase 1 guarantee: no discovery call for an excluded business."""
    import engine.providers.registry as reg

    rows = [
        RawProspect(name="Alpha AC", city="Tampa", state="FL", license_no="CAC1"),
        RawProspect(name="Beta AC", city="Tampa", state="FL", license_no="CAC2"),
        RawProspect(name="Gamma AC", city="Tampa", state="FL", license_no="CAC3"),
    ]

    def fake_iter(source, trade, city, seen):
        for r in rows:
            if r.dedupe_key in seen:
                continue
            seen.add(r.dedupe_key)
            yield r

    monkeypatch.setattr(reg, "_iter_rows_for_trade", fake_iter)
    monkeypatch.setattr(reg, "STATE_SOURCES", {"FL": [object()]})
    monkeypatch.setattr(reg, "_load_website_cache", lambda: {})
    monkeypatch.setattr(reg, "_load_miss_cache", lambda: {})
    monkeypatch.setattr(reg, "_save_website_cache", lambda c: None)
    monkeypatch.setattr(reg, "_save_miss_cache", lambda c: None)

    guess_spy = MagicMock(return_value="https://found.example")
    monkeypatch.setattr(reg, "guess_domain", guess_spy)
    monkeypatch.setattr(reg, "search_website", MagicMock(return_value=""))

    provider = reg.RegistryProvider()
    # CAC1 already known -> must be skipped with zero discovery spend on it
    results = provider.search("hvac tampa fl", limit=5, exclude_keys={"cac1"})

    assert provider.last_skipped_known == 1
    discovered_names = [call.args[1] for call in guess_spy.call_args_list]
    assert "Alpha AC" not in discovered_names  # excluded, never discovered
    assert set(discovered_names) == {"Beta AC", "Gamma AC"}
    assert {r.name for r in results} == {"Beta AC", "Gamma AC"}


def test_registry_reports_exhaustion(monkeypatch):
    import engine.providers.registry as reg

    rows = [RawProspect(name="Only One", city="Tampa", state="FL", license_no="CAC9")]

    def fake_iter(source, trade, city, seen):
        for r in rows:
            if r.dedupe_key not in seen:
                seen.add(r.dedupe_key)
                yield r

    monkeypatch.setattr(reg, "_iter_rows_for_trade", fake_iter)
    monkeypatch.setattr(reg, "STATE_SOURCES", {"FL": [object()]})
    monkeypatch.setattr(reg, "_load_website_cache", lambda: {})
    monkeypatch.setattr(reg, "_load_miss_cache", lambda: {})
    monkeypatch.setattr(reg, "_save_website_cache", lambda c: None)
    monkeypatch.setattr(reg, "_save_miss_cache", lambda c: None)
    monkeypatch.setattr(reg, "guess_domain", lambda c, n, country=None: "https://x.example")

    provider = reg.RegistryProvider()
    # ask for 10 but the city has 1 -> exhausted
    provider.search("hvac tampa fl", limit=10, exclude_keys=set())
    assert provider.last_exhausted is True


def test_run_prospecting_persists_dedupe_key_and_skips_known(session, monkeypatch):
    import engine.prospector as prospector

    class FakeProvider:
        name = "registry"
        last_skipped_known = 3
        last_exhausted = False

        def search(self, query, limit, exclude_keys=None, progress=None):
            assert exclude_keys is not None  # prospector must pass it
            return [RawProspect(name="New Co", city="tampa", state="FL",
                                website="https://newco.example", license_no="CAC77")]

    monkeypatch.setattr(prospector, "get_provider", lambda name: FakeProvider())
    monkeypatch.setattr("engine.prospector_settings.eff_provider", lambda: "registry")

    summary = prospector.run_prospecting(session, "hvac", "tampa", 10)
    assert summary["skipped_known"] == 3
    assert summary["kept"] == 1
    stored = session.query(Prospect).filter_by(name="New Co").one()
    assert stored.dedupe_key == "cac77"
