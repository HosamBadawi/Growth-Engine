"""Phase 2: incremental cache flush survives interrupt, negative-cache TTL,
progress emitted, capped miss-path cost."""
import json
from datetime import timedelta

import pytest

import engine.providers.registry as reg
from engine.providers.base import RawProspect
from engine.util import utcnow


@pytest.fixture(autouse=True)
def registry_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(reg, "REGISTRY_DIR", tmp_path)
    monkeypatch.setattr(reg, "_WEBSITE_CACHE_FILE", tmp_path / "website_cache.json")
    monkeypatch.setattr(reg, "_MISS_CACHE_FILE", tmp_path / "website_misses.json")
    monkeypatch.setattr(reg, "STATE_SOURCES", {"FL": [object()]})
    yield tmp_path


def _fake_iter(rows):
    def it(source, trade, city, seen):
        for r in rows:
            if r.dedupe_key not in seen:
                seen.add(r.dedupe_key)
                yield r
    return it


def test_negative_cache_ttl(registry_tmp):
    fresh = utcnow().isoformat()
    stale = (utcnow() - timedelta(days=40)).isoformat()
    (registry_tmp / "website_misses.json").write_text(
        json.dumps({"keepkey": fresh, "dropkey": stale}), encoding="utf-8")
    loaded = reg._load_miss_cache()
    assert "keepkey" in loaded
    assert "dropkey" not in loaded  # older than 30-day TTL, pruned


def test_atomic_write_roundtrip(registry_tmp):
    reg._save_website_cache({"a": "https://a.example"})
    assert reg._load_website_cache() == {"a": "https://a.example"}
    # no leftover temp file
    assert not list(registry_tmp.glob("*.tmp"))


def test_incremental_flush_survives_interrupt(registry_tmp, monkeypatch):
    """A crash mid-run must leave already-discovered domains on disk."""
    monkeypatch.setattr(reg, "_FLUSH_EVERY", 2)
    rows = [RawProspect(name=f"Co{i}", city="Tampa", state="FL", license_no=f"CAC{i}")
            for i in range(6)]
    monkeypatch.setattr(reg, "_iter_rows_for_trade", _fake_iter(rows))

    calls = {"n": 0}

    def guess(client, name):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt  # simulate Ctrl-C mid-run
        return f"https://{name.lower()}.example"

    monkeypatch.setattr(reg, "guess_domain", guess)

    provider = reg.RegistryProvider()
    with pytest.raises(KeyboardInterrupt):
        provider.search("hvac tampa fl", limit=6, exclude_keys=set())

    # the finally-block flush persisted the first 3 resolved domains
    cache = reg._load_website_cache()
    assert len(cache) >= 3
    assert "cac0" in cache


def test_progress_callback_emits(registry_tmp, monkeypatch):
    monkeypatch.setattr(reg, "_FLUSH_EVERY", 10)
    rows = [RawProspect(name=f"Co{i}", city="Tampa", state="FL", license_no=f"CAC{i}")
            for i in range(12)]
    monkeypatch.setattr(reg, "_iter_rows_for_trade", _fake_iter(rows))
    # every candidate is a miss so all 12 are "checked"
    monkeypatch.setattr(reg, "guess_domain", lambda c, n: "")
    monkeypatch.setattr(reg, "search_website", lambda *a, **k: "")
    monkeypatch.setattr(reg.time, "sleep", lambda s: None)

    lines = []
    provider = reg.RegistryProvider()
    provider.search("hvac tampa fl", limit=12, exclude_keys=set(),
                    progress=lines.append)
    assert any("Discovering websites" in ln for ln in lines)


def test_known_miss_skips_search(registry_tmp, monkeypatch):
    """A cached miss must not trigger another search within the TTL."""
    (registry_tmp / "website_misses.json").write_text(
        json.dumps({"cac0": utcnow().isoformat()}), encoding="utf-8")
    rows = [RawProspect(name="Co0", city="Tampa", state="FL", license_no="CAC0")]
    monkeypatch.setattr(reg, "_iter_rows_for_trade", _fake_iter(rows))

    from unittest.mock import MagicMock
    search_spy = MagicMock(return_value="")
    guess_spy = MagicMock(return_value="")
    monkeypatch.setattr(reg, "search_website", search_spy)
    monkeypatch.setattr(reg, "guess_domain", guess_spy)

    provider = reg.RegistryProvider()
    provider.search("hvac tampa fl", limit=5, exclude_keys=set())
    guess_spy.assert_not_called()
    search_spy.assert_not_called()
