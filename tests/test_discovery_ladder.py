"""Phase 2: the contact discovery ladder. Fixture-driven, no network."""
import httpx
import pytest

from engine.discovery import (AGGREGATOR_DOMAINS, DiscoveryBudget,
                              DiscoveryCache, DiscoveryInput, DiscoveryResult,
                              MIN_WEBSITE_SCORE, classify_social,
                              discover_contacts, extract_emails, extract_phone,
                              extract_social, fetch_linkinbio, is_aggregator,
                              is_linkinbio, normalise_phone, rank_emails,
                              robots_allows, score_candidate)


class FakeDDGS:
    """Stand-in for ddgs: returns canned hits per query, records what was asked."""

    def __init__(self, hits_by_query=None, default=None):
        self.hits_by_query = hits_by_query or {}
        self.default = default or []
        self.queries: list[str] = []

    def text(self, query, backend=None, max_results=5):
        self.queries.append(query)
        for needle, hits in self.hits_by_query.items():
            if needle in query:
                return [{"href": h} for h in hits]
        return [{"href": h} for h in self.default]


def client_for(routes: dict, record: list | None = None) -> httpx.Client:
    """Mock client: {host_or_url: (status, body)}; anything else 404s."""
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(str(request.url))
        url = str(request.url)
        for key, (status, body) in routes.items():
            if key in url:
                return httpx.Response(status, text=body,
                                      headers={"content-type": "text/html"})
        return httpx.Response(404, text="")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


NO_PACE = DiscoveryBudget(pace_min=0, pace_max=0)


# ── classification: the rules that stop us emailing the wrong people ─────────

def test_aggregators_are_never_websites():
    for url in ("https://www.tripadvisor.com/x", "https://ifood.com.br/y",
                "https://www.yelp.com/biz/z", "https://www.opentable.com/r"):
        assert is_aggregator(url), url


def test_plumbing_domains_are_not_mistaken_for_search_engines():
    """'grableplumbing.com' contains 'bing.com'. Substring matching would have
    silently discarded every *plumbing.com site, the operator's core niche."""
    assert not is_aggregator("https://grableplumbing.com/")
    assert not is_aggregator("https://tampabayplumbing.com")
    assert is_aggregator("https://www.bing.com/search?q=x")


def test_social_classification_and_canonicalisation():
    assert classify_social("https://www.instagram.com/nortegrill/?hl=pt") == (
        "instagram", "https://www.instagram.com/nortegrill")
    assert classify_social("https://wa.me/5521999998888") == (
        "whatsapp", "https://wa.me/5521999998888")
    assert classify_social("https://api.whatsapp.com/send?phone=5521999998888")[0] \
        == "whatsapp"
    assert classify_social("https://www.linkedin.com/company/acme")[0] == "linkedin"
    # share/intent links and platform pages are not profiles
    assert classify_social("https://www.facebook.com/sharer/sharer.php?u=x") == ("", "")
    assert classify_social("https://twitter.com/intent/tweet") == ("", "")
    assert classify_social("https://example.com/about") == ("", "")


def test_linkinbio_detection():
    assert is_linkinbio("https://linktr.ee/nortegrill")
    assert is_linkinbio("https://beacons.ai/x")
    assert not is_linkinbio("https://nortegrill.com.br")


# ── scoring: a wrong website is worse than no website ───────────────────────

def test_scoring_prefers_name_match_and_country_tld():
    good = score_candidate("https://nortegrill.com.br", "Norte Grill", "BR")
    weak = score_candidate("https://someotherplace.com/a/b/c", "Norte Grill", "BR")
    assert good > weak
    assert score_candidate("https://www.tripadvisor.com/norte-grill",
                           "Norte Grill", "BR") == 0     # aggregator: never


@pytest.mark.parametrize("url,business", [
    # Every one of these was actually returned by a live Rio run and would have
    # been emailed as if it were the prospect's own site.
    ("https://vejario.abril.com.br/x", "Restaurante Azumi"),      # magazine
    ("https://listaamarela.com.br/y", "Paraíso do Chopp"),        # BR yellow pages
    ("https://guiadasemana.com.br/z", "Botequim do Borges"),      # listings guide
    ("https://duogourmet.com.br/g", "Glória Gourmet"),            # dining club
    ("https://www.hugedomains.com", "Real Astoria"),              # domain reseller
])
def test_wrong_sites_from_live_runs_are_rejected(url, business):
    """A wrong website is worse than none: it feeds a stranger's address to the
    sender. The domain must resemble the business, not merely mention it."""
    from engine.discovery import host_resembles_name

    assert score_candidate(url, business, "BR") == 0
    assert not host_resembles_name(url, business)


