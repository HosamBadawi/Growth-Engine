"""Module 2 — Enricher: crawl the prospect's website for contacts and intel.

Hand-rolled httpx + BeautifulSoup crawler (research verdict: nothing on PyPI
beats ~200 lines of controlled code for homepage + /contact + /about).
Bounded depth, per-request timeout, robots.txt respected best-effort.
"""
import logging
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from db.models import Prospect, ProspectStatus
from engine.events import log_event

log = logging.getLogger("enricher")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GrowthEngine/2.0"
TIMEOUT = httpx.Timeout(10.0, connect=8.0)
MAX_EXTRA_PAGES = 4

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL_PATTERNS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "example.com", "sentry",
    "wixpress", "yourdomain", "domain.com", "email.com", "@2x", "godaddy",
    "schema.org", "sitelock",
)
ROLE_PREFIXES = {
    "info", "office", "contact", "admin", "sales", "support", "hello", "service",
    "billing", "team", "mail", "help", "dispatch", "scheduling", "customerservice",
}
CONTACT_LINK_RE = re.compile(r"(contact|about|team|our-story|meet|who-we-are|staff)", re.I)
OWNER_RE = re.compile(
    r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,2})\s*(?:,| is| –| -)?\s*(?:the |our |co-)?"
    r"(owner|founder|president|ceo)\b",
    re.I,
)
YEARS_SINCE_RE = re.compile(r"\bsince\s+((?:19|20)\d{2})\b", re.I)
YEARS_COUNT_RE = re.compile(r"\b(\d{1,2})\+?\s+years\b", re.I)

CHAT_WIDGET_MARKERS = (
    "intercom", "drift.com", "tawk.to", "livechat", "crisp.chat", "tidio",
    "podium.com", "hs-scripts", "zopim", "smartsupp", "chatwoot", "birdeye",
    "signpost", "webchat",
)
BOOKING_MARKERS = (
    "calendly.com", "housecallpro", "servicetitan", "getjobber", "jobber.com",
    "acuityscheduling", "workiz", "schedulicity", "bookingkoala", "setmore",
    "square.site/book", "youcanbook",
)
FAMILY_RE = re.compile(r"\bfamily[ -]owned\b|\bfather and son\b|\bfamily business\b", re.I)


def _fetch(client: httpx.Client, url: str) -> str | None:
    try:
        resp = client.get(url, follow_redirects=True)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "html"):
            return resp.text
    except httpx.HTTPError as exc:
        log.debug("fetch failed %s: %s", url, exc)
    return None


def _load_robots(client: httpx.Client, base: str) -> urllib.robotparser.RobotFileParser | None:
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = client.get(urljoin(base, "/robots.txt"), follow_redirects=True)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            return rp
    except httpx.HTTPError:
        pass
    return None


def extract_emails(html: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r'mailto:([^"\'?<>\s]+)', html, re.I):
        found.append(m.group(1))
    found.extend(EMAIL_RE.findall(html))
    cleaned = []
    for email in found:
        email = email.strip().strip(".").lower()
        if any(bad in email for bad in BAD_EMAIL_PATTERNS):
            continue
        if EMAIL_RE.fullmatch(email) and email not in cleaned:
            cleaned.append(email)
    return cleaned


def is_role_address(email: str) -> bool:
    return email.split("@")[0].lower() in ROLE_PREFIXES


