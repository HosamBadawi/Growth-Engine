"""RegistryProvider: query parsing, CSV row filtering, website discovery rules."""
import pytest

from engine.providers import registry
from engine.providers.registry import (STATE_SOURCES, _first_allowed,
                                       _iter_rows_for_trade, _title_case_business,
                                       _title_case_owner, parse_registry_query)


def _rows_for_trade(source, trade, city, limit=10):
    """Compat shim for these tests: collect up to limit from the lazy iterator."""
    out = []
    for raw in _iter_rows_for_trade(source, trade, city, seen=set()):
        out.append(raw)
        if len(out) >= limit:
            break
    return out

FIXTURE_ROWS = [
    # board, prefix, owner, business, x, addr, x, x, city, state, zip, county,
    # lic#, type, status, d1, d2, expiry, x, x, full_license, x
    '"06","CAC","RIVERA, MIKE J","COOL BREEZE AIR LLC","","123 PALM AVE","","","TAMPA","FL","33601","39","0001","C","A","","01/01/2015","08/31/2026","","","CAC0001",""',
    '"06","CAC","SMITH, ANN","","","1 MAIN ST","","","TAMPA","FL","33602","39","0002","C","A","","01/01/2015","08/31/2026","","","CAC0002",""',  # no DBA -> skipped
    '"06","CAC","OLD, GUY","EXPIRED AIR INC","","9 DEAD END","","","TAMPA","FL","33603","39","0003","C","I","","01/01/2001","08/31/2010","","","CAC0003",""',  # inactive
    '"06","CFC","JONES, PAT","TAMPA DRAIN KINGS INC","","55 PIPE RD","","","TAMPA","FL","33604","39","0004","C","A","","01/01/2018","08/31/2026","","","CFC0004",""',  # plumbing, not hvac
    '"06","CAC","MIAMI, MAN","SOUTH AIR CORP","","77 OCEAN DR","","","MIAMI","FL","33101","23","0005","C","A","","01/01/2019","08/31/2026","","","CAC0005",""',  # wrong city
    '"06","CAC","DUPE, DAN","COOL BREEZE AIR LLC","","123 PALM AVE","","","TAMPA","FL","33601","39","0001","C","A","","01/01/2015","08/31/2026","","","CAC0001",""',  # duplicate license
]


@pytest.fixture
def fl_fixture(tmp_path, monkeypatch):
    path = tmp_path / "fl_construction_1.csv"
    path.write_text("\n".join(FIXTURE_ROWS), encoding="latin-1")
    monkeypatch.setattr(registry, "ensure_extract", lambda source: path)
    return path


def test_parse_registry_query():
    assert parse_registry_query("hvac tampa fl") == ("hvac", "tampa", "FL")
    assert parse_registry_query("plumber saint petersburg fl") == ("plumber", "saint petersburg", "FL")
    assert parse_registry_query("hvac tampa") == ("hvac", "tampa", "FL")
    assert parse_registry_query("electrician orlando FL") == ("electrician", "orlando", "FL")


def test_rows_filtered_by_trade_city_status_and_deduped(fl_fixture):
    source = STATE_SOURCES["FL"][0]
    rows = _rows_for_trade(source, "hvac", "tampa", limit=10)
    assert len(rows) == 1  # active + Tampa + CAC + has DBA + deduped
    prospect = rows[0]
    assert prospect.name == "Cool Breeze Air LLC"
    assert prospect.owner_name == "Mike J Rivera"
    assert prospect.license_no == "CAC0001"
    assert prospect.city == "Tampa"
    assert prospect.source == "registry:FL"


def test_rows_plumber_prefix(fl_fixture):
    source = STATE_SOURCES["FL"][0]
    rows = _rows_for_trade(source, "plumber", "tampa", limit=10)
    assert [r.name for r in rows] == ["Tampa Drain Kings Inc"]


def test_unknown_trade_returns_empty(fl_fixture):
    source = STATE_SOURCES["FL"][0]
    assert _rows_for_trade(source, "landscaping", "tampa", 10) == []


def test_title_casing():
    assert _title_case_owner("ERGLE, GERALD K") == "Gerald K Ergle"
    assert _title_case_business("GKE, LLC") == "Gke, LLC"
    assert _title_case_business("TAMPA BAY PLUMBING PROS INC") == "Tampa Bay Plumbing Pros Inc"


def test_distinctive_tokens_for_domain_guessing():
    from engine.providers.registry import _distinctive_tokens

    assert _distinctive_tokens("Cool Breeze Air LLC") == ["cool", "breeze", "air"]
    assert _distinctive_tokens("The Clean Plumbers, Inc.") == ["clean", "plumbers"]
    assert _distinctive_tokens("A & E Service Co") == ["e"]


def test_website_discovery_skips_directories():
    hrefs = [
        "https://www.yelp.com/biz/grable-plumbing",
        "https://www.superpages.com/tampa-fl/x",
        "https://grableplumbing.com/",
        "https://www.facebook.com/grable",
    ]
    assert _first_allowed(hrefs) == "https://grableplumbing.com/"
    assert _first_allowed(["https://www.bbb.org/x", "https://angi.com/y"]) == ""
    # DuckDuckGo redirect-wrapped URLs get unwrapped
    wrapped = ["//duckduckgo.com/l/?uddg=https%3A%2F%2Fcoolbreezeair.com%2F&rut=abc"]
    assert _first_allowed(wrapped) == "https://coolbreezeair.com/"
