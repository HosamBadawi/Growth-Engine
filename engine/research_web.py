"""Researcher v2: real web deep-research per prospect.

WebResearcher runs 2-4 targeted searches (ddgs multi-engine, or a self-hosted
SearXNG when SEARXNG_BASE_URL is set), skips ad/sponsored results, fetches the
top organic pages politely (robots.txt best-effort like the enricher, 8s
timeout, ~6k chars total), and distills through the LLM researcher role into
the same card schema as v1, plus source_urls.

Research note (per repo rule 6): gpt-researcher (Apache-2.0) was evaluated and
deliberately NOT integrated: it pulls a heavy dependency tree and an agent
loop designed for long-form reports, while this pipeline needs one small JSON
card per prospect. ddgs + httpx + bs4 (all existing deps) cover it.
"""
import asyncio
import logging
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from db.models import Prospect
from engine.config import get_settings
from engine.llm import llm_chat_json
from engine.researcher import ResearchProvider

log = logging.getLogger("researcher.web")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GrowthEngine/2.0"
SEARCH_BACKENDS = ["google", "brave", "duckduckgo", "mojeek", "yahoo"]
SEARCH_DELAY_SECONDS = 3.0
FETCH_TIMEOUT = httpx.Timeout(8.0, connect=6.0)
TOTAL_TEXT_CAP = 6000
PER_PAGE_CAP = 2500

# Domains that are never "organic content about the business"
SKIP_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "pinterest.com", "mapquest.com", "wikipedia.org",
)
AD_MARKERS = ("sponsored", "/aclk", "googleadservices", "doubleclick",
              "utm_source=ads", "gclid=")


def _search_queries(prospect: Prospect) -> list[str]:
    city = (prospect.city or "").title()
    queries = [
        f'"{prospect.name}" {city} reviews',
        f'"{prospect.name}" owner',
        f'"{prospect.name}" {city}',
    ]
    if prospect.website:
        host = urlparse(prospect.website).netloc.removeprefix("www.")
        if host:
            queries.append(f'"{host}"')
    return queries[:4]


def _is_organic(href: str) -> bool:
    if not href.startswith("http"):
        return False
    lowered = href.lower()
    if any(marker in lowered for marker in AD_MARKERS):
        return False
    host = urlparse(href).netloc.lower()
    return not any(host.endswith(d) or host.endswith("." + d) for d in SKIP_DOMAINS)


def search_links(prospect: Prospect, max_links: int = 6) -> list[str]:
    """Blocking: run the query set, return deduped organic result URLs."""
    settings = get_settings()
    links: list[str] = []

    if settings.searxng_base_url:
        with httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT}) as client:
            for query in _search_queries(prospect):
                try:
                    resp = client.get(
                        f"{settings.searxng_base_url.rstrip('/')}/search",
                        params={"q": query, "format": "json"},
                    )
                    resp.raise_for_status()
                    for result in resp.json().get("results", [])[:3]:
                        href = result.get("url", "")
                        if _is_organic(href) and href not in links:
                            links.append(href)
                except httpx.HTTPError as exc:
                    log.debug("searxng query failed (%s): %s", query, exc)
                time.sleep(SEARCH_DELAY_SECONDS)
                if len(links) >= max_links:
                    break
        return links[:max_links]

    from ddgs import DDGS

    ddgs_client = DDGS(timeout=8)
    for i, query in enumerate(_search_queries(prospect)):
        backend = SEARCH_BACKENDS[i % len(SEARCH_BACKENDS)]
        try:
            results = ddgs_client.text(query, backend=backend, max_results=3)
            for result in results or []:
                href = result.get("href", "")
                if _is_organic(href) and href not in links:
                    links.append(href)
        except Exception as exc:  # noqa: BLE001 (one engine failing is routine)
            log.debug("search failed (%s via %s): %s", query, backend, exc)
        time.sleep(SEARCH_DELAY_SECONDS)
        if len(links) >= max_links:
            break
    return links[:max_links]


def _robots_allows(client: httpx.Client, url: str) -> bool:
    try:
        parsed = urlparse(url)
        rp = urllib.robotparser.RobotFileParser()
        resp = client.get(urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt"))
        if resp.status_code != 200:
            return True
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except httpx.HTTPError:
        return True


def fetch_page_texts(links: list[str], max_pages: int) -> tuple[str, list[str]]:
    """Blocking, sequential, polite. Returns (joined text, urls actually used)."""
    texts: list[str] = []
    used: list[str] = []
    total = 0
    with httpx.Client(timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True) as client:
        for url in links:
            if len(used) >= max_pages or total >= TOTAL_TEXT_CAP:
                break
            if not _robots_allows(client, url):
                continue
            try:
                resp = client.get(url)
                if resp.status_code != 200 or "html" not in resp.headers.get(
                        "content-type", "html"):
                    continue
                soup = BeautifulSoup(resp.text, "lxml")
                for tag in soup(["script", "style", "noscript", "header", "footer",
                                 "nav"]):
                    tag.decompose()
                text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:PER_PAGE_CAP]
                if len(text) > 200:
                    texts.append(f"[source: {url}]\n{text}")
                    used.append(url)
                    total += len(text)
            except httpx.HTTPError as exc:
                log.debug("fetch failed %s: %s", url, exc)
            time.sleep(1.0)
    return "\n\n".join(texts)[:TOTAL_TEXT_CAP], used


class WebResearcher(ResearchProvider):
    """Deep research card from live web sources (RESEARCH_MODE=web)."""

    async def build_card(self, prospect: Prospect) -> dict:
        settings = get_settings()
        links = await asyncio.to_thread(search_links, prospect)
        web_text, source_urls = await asyncio.to_thread(
            fetch_page_texts, links, settings.research_max_pages
        )

        intel = prospect.intel_json or {}
        site_text = (intel.get("about_text") or "")[:1500]
        if not web_text and not site_text:
            raise LookupError("no usable web or site text found")

        system = (
            "You extract sales personalization intel for cold outreach to local "
            "service businesses from web research. Be factual: only use what is "
            "in the provided sources, never invent, and ignore anything that "
            "reads like an ad, sponsored placement or SEO boilerplate. "
            "Reply with ONLY a JSON object."
        )
        user = (
            f"Business: {prospect.name} ({prospect.trade}, "
            f"{(prospect.city or '').title()}, {prospect.state or ''})\n"
            f"Known facts: rating={prospect.rating}, reviews={prospect.review_count}, "
            f"owner_guess={prospect.owner_name}\n\n"
            f"Their own website says:\n{site_text or '(nothing scraped)'}\n\n"
            f"Web research sources:\n{web_text or '(no pages fetched)'}\n\n"
            "Produce JSON with keys:\n"
            '- "owner_name": first+last name of the owner if the sources state it, else null\n'
            '- "human_detail": one specific human detail as a SHORT lowercase phrase '
            "under 10 words completing 'their ___ stood out' (years, family story, "
            "a named specialty, a concrete reviewed strength)\n"
            '- "pain_signal": one signal they may miss calls or respond slowly. One sentence.\n'
            '- "bullets": array of exactly 3 short strings for a human to skim.'
        )
        card = await llm_chat_json(
            "researcher", system, user,
            required_keys=["human_detail", "pain_signal", "bullets"],
        )
        card["source_urls"] = source_urls
        return card
