"""The contact discovery ladder: business name + city -> reachable contact.

One entry point, `discover_contacts()`, climbing rungs cheapest-first and
short-circuiting the moment it has what it needs. Every populated field records
WHERE it came from and how confident we are (ground rule 14): a product whose
output people act on may not present unsourced data.

    A  provider data        free      OSM tags / Places fields already in hand
    B  domain guess         cheap     country-aware host probes
    C  web search           paced     scored candidates, never first-hit
    D  social extraction    free      public profile URLs from C and the site
    E  link-in-bio          1 fetch   public aggregator page -> the real domain
    F  site crawl           bounded   emails, contact form, multilingual paths
    G  phone consolidation  free      provider / wa.me / tel: / page text

WHAT THIS MODULE DELIBERATELY DOES NOT DO
- It never fetches a page behind a login wall. Instagram/Facebook profile URLs
  are recorded as data and never opened; only public pages (a business's own
  site, a public link-in-bio page, a search results page) are fetched.
- It never messages anyone. Social handles are surfaced for the operator to
  contact manually; email is the only automated channel in this system.
- It has no proxy rotation, fingerprint spoofing, or CAPTCHA handling, and it
  respects robots.txt on every fetch. If a site says no, we skip and record why.
These are product decisions, not oversights. Do not "fix" them.
"""
import logging
import random
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

# Sync progress hook: a plain callable, declared here so this module imports
# nothing from engine.providers (which would create an import cycle).
SyncProgress = Callable[[str], None]

log = logging.getLogger("discovery")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GrowthEngine/2.2"

# Country -> candidate TLDs, most specific first. `.com` is appended as the
# universal fallback for every country. Extend with one line per market.
COUNTRY_TLDS: dict[str, tuple[str, ...]] = {
    "BR": ("com.br", "br"),
    "EG": ("com.eg", "eg"),
    "US": ("com", "net"),
    "CA": ("ca", "com"),
    "GB": ("co.uk", "uk"),
    "UK": ("co.uk", "uk"),
    "DE": ("de",),
    "FR": ("fr",),
    "AE": ("ae", "com"),
    "SA": ("com.sa", "sa"),
    "ES": ("es",),
    "IT": ("it",),
    "NL": ("nl",),
    "AU": ("com.au", "au"),
    "IN": ("in", "co.in"),
    "MX": ("com.mx", "mx"),
    "PT": ("pt",),
    "AR": ("com.ar",),
    "ZA": ("co.za",),
    "NG": ("com.ng", "ng"),
    "KE": ("co.ke",),
    "MA": ("ma",),
    "JO": ("jo", "com.jo"),
}

# Address-tail country names seen in provider output -> ISO code.
_COUNTRY_NAMES = {
    "brazil": "BR", "brasil": "BR",
    "egypt": "EG", "مصر": "EG",
    "united states": "US", "usa": "US", "united states of america": "US",
    "canada": "CA",
    "united kingdom": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "germany": "DE", "deutschland": "DE",
    "france": "FR",
    "united arab emirates": "AE", "uae": "AE",
    "saudi arabia": "SA",
    "spain": "ES", "españa": "ES",
    "italy": "IT", "italia": "IT",
    "netherlands": "NL",
    "australia": "AU",
    "india": "IN",
    "mexico": "MX", "méxico": "MX",
    "portugal": "PT",
    "argentina": "AR",
    "south africa": "ZA",
    "nigeria": "NG",
    "kenya": "KE",
    "morocco": "MA",
    "jordan": "JO",
}


def country_from_address(address: str) -> str:
    """Best-effort ISO code from an address tail ('..., Rio de Janeiro, Brazil')."""
    if not address:
        return ""
    tail = address.split(",")[-1].strip().lower()
    if len(tail) == 2 and tail.isalpha():
        return tail.upper()
    return _COUNTRY_NAMES.get(tail, "")


def tlds_for_country(country: str | None) -> list[str]:
    """Candidate TLDs for a country, always ending in the universal `.com`."""
    tlds = list(COUNTRY_TLDS.get((country or "").upper(), ()))
    if "com" not in tlds:
        tlds.append("com")
    return tlds


