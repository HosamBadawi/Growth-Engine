"""Researcher: the personalization quality gate."""
from engine.researcher import is_clean_detail


def test_rejects_the_literal_observed_failure():
    # This exact string once reached a draft as if it were a compliment.
    assert not is_clean_detail("best of the bay area sponsored search", {})


def test_rejects_ad_and_seo_noise():
    assert not is_clean_detail("top result on google for plumbers", {})
    assert not is_clean_detail("their ppc ads stood out", {})
    assert not is_clean_detail("ranked #1 on yelp listing", {})
    assert not is_clean_detail("great local ad campaign", {})


def test_rejects_unbacked_superlatives():
    assert not is_clean_detail("the best plumber in tampa", {})
    assert not is_clean_detail("leading hvac provider", {"rating": None})
    assert not is_clean_detail("top rated company", {})


def test_allows_superlative_backed_by_countable_fact():
    facts = {"rating": 4.9, "review_count": 120}
    assert is_clean_detail("top rated with 4.9 stars across 120 reviews", facts)


def test_rejects_empty_and_overlong():
    assert not is_clean_detail("", {})
    assert not is_clean_detail("   ", {})
    assert not is_clean_detail(
        "a very long rambling detail that goes on and on well past ten words total", {}
    )


def test_allows_clean_details():
    assert is_clean_detail("20 years serving Tampa families", {})
    assert is_clean_detail("family owned since 2003", {})
    assert is_clean_detail("4.9 stars across 120 reviews", {"rating": 4.9})


def test_word_ad_does_not_match_inside_words():
    # "adams" must not trip the \bads?\b noise rule
    assert is_clean_detail("run by the Adams family since 1990", {})


# ── researcher v2: mode selection, cache TTL, organic filtering ──────────────

def test_mode_selects_provider(monkeypatch):
    from engine.config import get_settings
    from engine.research_web import WebResearcher
    from engine.researcher import LocalOllamaResearcher, get_researcher

    monkeypatch.setenv("RESEARCH_MODE", "local")
    get_settings.cache_clear()
    assert isinstance(get_researcher(), LocalOllamaResearcher)

    monkeypatch.setenv("RESEARCH_MODE", "web")
    get_settings.cache_clear()
    assert isinstance(get_researcher(), WebResearcher)


def test_card_cache_ttl():
    from datetime import timedelta

    from engine.researcher import _card_is_fresh
    from engine.util import utcnow

    assert not _card_is_fresh({})
    assert _card_is_fresh({"card": {"human_detail": "x"}})  # legacy, no stamp
    fresh = (utcnow() - timedelta(days=2)).isoformat()
    stale = (utcnow() - timedelta(days=8)).isoformat()
    card = {"human_detail": "x"}
    assert _card_is_fresh({"card": card, "card_fetched_at": fresh})
    assert not _card_is_fresh({"card": card, "card_fetched_at": stale})


def test_web_search_skips_ads_and_socials():
    from engine.research_web import _is_organic

    assert _is_organic("https://tampabaytimes.com/best-plumbers")
    assert not _is_organic("https://www.facebook.com/someplumber")
    assert not _is_organic("https://ad.example/?gclid=abc")
    assert not _is_organic("https://googleadservices.com/pagead/x")
    assert not _is_organic("//relative-nonsense")


def test_web_queries_are_targeted(prospect):
    from engine.research_web import _search_queries

    queries = _search_queries(prospect)
    assert any("reviews" in q for q in queries)
    assert any("owner" in q for q in queries)
    assert all(prospect.name in q or "example" in q for q in queries)
    assert len(queries) <= 4
