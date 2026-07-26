"""Enricher (module 2): crawl the prospect's website for contacts and intel.

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


def outreach_channel(prospect: Prospect) -> str:
    """How this prospect can actually be reached, most automatable first.

    'email'  -> the engine can send automatically (if verified)
    'form'   -> a human submits their contact form
    'phone'  -> a human calls
    'social' -> a human messages them; the engine NEVER does (see discovery.py)
    'none'   -> no channel found
    """
    if prospect.email:
        return "email"
    if prospect.contact_form_url:
        return "form"
    if prospect.phone:
        return "phone"
    if prospect.social_links:
        return "social"
    return "none"


def _apply_discovery(prospect: Prospect, found) -> None:
    """Persist ladder output, never downgrading a higher-confidence value."""
    from engine.discovery import is_aggregator, is_parked

    existing = dict(prospect.provenance or {})

    # Clear a stored directory/parked URL when the ladder could not confirm a
    # real site. Rows saved before the aggregator gate existed still carry these,
    # and a directory URL is worse than none: the crawler mines it for emails and
    # can hand the sender a stranger's address.
    if prospect.website and not found.website and (
            is_aggregator(prospect.website) or is_parked(prospect.website)):
        listing = prospect.website
        prospect.website = None
        existing.pop("website", None)
        intel = dict(prospect.intel_json or {})
        intel["demoted_listing"] = listing
        prospect.intel_json = intel
        log.info("%s: cleared directory URL %s (not a real website)",
                 prospect.name, listing)

    def better(field_name: str, confidence: float) -> bool:
        return confidence >= float((existing.get(field_name) or {}).get("confidence", 0))

    for field_name, value in (("website", found.website), ("phone", found.phone),
                              ("contact_form_url", found.contact_form_url)):
        note = found.provenance.get(field_name)
        if value and note and better(field_name, note["confidence"]):
            setattr(prospect, field_name, value)
            existing[field_name] = note
    if found.emails:
        note = found.provenance.get("emails")
        if not prospect.email or (note and better("emails", note["confidence"])):
            prospect.email = found.emails[0]
            if note:
                existing["emails"] = note
    if found.social_links:
        merged = dict(prospect.social_links or {})
        for platform, url in found.social_links.items():
            merged.setdefault(platform, url)
            key = f"social.{platform}"
            if key in found.provenance:
                existing.setdefault(key, found.provenance[key])
        prospect.social_links = merged
    if found.country and not prospect.country:
        prospect.country = found.country
    prospect.provenance = existing
    prospect.discovery_partial = bool(found.partial)


def enrich_prospect(session: Session, prospect: Prospect, polite_delay: float = 2.0,
                    budget=None, progress=None, index: int = 0) -> dict:
    """Climb the discovery ladder, then extract intel from whatever it fetched.

    v2.2: contact discovery moved to engine/discovery.py so the enricher, the
    providers and the backfill all use the SAME rungs and the same provenance.
    A prospect with no website is no longer a dead end: the ladder may still
    find one (rungs B/C/E) or return a social/phone channel instead.
    """
    from engine.discovery import (DiscoveryBudget, DiscoveryInput,
                                  discover_contacts, rank_emails)

    intel = dict(prospect.intel_json or {})
    budget = budget or DiscoveryBudget()
    website = prospect.website or ""
    if website and not website.startswith("http"):
        website = "https://" + website

    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True) as client:
        found = discover_contacts(
            client,
            DiscoveryInput(
                name=prospect.name, city=prospect.city or "",
                state=prospect.state or "", country=prospect.country or "",
                niche=prospect.trade or "", website=website,
                phone=prospect.phone or "",
                emails=[prospect.email] if prospect.email else [],
                key=prospect.dedupe_key or "",
            ),
            budget=budget, progress=progress, index=index,
        )

    _apply_discovery(prospect, found)
    pages = found.pages
    if not pages:
        intel["enrich_error"] = ("no reachable website"
                                 if not found.website else "homepage unreachable")
        intel["discovery_rungs"] = found.rungs
        intel["listings"] = found.listings
        prospect.intel_json = intel
        prospect.status = ProspectStatus.ENRICHED
        session.commit()
        log_event(session, "enricher",
                  f"{prospect.name}: no page content "
                  f"(rungs {'/'.join(found.rungs)}, channel {outreach_channel(prospect)})",
                  level="WARNING")
        return intel

    website = found.website or website
    all_html = "\n".join(pages.values())
    all_html_lower = all_html.lower()
    emails = found.emails or extract_emails(all_html)
    best = prospect.email or (rank_emails(emails, website)[0] if emails else None)

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

    socials = dict(prospect.social_links or {})

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
            "discovery_rungs": found.rungs,
            "listings": found.listings,
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