# Directories, marketplaces and social platforms. NEVER accepted as `website`:
# a listing page is not the business's own site, and treating one as such
# poisons email extraction downstream.
AGGREGATOR_DOMAINS = {
    # food / hospitality marketplaces
    "tripadvisor", "yelp", "ifood", "thefork", "zomato", "opentable", "grubhub",
    "ubereats", "rappi", "talabat", "elmenus", "deliveroo", "doordash",
    "seamless", "restaurantguru", "menulist", "sluurpy", "yably", "booking.com",
    # business directories (v2.1 blocklist, kept in full)
    "foursquare", "justdial", "yellowpages", "superpages", "mapquest", "manta",
    "buzzfile", "dnb.com", "bizapedia", "opencorporates", "porch.com",
    "chamberofcommerce", "birdeye", "expertise", "zoominfo", "cylex",
    "local.com", "citysearch", "hvacservice.io", "bbb.org", "allbiz",
    "merchantcircle", "brownbook", "hotfrog", "elocal", "ezlocal", "yellowbot",
    "storeboard", "trustpilot", "alignable", "crunchbase", "bizprofile",
    # home-services lead marketplaces and contractor directories
    "angi", "homeadvisor", "thumbtack", "houzz", "nextdoor", "buildzoom",
    "networx", "homeguide", "dexknows", "whitepages", "corporationwiki",
    "contractors.com", "yellowbook", "bark.com", "checkatrade", "trustedpros",
    # search engines / general
    "google.com", "goo.gl", "maps.app.goo.gl", "bing.com", "duckduckgo.com",
    "wikipedia", "amazon.com", "ebay.com", "glassdoor", "indeed",
    # non-English directories, magazines and dining clubs observed in live runs
    "listaamarela", "guiadasemana", "vejario", "abril.com.br", "duogourmet",
    "apontador", "hagah", "kekanto", "quandoo", "restorando", "guiamais",
    "telelistas", "solutudo", "encontraniteroi", "riomaisbarato", "catho",
}
# Domain-for-sale / parking pages. They echo the domain name back at you, which
# defeats the "does the page mention the business?" guard, so they must be
# rejected by host, not by content.
PARKED_DOMAINS = {
    "hugedomains", "sedo", "afternic", "dan.com", "undeveloped",
    "domainmarket", "buydomains", "namecheap", "godaddy", "squadhelp",
    "brandbucket", "parkingcrew", "bodis", "above.com", "sav.com",
}
SOCIAL_DOMAINS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "linkedin.com": "linkedin",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "wa.me": "whatsapp",
    "api.whatsapp.com": "whatsapp",
}
# Public, static link-in-bio aggregators. Fetching these is the whole point of
# rung E: the owner put their real website there themselves.
LINKINBIO_DOMAINS = {
    "linktr.ee", "beacons.ai", "bio.link", "linkin.bio", "campsite.bio",
    "lnk.bio", "solo.to", "taplink.cc", "msha.ke", "carrd.co", "many.link",
    "link.me", "shorby.com", "koji.to", "withkoji.com", "flowcode.com",
}
# Platform paths that are not a business profile.
_SOCIAL_JUNK_PATHS = {
    "share", "sharer", "sharer.php", "intent", "home", "login", "signup",
    "explore", "search", "hashtag", "help", "policies", "privacy", "terms",
    "pages", "watch", "reel", "reels", "p", "tv", "stories", "story",
    "directory", "developers", "business", "legal", "settings", "accounts",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL_PATTERNS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "example.com", "sentry",
    "wixpress", "yourdomain", "domain.com", "email.com", "@2x", "godaddy",
    "schema.org", "sitelock", "cloudflare", ".wordpress", "core.js",
)
PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")

# Contact-page paths. English-only was a real gap: the operator's own market is
# Egyptian and the observed run was Brazilian.
CONTACT_PATH_RE = re.compile(
    r"(contact|contato|contacto|contatti|kontakt|nous-contacter|contacte|"
    r"about|sobre|acerca|quienes|chi-siamo|ueber-uns|team|equipe|"
    r"%D8%A7%D8%AA%D8%B5%D9%84|%D8%AA%D9%88%D8%A7%D8%B5%D9%84|اتصل|تواصل)",
    re.I,
)
ROLE_LOCALPARTS = {
    "info", "contact", "contato", "contacto", "kontakt", "atendimento",
    "comercial", "sac", "office", "admin", "sales", "vendas", "support",
    "suporte", "hello", "hola", "ola", "service", "servicio", "reservas",
    "reservations", "booking", "mail", "email", "team", "help",
}

MIN_WEBSITE_SCORE = 5   # below this we return nothing: a wrong site is worse
_SEARCH_BACKENDS = ["google", "brave", "duckduckgo", "mojeek", "yahoo"]


# ── inputs / outputs ─────────────────────────────────────────────────────────

@dataclass
class DiscoveryInput:
    name: str
    city: str = ""
    state: str = ""
    country: str = ""
    niche: str = ""
    website: str = ""          # rung A: whatever the provider already gave us
    phone: str = ""
    emails: list[str] = field(default_factory=list)
    key: str = ""              # cache key (dedupe key)