def pick_best_email(emails: list[str], website: str) -> str | None:
    """Prefer personal on-domain > role on-domain > personal anywhere > any."""
    if not emails:
        return None
    domain = urlparse(website).netloc.lower().removeprefix("www.")
    on_domain = [e for e in emails if domain and e.endswith("@" + domain)]
    for pool in (on_domain, emails):
        personal = [e for e in pool if not is_role_address(e)]
        if personal:
            return personal[0]
        if pool:
            return pool[0]
    return emails[0]


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def enrich_prospect(session: Session, prospect: Prospect, polite_delay: float = 2.0) -> dict:
    """Crawl website, update prospect in place. Returns the intel found."""
    intel = dict(prospect.intel_json or {})
    website = prospect.website or ""
    if not website.startswith("http"):
        website = "https://" + website

    headers = {"User-Agent": USER_AGENT}
    pages: dict[str, str] = {}
    with httpx.Client(timeout=TIMEOUT, headers=headers) as client:
        robots = _load_robots(client, website)

        def allowed(url: str) -> bool:
            if robots is None:
                return True
            try:
                return robots.can_fetch(USER_AGENT, url)
            except Exception:
                return True

        home_html = _fetch(client, website) if allowed(website) else None
        if home_html is None:
            intel["enrich_error"] = "homepage unreachable"
            prospect.intel_json = intel
            prospect.status = ProspectStatus.ENRICHED
            session.commit()
            log_event(session, "enricher", f"{prospect.name}: homepage unreachable", level="WARNING")
            return intel
        pages[website] = home_html

        base_host = urlparse(website).netloc
        soup = BeautifulSoup(home_html, "lxml")
        candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = urljoin(website, a["href"])
            parsed = urlparse(href)
            if parsed.netloc != base_host:
                continue
            if CONTACT_LINK_RE.search(parsed.path) and href not in candidates:
                candidates.append(href)
        for url in candidates[:MAX_EXTRA_PAGES]:
            if not allowed(url):
                continue
            time.sleep(polite_delay / 2)
            html = _fetch(client, url)
            if html:
                pages[url] = html

    all_html = "\n".join(pages.values())
    all_html_lower = all_html.lower()

    emails = extract_emails(all_html)
    best = pick_best_email(emails, website)
    if best and not prospect.email:
        prospect.email = best

    about_pages = [u for u in pages if re.search(r"about|story|team|meet", u, re.I)]
    about_text = ""
    for url in about_pages or list(pages):
        about_text = _page_text(pages[url])
        if len(about_text) > 200:
            break
    about_text = about_text[:3000]

    owner_match = OWNER_RE.search(about_text) or OWNER_RE.search(_page_text(all_html)[:5000])
    if owner_match and not prospect.owner_name:
        prospect.owner_name = owner_match.group(1)

    contact_form_url = None
    for url, html in pages.items():
        if re.search(r"contact", url, re.I):
            psoup = BeautifulSoup(html, "lxml")
            form = psoup.find("form")
            if form and (form.find("textarea") or form.find("input", {"type": "email"})):
                contact_form_url = url
                break
    if not contact_form_url:
        home_soup = BeautifulSoup(home_html, "lxml")
        form = home_soup.find("form")
        if form and form.find("textarea"):
            contact_form_url = website
    if contact_form_url:
        prospect.contact_form_url = contact_form_url

    socials = {}
    for m in re.finditer(r'https?://(?:www\.)?(facebook|instagram)\.com/[^\s"\'<>)]+', all_html, re.I):
        socials.setdefault(m.group(1).lower(), m.group(0))

    years_in_business = None
    since = YEARS_SINCE_RE.search(about_text) or YEARS_SINCE_RE.search(all_html[:20000])
    if since:
        years_in_business = f"since {since.group(1)}"
    else:
        count = YEARS_COUNT_RE.search(about_text)
        if count:
            years_in_business = f"{count.group(1)} years"

    intel.update(
        {
            "emails_found": emails,
            "best_email": best,
            "email_is_role": is_role_address(best) if best else None,
            "socials": socials,
            "has_chat_widget": any(mk in all_html_lower for mk in CHAT_WIDGET_MARKERS),
            "has_online_booking": any(mk in all_html_lower for mk in BOOKING_MARKERS),
            "family_owned": bool(FAMILY_RE.search(about_text) or FAMILY_RE.search(all_html_lower[:20000])),
            "years_in_business": years_in_business,
            "about_text": about_text,
            "pages_crawled": list(pages),
        }
    )
    prospect.intel_json = intel
    prospect.status = ProspectStatus.ENRICHED
    session.commit()
    log_event(
        session, "enricher",
        f"{prospect.name}: {len(emails)} emails, owner={prospect.owner_name or '?'}, "
        f"chat_widget={intel['has_chat_widget']}, booking={intel['has_online_booking']}",
    )
    time.sleep(polite_delay)
    return intel