@pytest.mark.parametrize("url,business", [
    ("https://ranchoportugues.com.br", "Rancho"),
    ("https://obomgaleto.com.br", "O Bom Galeto"),
    ("https://nortegrill.com.br", "Norte Grill"),
    ("https://gloriagourmet.com.br", "Glória Gourmet"),
])
def test_genuine_sites_still_accepted(url, business):
    from engine.discovery import host_resembles_name

    assert host_resembles_name(url, business)
    assert score_candidate(url, business, "BR") >= MIN_WEBSITE_SCORE


def test_owner_published_bio_link_bypasses_the_name_gate():
    """A domain that looks nothing like the name is acceptable IF the owner
    published it on their own bio page. That endorsement is the evidence."""
    url = "https://churrascarianorte.com.br"
    assert score_candidate(url, "Norte Grill", "BR") == 0
    assert score_candidate(url, "Norte Grill", "BR", from_bio=True) >= MIN_WEBSITE_SCORE


def test_bio_links_score_highest():
    """The owner published this link themselves: strongest signal we have."""
    plain = score_candidate("https://nortegrill.com.br", "Norte Grill", "BR")
    bio = score_candidate("https://nortegrill.com.br", "Norte Grill", "BR",
                          from_bio=True)
    assert bio == plain + 4


def test_unreachable_candidate_is_penalised():
    client = client_for({})   # everything 404s
    assert score_candidate("https://nortegrill.com.br", "Norte Grill", "BR",
                           client) < MIN_WEBSITE_SCORE


# ── rung E: the Norte Grill case study, offline ─────────────────────────────

def test_linkinbio_rung_recovers_the_real_website():
    """The Norte Grill case, offline.

    The domain does NOT match the business name, so rung B cannot guess it and
    search returns only an aggregator plus a Linktree. Only rung E, reading the
    public bio page the owner published, recovers the real site.
    """
    bio_html = """
      <html><body>
        <a href="https://www.instagram.com/nortegrill">Instagram</a>
        <a href="https://churrascarianorte.com.br">Nosso site</a>
        <a href="https://wa.me/5521999998888">WhatsApp</a>
      </body></html>"""
    site_html = "<html><body>Norte Grill churrascaria no Rio</body></html>"
    client = client_for({
        "linktr.ee": (200, bio_html),
        "churrascarianorte.com.br": (200, site_html),
        "robots.txt": (404, ""),
    })
    ddgs = FakeDDGS(default=["https://www.tripadvisor.com/norte-grill",
                             "https://linktr.ee/nortegrill"])

    result = discover_contacts(
        client, DiscoveryInput(name="Norte Grill", city="Rio de Janeiro",
                               country="BR", niche="restaurants"),
        budget=NO_PACE, ddgs_client=ddgs)

    assert result.website == "https://churrascarianorte.com.br"
    assert result.provenance["website"]["rung"] == "E"
    assert "linktr.ee" in result.provenance["website"]["source"]
    assert result.social_links["instagram"] == "https://www.instagram.com/nortegrill"
    assert result.phone == "+5521999998888"      # parsed from the wa.me link
    assert "E" in result.rungs
    # the aggregator was kept as a listing, never as the website
    assert any("tripadvisor" in u for u in result.listings)


def test_ladder_short_circuits_when_provider_data_is_enough():
    """Rung A gave a site and an email: no search, no bio fetch."""
    client = client_for({"acme.com.br": (200, "<html>Acme contato</html>")})
    ddgs = FakeDDGS(default=["https://should-not-be-used.com"])
    result = discover_contacts(
        client, DiscoveryInput(name="Acme", city="Rio", country="BR",
                               website="https://acme.com.br",
                               emails=["contato@acme.com.br"]),
        budget=NO_PACE, ddgs_client=ddgs)
    assert result.website == "https://acme.com.br"
    assert result.provenance["website"]["rung"] == "A"
    assert ddgs.queries == []          # rung C never ran


def test_provider_aggregator_website_is_demoted_to_listing():
    client = client_for({})
    result = discover_contacts(
        client, DiscoveryInput(name="Bar X", city="Rio", country="BR",
                               website="https://www.tripadvisor.com/bar-x"),
        budget=DiscoveryBudget(allow_search=False, pace_min=0, pace_max=0))
    assert result.website == ""
    assert "https://www.tripadvisor.com/bar-x" in result.listings


# ── rung F: crawl, robots, multilingual paths ───────────────────────────────

def test_crawl_extracts_portuguese_contact_page():
    home = ('<html><body><a href="/contato">Contato</a>'
            '<a href="https://www.instagram.com/casabraz">IG</a></body></html>')
    contato = ('<html><body>Fale conosco: contato@casabraz.com.br '
               '<a href="tel:+552122334455">ligar</a>'
               '<form><input type="email"><textarea></textarea></form>'
               '</body></html>')
    client = client_for({"/contato": (200, contato),
                         "casabraz.com.br": (200, home),
                         "robots.txt": (404, "")})
    result = discover_contacts(
        client, DiscoveryInput(name="Casa Braz", city="Rio", country="BR",
                               website="https://casabraz.com.br"),
        budget=NO_PACE)
    assert "contato@casabraz.com.br" in result.emails
    assert result.contact_form_url.endswith("/contato")
    assert result.social_links["instagram"].endswith("/casabraz")
    assert result.phone.startswith("+55")