@dataclass
class DiscoveryBudget:
    """Hard ceilings. A find run must never be able to hang again."""
    total_seconds: float = 25.0
    fetch_timeout: float = 6.0
    probe_timeout: float = 3.0
    max_site_pages: int = 4
    allow_search: bool = True
    allow_linkinbio: bool = True
    allow_site_crawl: bool = True
    pace_min: float = 4.0      # politeness between searches: do not remove
    pace_max: float = 5.5


@dataclass
class DiscoveryResult:
    website: str = ""
    emails: list[str] = field(default_factory=list)
    phone: str = ""
    social_links: dict = field(default_factory=dict)
    listings: list[str] = field(default_factory=list)
    contact_form_url: str = ""
    provenance: dict = field(default_factory=dict)
    partial: bool = False
    rungs: list[str] = field(default_factory=list)   # audit trail of what ran
    country: str = ""
    # Raw HTML of pages fetched in rung F, so callers (the enricher's intel
    # extraction) can reuse them instead of fetching the same pages again.
    pages: dict = field(default_factory=dict)

    def note(self, field_name: str, rung: str, source: str, confidence: float) -> None:
        self.provenance[field_name] = {"rung": rung, "source": source,
                                       "confidence": round(confidence, 2)}

    @property
    def best_email(self) -> str:
        return self.emails[0] if self.emails else ""


# ── url helpers ──────────────────────────────────────────────────────────────

def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    """Domain match on LABEL boundaries, never a bare substring.

    A substring test is actively dangerous here: 'grableplumbing.com' contains
    'bing.com', so every *plumbing.com site, the operator's core niche, would
    be misclassified as a search engine and silently discarded.
    """
    if not host:
        return False
    if host == domain or host.endswith("." + domain):
        return True
    if "." not in domain:                       # bare brand like 'yelp', 'angi'
        return domain in host.split(".")
    return False


def is_aggregator(url: str) -> bool:
    host = _host(url)
    return any(_host_matches(host, d) for d in AGGREGATOR_DOMAINS)


def is_parked(url: str) -> bool:
    host = _host(url)
    return any(_host_matches(host, d) for d in PARKED_DOMAINS)


def host_resembles_name(url: str, business: str) -> bool:
    """Does the DOMAIN itself look like this business?

    This is a gate, not a bonus. A page that merely mentions the business name
    proves nothing: directories, magazines and domain-parking pages all do that
    for thousands of businesses. Live run evidence: 'Restaurante Azumi' scored
    a magazine (vejario.abril.com.br), 'Paraiso do Chopp' scored the Brazilian
    yellow pages, 'Real Astoria' scored a domain reseller. Emailing any of those
    reaches a stranger, not the prospect.
    """
    tokens = distinctive_tokens(business)
    if not tokens:
        return False
    label = re.sub(r"[^a-z0-9]", "", _deaccent(_host(url).split(".")[0]))
    if not label:
        return False
    if "".join(tokens) in label:
        return True
    # ALL distinctive tokens must appear. One shared generic word is not a
    # match: 'duogourmet.com.br' is not 'Gloria Gourmet'.
    strong = [t for t in tokens if len(t) > 3]
    return bool(strong) and all(t in label for t in strong)


def is_linkinbio(url: str) -> bool:
    host = _host(url)
    return any(_host_matches(host, d) for d in LINKINBIO_DOMAINS)


def classify_social(url: str) -> tuple[str, str]:
    """(platform, canonical profile url) or ('', '') if it is not a profile."""
    host = _host(url)
    platform = ""
    for domain, name in SOCIAL_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            platform = name
            break
    if not platform:
        return "", ""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if platform == "whatsapp":
        # wa.me/<number> puts it in the path; api.whatsapp.com/send?phone=<n>
        # puts it in the query. Check both, in that order.
        candidates = [parts[0] if parts else ""]
        candidates += parse_qs(parsed.query).get("phone", [])
        for candidate in candidates:
            digits = re.sub(r"\D", "", candidate or "")
            if len(digits) >= 8:
                return "whatsapp", f"https://wa.me/{digits}"
        return "", ""
    if not parts or parts[0].lower() in _SOCIAL_JUNK_PATHS:
        return "", ""
    handle = parts[0]
    if platform == "linkedin":
        if len(parts) < 2 or parts[0] not in ("company", "in"):
            return "", ""
        handle = f"{parts[0]}/{parts[1]}"
    elif platform == "youtube" and parts[0] in ("channel", "c", "user"):
        handle = f"{parts[0]}/{parts[1]}" if len(parts) > 1 else parts[0]
    canonical = f"https://{'www.' if platform != 'whatsapp' else ''}" \
                f"{'instagram.com' if platform == 'instagram' else host}/{handle}"
    return platform, canonical.split("?")[0].rstrip("/")


def _unwrap_redirect(href: str) -> str:
    """DuckDuckGo-style /l/?uddg=<encoded> wrappers -> the real URL."""
    match = re.search(r"uddg=([^&]+)", href or "")
    return unquote(match.group(1)) if match else href


# ── rung B: domain guessing ──────────────────────────────────────────────────

_NAME_STOPWORDS = {
    "llc", "inc", "corp", "co", "company", "corporation", "the", "of", "and",
    "a", "an", "&", "services", "service", "enterprises", "group", "usa",
    "ltda", "ltd", "sa", "me", "restaurante", "restaurant", "cafe", "bar",
}
MAX_DOMAIN_PROBES = 4


def _deaccent(text: str) -> str:
    """'Glória' -> 'gloria'. Without this the regex splits accented words into
    fragments ('gl' + 'ria'), which wrecks both tokenising and host matching.
    The two markets this engine targets are Brazilian and Egyptian."""
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c))


def distinctive_tokens(business: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _deaccent(business).lower())
            if t not in _NAME_STOPWORDS and len(t) > 1]


def domain_candidates(business: str, country: str | None = None) -> list[str]:
    """Hosts to probe, best first, capped. Name-major so the strongest name
    gets all its TLDs (including the .com fallback) before a second spelling."""
    tokens = distinctive_tokens(business)
    if not tokens:
        return []
    names = [t for t in dict.fromkeys([
        "".join(tokens),
        "".join(tokens[:2]) if len(tokens) > 1 else "",
        "-".join(tokens) if len(tokens) > 1 else "",
    ]) if t and 3 <= len(t) <= 40]
    hosts: list[str] = []
    tlds = tlds_for_country(country)
    for name in names:
        for tld in tlds:
            host = f"{name}.{tld}"
            if host not in hosts:
                hosts.append(host)
            if len(hosts) >= MAX_DOMAIN_PROBES:
                return hosts
    return hosts


def guess_domain(client: httpx.Client, business: str, country: str | None = None,
                 timeout: float = 3.0, deadline: float | None = None) -> str:
    """Probe likely hosts; accept only if the page really mentions the business
    (parked-domain guard)."""
    verify = [t for t in distinctive_tokens(business) if len(t) > 3] \
        or distinctive_tokens(business)
    if not verify:
        return ""
    for host in domain_candidates(business, country):
        if remaining(deadline) <= 1:
            return ""
        try:
            # Never let a probe run past the budget.
            resp = client.get(f"https://{host}",
                              timeout=min(timeout, remaining(deadline)),
                              follow_redirects=True)
            if resp.status_code != 200:
                continue
            final = f"https://{httpx.URL(str(resp.url)).host}"
            # A guessed domain often redirects somewhere else entirely. Re-check
            # the FINAL host: domain resellers echo the name back at you, so the
            # content check alone accepted hugedomains.com as 'Real Astoria'.
            if is_parked(final) or is_aggregator(final) or is_linkinbio(final):
                continue
            if not host_resembles_name(final, business):
                continue
            if any(t in resp.text[:8000].lower() for t in verify):
                return final
        except httpx.HTTPError:
            continue
    return ""


# ── rung C: scored web search ────────────────────────────────────────────────

def score_candidate(url: str, business: str, country: str, client=None,
                    timeout: float = 6.0, from_bio: bool = False,
                    deadline: float | None = None) -> int:
    """Score a candidate website. A WRONG website is worse than none: it feeds
    the email extractor a stranger's address and can send them a cold email.
    Anything below MIN_WEBSITE_SCORE is discarded rather than guessed at."""
    if not url or not url.startswith("http") or is_aggregator(url) \
            or is_linkinbio(url) or is_parked(url):
        return 0
    # HARD GATE: the domain must look like the business. The only exception is a
    # link the owner published themselves on their own bio page (rung E), where
    # the endorsement replaces the name match.
    if not from_bio and not host_resembles_name(url, business):
        return 0
    host = _host(url)
    tokens = distinctive_tokens(business)
    score = 0
    if host_resembles_name(url, business):
        score += 3
    for tld in tlds_for_country(country)[:-1]:      # country TLDs, excluding .com
        if host.endswith("." + tld):
            score += 2
            break
    path = urlparse(url).path.strip("/")
    if path.count("/") <= 1:
        score += 1
    if from_bio:
        score += 4     # the owner published this link themselves
    if client is not None:
        try:
            resp = client.get(url, timeout=min(timeout, remaining(deadline)),
                              follow_redirects=True)
            if resp.status_code == 200:
                body = resp.text[:8000].lower()
                if any(t in body for t in tokens if len(t) > 3) or (
                        tokens and tokens[0] in body):
                    score += 3
            else:
                score -= 2
        except httpx.HTTPError:
            score -= 2      # unreachable: do not hand this to the sender
    return score