def test_robots_disallow_is_respected():
    robots = "User-agent: *\nDisallow: /"
    client = client_for({"robots.txt": (200, robots),
                         "blocked.example": (200, "<html>secret@blocked.example</html>")})
    result = discover_contacts(
        client, DiscoveryInput(name="Blocked Co", website="https://blocked.example"),
        budget=DiscoveryBudget(allow_search=False, pace_min=0, pace_max=0))
    assert result.emails == []
    assert result.provenance.get("crawl", {}).get("source") == "robots.txt disallowed"


def test_linkinbio_respects_robots():
    client = client_for({"robots.txt": (200, "User-agent: *\nDisallow: /"),
                         "linktr.ee": (200, '<a href="https://x.com.br">site</a>')})
    links, emails, phone = fetch_linkinbio(client, "https://linktr.ee/x", NO_PACE)
    assert links == [] and emails == [] and phone == ""


# ── provenance and budget ───────────────────────────────────────────────────

def test_every_populated_field_has_provenance():
    client = client_for({"acme.com.br": (200, "<html>Acme contato@acme.com.br</html>"),
                         "robots.txt": (404, "")})
    result = discover_contacts(
        client, DiscoveryInput(name="Acme", city="Rio", country="BR",
                               website="https://acme.com.br"),
        budget=DiscoveryBudget(allow_search=False, pace_min=0, pace_max=0))
    assert result.website and result.emails
    for field_name in ("website", "emails"):
        note = result.provenance[field_name]
        assert note["rung"] and note["source"] and 0 < note["confidence"] <= 1


def test_budget_exhaustion_returns_partial_not_hang(monkeypatch):
    import engine.discovery as disc

    ticks = iter([0, 0, 0, 99, 99, 99, 99, 99, 99, 99, 99])
    monkeypatch.setattr(disc.time, "monotonic", lambda: next(ticks, 99))
    client = client_for({})
    result = discover_contacts(
        client, DiscoveryInput(name="Slow Co", city="Rio", country="BR"),
        budget=DiscoveryBudget(total_seconds=5, pace_min=0, pace_max=0))
    assert result.partial is True


def test_hard_ceiling_bounds_a_blocking_rung():
    """A rung that blocks forever must not hang the run. Measured worst case
    over 20 live businesses: 24.0s against a 25s budget (0 over)."""
    import time as real_time

    def hanging_handler(request):
        real_time.sleep(30)          # simulate a socket that never answers
        return httpx.Response(200, text="")

    client = httpx.Client(transport=httpx.MockTransport(hanging_handler))
    started = real_time.monotonic()
    result = discover_contacts(
        client, DiscoveryInput(name="Hang Co", city="Rio", country="BR"),
        budget=DiscoveryBudget(total_seconds=3, pace_min=0, pace_max=0,
                               allow_search=False))
    elapsed = real_time.monotonic() - started
    assert elapsed < 5, f"ladder overran its 3s budget: {elapsed:.1f}s"
    assert result.partial is True


def test_pace_sleep_is_not_removed():
    """Politeness between searches is why the backends keep answering."""
    default = DiscoveryBudget()
    assert default.pace_min >= 4.0 and default.pace_max >= default.pace_min


# ── helpers ─────────────────────────────────────────────────────────────────

def test_rank_emails_prefers_on_domain_personal():
    emails = ["info@other.com", "contato@casabraz.com.br", "chef@casabraz.com.br"]
    assert rank_emails(emails, "https://casabraz.com.br")[0] == "chef@casabraz.com.br"


def test_extract_emails_filters_junk():
    html = ('a@b.png contato@real.com.br sentry@wixpress.com '
            '<a href="mailto:chef@real.com.br">x</a>')
    emails = extract_emails(html)
    assert "contato@real.com.br" in emails and "chef@real.com.br" in emails
    assert not any("png" in e or "wixpress" in e for e in emails)


def test_normalise_phone():
    assert normalise_phone("(21) 2233-4455", "BR") == "+552122334455"
    assert normalise_phone("+55 21 2233 4455") == "+552122334455"
    assert normalise_phone("", "BR") == ""


def test_cache_records_hits_and_only_complete_misses():
    cache = DiscoveryCache({}, {})
    hit = DiscoveryResult(website="https://x.com.br")
    hit.note("website", "C", "search", 0.8)
    cache.put("k1", hit)
    assert cache.get("k1")["website"] == "https://x.com.br"

    cache.put("k2", DiscoveryResult())                    # complete miss
    assert cache.is_known_miss("k2")
    cache.put("k3", DiscoveryResult(partial=True))        # ran out of budget
    assert not cache.is_known_miss("k3"), "a partial run must not poison the cache"