def remaining(deadline: float | None) -> float:
    """Seconds left before the deadline (a large number when there is none)."""
    return 3600.0 if deadline is None else max(0.0, deadline - time.monotonic())


def _call_with_hard_timeout(func, seconds: float):
    """Run a blocking call with a real wall-clock ceiling.

    Pre-checks alone cannot bound a call that is already running, and the search
    client's own timeout is set at construction. Abandoning the worker thread is
    acceptable here: it holds no lock and its result is simply discarded. This is
    what makes the per-business budget an actual ceiling rather than a hope.
    """
    import concurrent.futures

    if seconds <= 0:
        raise TimeoutError("no budget left")
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(func).result(timeout=seconds)
    finally:
        executor.shutdown(wait=False)


def _pace(budget: DiscoveryBudget) -> None:
    """Politeness between searches. This is why the backends keep answering."""
    time.sleep(random.uniform(budget.pace_min, budget.pace_max))


def search_candidates(ddgs_client, query: str, index: int,
                      max_results: int = 5, deadline: float | None = None) -> list[str]:
    """One paced lookup, rotating engines; primary then ONE alternate."""
    backend = _SEARCH_BACKENDS[index % len(_SEARCH_BACKENDS)]
    for attempt in range(2):
        # Check BEFORE every attempt, not only around the backoff: a single
        # search call can take ~8s, so starting one with 2s left overshoots.
        if deadline is not None and time.monotonic() >= deadline - 2:
            return []
        try:
            rows = _call_with_hard_timeout(
                lambda: ddgs_client.text(query, backend=backend,
                                         max_results=max_results),
                remaining(deadline))
            return [_unwrap_redirect(r.get("href", "")) for r in (rows or [])]
        except Exception as exc:  # noqa: BLE001 (ratelimit/timeout: rotate once)
            log.debug("search backend %s failed for %r: %s", backend, query, exc)
            if attempt == 0:
                backoff = 8 + random.uniform(0, 3)
                if deadline is not None:
                    # Never sleep past the caller's deadline: the backoff used to
                    # blow the per-business budget on its own.
                    backoff = min(backoff, max(0.0, deadline - time.monotonic()))
                if backoff <= 0:
                    return []
                time.sleep(backoff)
                backend = _SEARCH_BACKENDS[(index + 1) % len(_SEARCH_BACKENDS)]
    return []


# ── rung F: site crawl ───────────────────────────────────────────────────────

def _robots(client: httpx.Client, base: str):
    parser = urllib.robotparser.RobotFileParser()
    try:
        resp = client.get(urljoin(base, "/robots.txt"), timeout=5,
                          follow_redirects=True)
        if resp.status_code == 200:
            parser.parse(resp.text.splitlines())
            return parser
    except httpx.HTTPError:
        pass
    return None


def robots_allows(parser, url: str) -> bool:
    if parser is None:
        return True
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:  # noqa: BLE001 (a malformed robots must not block the run)
        return True


def extract_emails(html: str) -> list[str]:
    found = [m.group(1) for m in re.finditer(r'mailto:([^"\'?<>\s]+)', html, re.I)]
    found += EMAIL_RE.findall(html)
    out: list[str] = []
    for email in found:
        email = email.strip().strip(".").lower()
        if any(bad in email for bad in BAD_EMAIL_PATTERNS):
            continue
        if EMAIL_RE.fullmatch(email) and email not in out:
            out.append(email)
    return out


def rank_emails(emails: list[str], website: str) -> list[str]:
    """On-domain personal > on-domain role > off-domain personal > rest."""
    domain = _host(website)

    def rank(email: str) -> tuple[int, int]:
        local, _, host = email.partition("@")
        on_domain = bool(domain) and (host == domain or host.endswith("." + domain))
        return (0 if on_domain else 1, 0 if local not in ROLE_LOCALPARTS else 1)

    return sorted(dict.fromkeys(emails), key=rank)


def extract_social(html: str, results: list[str] | None = None) -> dict:
    urls = re.findall(r'https?://[^\s"\'<>)]+', html or "")
    urls += results or []
    social: dict = {}
    for url in urls:
        platform, canonical = classify_social(url)
        if platform and platform not in social:
            social[platform] = canonical
    return social


def extract_phone(html: str, existing: str = "") -> str:
    if existing:
        return existing
    tel = re.search(r'tel:([+\d][\d\s().\-]{6,})', html or "", re.I)
    if tel:
        return re.sub(r"\s+", " ", tel.group(1)).strip()
    match = PHONE_RE.search(BeautifulSoup(html or "", "lxml").get_text(" ")[:6000])
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else ""


def normalise_phone(phone: str, country: str = "") -> str:
    """Best-effort E.164. Digits only, prefixed when the country code is known."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+"):
        return "+" + digits
    prefixes = {"US": "1", "CA": "1", "BR": "55", "EG": "20", "GB": "44",
                "DE": "49", "FR": "33", "AE": "971", "SA": "966", "IN": "91"}
    code = prefixes.get((country or "").upper())
    if code and not digits.startswith(code):
        return f"+{code}{digits.lstrip('0')}"
    return f"+{digits}" if digits else ""


# ── the ladder ───────────────────────────────────────────────────────────────

def discover_contacts(client: httpx.Client, business: DiscoveryInput,
                      budget: DiscoveryBudget | None = None,
                      progress: SyncProgress | None = None,
                      ddgs_client=None, index: int = 0,
                      cache: "DiscoveryCache | None" = None) -> DiscoveryResult:
    """Climb the ladder under a HARD wall-clock ceiling.

    The ceiling is enforced structurally, by running the rungs in a worker and
    abandoning them at the deadline, because in-rung deadline checks cannot bound
    a network call that is already blocked (measured: 3 of 20 businesses still
    overran a checks-only implementation). Whatever the rungs filled in by then
    is returned with partial=True: the operator gets partial data, never a hang.
    """
    budget = budget or DiscoveryBudget()
    result = DiscoveryResult(country=business.country)
    try:
        # Reserve a second for thread hand-off so the OBSERVED wall clock stays
        # under budget.total_seconds rather than a hair over it.
        _call_with_hard_timeout(
            lambda: _run_ladder(result, client, business, budget, progress,
                                ddgs_client, index, cache),
            max(1.0, budget.total_seconds - 1.0))
    except TimeoutError:
        result.partial = True
        log.info("discovery budget exhausted for %s after %.0fs (rungs %s)",
                 business.name, budget.total_seconds, "/".join(result.rungs))
        if cache is not None and business.key:
            cache.put(business.key, result)
    return result


def _run_ladder(result: DiscoveryResult, client: httpx.Client,
                business: DiscoveryInput, budget: DiscoveryBudget,
                progress: SyncProgress | None, ddgs_client, index: int,
                cache: "DiscoveryCache | None") -> DiscoveryResult:
    started = time.monotonic()

    def out_of_time() -> bool:
        if time.monotonic() - started >= budget.total_seconds:
            result.partial = True
            return True
        return False

    def emit(text: str) -> None:
        if progress:
            try:
                progress(text)
            except Exception:  # noqa: BLE001
                pass

    def done() -> bool:
        """Stop condition: a website plus a way to reach them."""
        return bool(result.website and (result.emails or result.contact_form_url))

    # ── A: provider data ────────────────────────────────────────────────────
    result.rungs.append("A")
    if business.website and not is_aggregator(business.website):
        result.website = business.website
        result.note("website", "A", business.website, 0.95)
    elif business.website:
        result.listings.append(business.website)
    if business.emails:
        result.emails = rank_emails(business.emails, result.website)
        result.note("emails", "A", "provider", 0.9)
    if business.phone:
        result.phone = business.phone
        result.note("phone", "A", "provider", 0.9)

    cached = cache.get(business.key) if (cache and business.key) else None
    if cached and not result.website and cached.get("website"):
        result.website = cached["website"]
        result.note("website", "cache", cached.get("source", "cache"), 0.8)

    # ── B: domain guess ─────────────────────────────────────────────────────
    if not result.website and not out_of_time():
        result.rungs.append("B")
        guessed = guess_domain(client, business.name, business.country,
                               timeout=budget.probe_timeout,
                               deadline=started + budget.total_seconds)
        if guessed:
            result.website = guessed
            result.note("website", "B", "domain guess", 0.75)

    # ── C: scored web search ────────────────────────────────────────────────
    search_hits: list[str] = []
    if budget.allow_search and not result.website and not out_of_time():
        result.rungs.append("C")
        if ddgs_client is None:
            from ddgs import DDGS

            ddgs_client = DDGS(timeout=6)
        where = " ".join(p for p in [business.city, business.state] if p)
        variants = [f'"{business.name}" {where}'.strip()]
        if business.niche:
            variants.append(f'"{business.name}" {where} {business.niche}'.strip())
        tld = tlds_for_country(business.country)[0]
        if tld != "com":
            variants.append(f'"{business.name}" site:.{tld}')

        deadline = started + budget.total_seconds
        best, best_score = "", 0
        for variant_no, query in enumerate(variants):
            # A search plus its verification fetch needs ~10s to be worth
            # starting; without this the last variant reliably ran past the cap.
            if out_of_time() or (deadline - time.monotonic()) < 10:
                break
            hits = search_candidates(ddgs_client, query, index + variant_no,
                                     deadline=started + budget.total_seconds)
            search_hits.extend(hits)

            # Two-phase scoring. Phase 1 is free (no network): it applies the
            # host-name gate and drops directories outright. Phase 2 fetches ONLY
            # the top survivors. Verifying every hit at 6s each was the single
            # biggest budget overrun (5 hits = 30s in one rung).
            ranked = sorted(
                ((score_candidate(url, business.name, business.country), url)
                 for url in hits),
                reverse=True)
            for offline_score, url in ranked[:2]:
                if offline_score <= 0 or out_of_time():
                    break
                score = score_candidate(url, business.name, business.country,
                                        client, budget.fetch_timeout,
                                        deadline=deadline)
                if score > best_score:
                    best, best_score = url, score
                if best_score >= MIN_WEBSITE_SCORE:
                    break
            if best_score >= MIN_WEBSITE_SCORE or out_of_time():
                break
            _pace(budget)      # only pace when another search will actually run
        if best_score >= MIN_WEBSITE_SCORE:
            result.website = f"https://{_host(best)}"
            result.note("website", "C", f"search score {best_score}",
                        min(0.9, 0.5 + best_score / 20))
        elif best:
            log.debug("discarded low-confidence candidate %s (score %d) for %s",
                      best, best_score, business.name)

    # ── D: social links from search results ─────────────────────────────────
    if search_hits:
        result.rungs.append("D")
        for platform, url in extract_social("", search_hits).items():
            result.social_links.setdefault(platform, url)
            result.note(f"social.{platform}", "D", "search results", 0.7)
        for url in search_hits:
            if is_aggregator(url) and url not in result.listings:
                result.listings.append(url)

    # ── E: link-in-bio resolution ───────────────────────────────────────────
    if budget.allow_linkinbio and not result.website and not out_of_time():
        bio_url = next((u for u in search_hits if is_linkinbio(u)), "")
        if bio_url:
            result.rungs.append("E")
            emit(f"    link-in-bio: {bio_url}")
            links, bio_emails, bio_phone = fetch_linkinbio(client, bio_url, budget)
            best, best_score = "", 0
            for url in links:
                score = score_candidate(url, business.name, business.country,
                                        client, budget.fetch_timeout, from_bio=True)
                if score > best_score:
                    best, best_score = url, score
            if best_score >= MIN_WEBSITE_SCORE:
                result.website = f"https://{_host(best)}"
                result.note("website", "E", bio_url, min(0.95, 0.6 + best_score / 20))
            for platform, url in extract_social("", links).items():
                result.social_links.setdefault(platform, url)
                result.note(f"social.{platform}", "E", bio_url, 0.85)
            if bio_emails and not result.emails:
                result.emails = rank_emails(bio_emails, result.website)
                result.note("emails", "E", bio_url, 0.8)
            if bio_phone and not result.phone:
                result.phone = bio_phone
                result.note("phone", "E", bio_url, 0.8)

    # ── F: site crawl ───────────────────────────────────────────────────────
    if budget.allow_site_crawl and result.website and not done() and not out_of_time():
        result.rungs.append("F")
        crawl = crawl_site(client, result.website, budget,
                           deadline=started + budget.total_seconds)
        if crawl["emails"] and not result.emails:
            result.emails = rank_emails(crawl["emails"], result.website)
            result.note("emails", "F", crawl["source"], 0.85)
        if crawl["contact_form_url"]:
            result.contact_form_url = crawl["contact_form_url"]
            result.note("contact_form_url", "F", crawl["contact_form_url"], 0.9)
        for platform, url in crawl["social"].items():
            if platform not in result.social_links:
                result.social_links[platform] = url
                result.note(f"social.{platform}", "F", result.website, 0.9)
        if crawl["phone"] and not result.phone:
            result.phone = crawl["phone"]
            result.note("phone", "F", crawl["source"], 0.8)
        if crawl["blocked"]:
            result.note("crawl", "F", "robots.txt disallowed", 0.0)
        result.pages = crawl.get("pages", {})

    # ── G: phone consolidation ──────────────────────────────────────────────
    result.rungs.append("G")
    if not result.phone and "whatsapp" in result.social_links:
        digits = re.sub(r"\D", "", result.social_links["whatsapp"])
        if len(digits) >= 8:
            result.phone = f"+{digits}"
            result.note("phone", "G", "wa.me link", 0.85)
    if result.phone:
        result.phone = normalise_phone(result.phone, business.country)

    if out_of_time():
        result.partial = True
    if cache is not None and business.key:
        cache.put(business.key, result)
    return result


def fetch_linkinbio(client: httpx.Client, url: str,
                    budget: DiscoveryBudget) -> tuple[list[str], list[str], str]:
    """Read ONE public aggregator page: outbound links, mailto:, wa.me phone."""
    parser = _robots(client, url)
    if not robots_allows(parser, url):
        log.info("link-in-bio %s disallowed by robots.txt, skipping", url)
        return [], [], ""
    try:
        resp = client.get(url, timeout=budget.fetch_timeout, follow_redirects=True)
        if resp.status_code != 200:
            return [], [], ""
        html = resp.text
    except httpx.HTTPError as exc:
        log.debug("link-in-bio fetch failed %s: %s", url, exc)
        return [], [], ""

    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith("http") and not is_linkinbio(href) and href not in links:
            links.append(href)
    # Aggregators often render links from embedded JSON rather than anchors.
    for match in re.finditer(r'"(https?://[^"\s]+)"', html):
        href = match.group(1)
        if href.startswith("http") and not is_linkinbio(href) and href not in links:
            links.append(href)
    phone = ""
    for link in links:
        platform, canonical = classify_social(link)
        if platform == "whatsapp":
            phone = "+" + re.sub(r"\D", "", canonical)
            break
    return links, extract_emails(html), phone


def crawl_site(client: httpx.Client, website: str, budget: DiscoveryBudget,
               deadline: float | None = None) -> dict:
    """Homepage + up to N contact/about pages. robots.txt respected throughout."""
    out = {"emails": [], "social": {}, "phone": "", "contact_form_url": "",
           "source": website, "blocked": False, "pages": {}}
    parser = _robots(client, website)
    if not robots_allows(parser, website):
        out["blocked"] = True
        return out
    try:
        resp = client.get(website,
                          timeout=min(budget.fetch_timeout, remaining(deadline)),
                          follow_redirects=True)
        if resp.status_code != 200:
            return out
        home = resp.text
    except httpx.HTTPError:
        return out

    pages = {website: home}
    base_host = urlparse(str(resp.url)).netloc
    soup = BeautifulSoup(home, "lxml")
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(website, anchor["href"])
        parsed = urlparse(href)
        if parsed.netloc != base_host or href in candidates:
            continue
        if CONTACT_PATH_RE.search(parsed.path) or CONTACT_PATH_RE.search(
                anchor.get_text(" ")[:60]):
            candidates.append(href)
    for url in candidates[:budget.max_site_pages]:
        if deadline and time.monotonic() >= deadline:
            break
        if not robots_allows(parser, url):
            continue
        try:
            page = client.get(url,
                              timeout=min(budget.fetch_timeout, remaining(deadline)),
                              follow_redirects=True)
            if page.status_code == 200:
                pages[url] = page.text
        except httpx.HTTPError:
            continue

    all_html = "\n".join(pages.values())
    out["emails"] = extract_emails(all_html)
    out["social"] = extract_social(all_html)
    out["phone"] = extract_phone(all_html)
    for url, html in pages.items():
        page_soup = BeautifulSoup(html, "lxml")
        form = page_soup.find("form")
        if form and (form.find("textarea") or form.find("input", {"type": "email"})):
            out["contact_form_url"] = url
            break
    if len(pages) > 1:
        out["source"] = ", ".join(list(pages)[:3])
    out["pages"] = pages
    return out


# ── cache ────────────────────────────────────────────────────────────────────

class DiscoveryCache:
    """Positive and negative results, both persisted, both TTL'd."""

    def __init__(self, hits: dict, misses: dict, ttl_days: int = 30):
        self.hits, self.misses, self.ttl_days = hits, misses, ttl_days

    def get(self, key: str):
        return self.hits.get(key)

    def is_known_miss(self, key: str) -> bool:
        return key in self.misses

    def put(self, key: str, result: DiscoveryResult) -> None:
        from engine.util import utcnow

        if result.website:
            self.hits[key] = {"website": result.website,
                              "source": (result.provenance.get("website") or {}).get("source", "")}
            self.misses.pop(key, None)
        elif not result.partial:      # only remember a miss we fully investigated
            self.misses[key] = utcnow().isoformat()
