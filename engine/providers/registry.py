"""RegistryProvider — US state contractor license registries (public records).

v1: Florida DBPR daily CSV extracts (Ch. 119 public records, free bulk download).
Gives the ground-truth universe of licensed HVAC/plumbing/electrical businesses
with owner name + license number (both personalization gold). No phone/website
in the data, so we discover the website via a keyless DuckDuckGo HTML search and
let the existing enricher + verifier resolve contact details.

Query format: "<trade> <city> <state>" e.g. "hvac tampa fl" (state defaults FL).
Design is state-pluggable: add TX TDLR / CA CSLB by filling STATE_SOURCES.
"""
import csv
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from engine.config import get_settings
from engine.providers.base import ProspectProvider, RawProspect
from engine.util import utcnow

log = logging.getLogger("prospector.registry")

REGISTRY_DIR = Path("data/registry")
CACHE_TTL_HOURS = 24  # DBPR regenerates every morning
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Blocklist for website discovery: directories/aggregators, never the real site.
_DISCOVERY_BLOCK = (
    "yelp.", "yellowpages.", "superpages.", "bbb.org", "facebook.", "instagram.",
    "linkedin.", "angi.", "homeadvisor.", "thumbtack.", "houzz.", "mapquest.",
    "manta.", "buzzfile.", "dnb.com", "bizapedia.", "opencorporates.", "indeed.",
    "glassdoor.", "porch.com", "hvacservice.io", "chamberofcommerce.", "youtube.",
    "wikipedia.", "google.", "duckduckgo.", "birdeye.", "nextdoor.", "expertise.",
    "tripadvisor.", "zoominfo.", "cylex", "local.com", "citysearch",
)


@dataclass
class RegistrySource:
    state: str
    name: str
    url: str
    filename: str
    # 0-based CSV column indices (quote/comma-delimited, latin-1)
    col_prefix: int
    col_owner: int
    col_business: int
    col_addr: int
    col_city: int
    col_state: int
    col_zip: int
    col_status: int
    col_license: int
    active_values: tuple = ("A",)
    # trade keyword -> license prefixes that count as that trade in this state
    trade_prefixes: dict = field(default_factory=dict)


STATE_SOURCES: dict[str, list[RegistrySource]] = {
    "FL": [
        RegistrySource(
            state="FL", name="DBPR construction",
            url="https://www2.myfloridalicense.com/sto/file_download/extracts/CONSTRUCTIONLICENSE_1.csv",
            filename="fl_construction_1.csv",
            col_prefix=1, col_owner=2, col_business=3, col_addr=5, col_city=8,
            col_state=9, col_zip=10, col_status=14, col_license=20,
            trade_prefixes={
                "hvac": {"CAC", "RAC", "CMC", "RMC"},
                "air conditioning": {"CAC", "RAC", "CMC", "RMC"},
                "plumber": {"CFC", "RFC"},
                "plumbing": {"CFC", "RFC"},
                "roofer": {"CCC", "RCC"},
                "roofing": {"CCC", "RCC"},
            },
        ),
        RegistrySource(
            state="FL", name="DBPR electrical",
            url="https://www2.myfloridalicense.com/sto/file_download/extracts/lic08el.csv",
            filename="fl_electrical.csv",
            col_prefix=1, col_owner=2, col_business=3, col_addr=5, col_city=8,
            col_state=9, col_zip=10, col_status=14, col_license=20,
            trade_prefixes={
                "electrician": {"EC", "ER"},
                "electrical": {"EC", "ER"},
            },
        ),
    ],
}

_QUERY_RE = re.compile(r"^\s*(.*?)\s+([A-Za-z .]+?)\s+([A-Za-z]{2})\s*$")


def parse_registry_query(query: str, default_state: str = "FL") -> tuple[str, str, str]:
    """'hvac tampa fl' -> ('hvac', 'tampa', 'FL'). State optional."""
    query = query.strip()
    match = _QUERY_RE.match(query)
    if match and len(match.group(3)) == 2:
        return match.group(1).lower(), match.group(2).lower().strip(), match.group(3).upper()
    parts = query.split()
    if parts and len(parts[-1]) == 2 and parts[-1].isalpha():
        state = parts[-1].upper()
        parts = parts[:-1]
    else:
        state = default_state
    if len(parts) >= 2:
        return parts[0].lower(), " ".join(parts[1:]).lower(), state
    return (parts[0].lower() if parts else ""), "", state


def _title_case_owner(raw: str) -> str:
    """'ERGLE, GERALD K' -> 'Gerald K Ergle'."""
    raw = raw.strip()
    if "," in raw:
        last, _, first = raw.partition(",")
        raw = f"{first.strip()} {last.strip()}"
    return " ".join(w.capitalize() for w in raw.split())


_INITIALS_RE = re.compile(r"([A-Za-z]\.)+[A-Za-z]?\.?$")

# DBPR uses these in the DBA column for licensees operating under their own name
# (including a long-lived typo in the data itself).
_DBA_PLACEHOLDERS = {"INDIVIDUAL", "INDIVIDUAL.", "INDIVDUAL", "NONE", "N/A", "NA", "SAME"}


def _title_case_business(raw: str) -> str:
    words = []
    for w in raw.strip().split():
        words.append(w.upper() if _INITIALS_RE.fullmatch(w) else w.capitalize())
    cleaned = " ".join(words)
    cleaned = re.sub(r"\bLlc\b", "LLC", cleaned)
    cleaned = re.sub(r"\bHvac\b", "HVAC", cleaned)
    return re.sub(r"\bAc\b", "AC", cleaned)


def ensure_extract(source: RegistrySource) -> Path:
    """Download the extract if missing or older than the TTL. Returns the path."""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    path = REGISTRY_DIR / source.filename
    if path.exists():
        age_h = (utcnow().timestamp() - path.stat().st_mtime) / 3600
        if age_h < CACHE_TTL_HOURS:
            return path
    log.info("Downloading %s registry extract (%s)...", source.state, source.name)
    try:
        with httpx.stream("GET", source.url, timeout=180, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
            tmp.replace(path)
    except httpx.HTTPError as exc:
        if path.exists():
            log.warning("Refresh failed (%s); using cached %s", exc, path.name)
            return path
        raise
    return path


def _rows_for_trade(source: RegistrySource, trade: str, city: str,
                    limit: int) -> list[RawProspect]:
    prefixes = None
    for key, pset in source.trade_prefixes.items():
        if key in trade or trade in key:
            prefixes = pset
            break
    if prefixes is None:
        return []
    path = ensure_extract(source)
    city_u = city.upper().strip()
    out: list[RawProspect] = []
    seen: set[str] = set()
    max_cols = max(source.col_license, source.col_status, source.col_zip) + 1

    with open(path, encoding="latin-1", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < max_cols:
                continue
            if row[source.col_prefix].strip().upper() not in prefixes:
                continue
            if source.active_values and row[source.col_status].strip() not in source.active_values:
                continue
            if city_u and row[source.col_city].strip().upper() != city_u:
                continue
            business = row[source.col_business].strip()
            owner = row[source.col_owner].strip()
            if not business or business.upper() in _DBA_PLACEHOLDERS:
                continue  # individual qualifier without a real DBA; we want businesses
            license_no = row[source.col_license].strip()
            dedupe = license_no or f"{business}|{row[source.col_city]}"
            if dedupe in seen:
                continue
            seen.add(dedupe)
            out.append(RawProspect(
                name=_title_case_business(business),
                category=trade,
                city=row[source.col_city].strip().title(),
                state=row[source.col_state].strip().upper() or source.state,
                address=", ".join(p for p in [
                    row[source.col_addr].strip(),
                    row[source.col_city].strip().title(),
                    f"{row[source.col_state].strip().upper()} "
                    f"{row[source.col_zip].strip()}".strip(),
                ] if p),
                source=f"registry:{source.state}",
                owner_name=_title_case_owner(owner) if owner else "",
                license_no=license_no,
            ))
            if len(out) >= limit * 4:  # headroom for website-discovery misses
                break
    return out


_WEBSITE_CACHE_FILE = REGISTRY_DIR / "website_cache.json"


def _load_website_cache() -> dict:
    if _WEBSITE_CACHE_FILE.exists():
        try:
            import json

            return json.loads(_WEBSITE_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _save_website_cache(cache: dict) -> None:
    import json

    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    _WEBSITE_CACHE_FILE.write_text(json.dumps(cache, indent=0), encoding="utf-8")


def _first_allowed(hrefs: list[str]) -> str:
    """First candidate that is a plausible business site, not a directory."""
    for href in hrefs:
        match = re.search(r"uddg=([^&]+)", href)
        if match:
            from urllib.parse import unquote

            href = unquote(match.group(1))
        if href.startswith("http") and not any(b in href.lower() for b in _DISCOVERY_BLOCK):
            return href
    return ""


# ── tier 1: domain guessing (no third party at all) ─────────────────────────

_NAME_STOPWORDS = {
    "llc", "inc", "corp", "co", "company", "corporation", "the", "of", "and",
    "a", "an", "&", "services", "service", "enterprises", "group", "usa", "fl",
}


def _distinctive_tokens(business: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", business.lower())
            if t not in _NAME_STOPWORDS]


def guess_domain(client: httpx.Client, business: str) -> str:
    """Probe likely .com domains built from the business name; verify the page
    actually mentions the business before accepting (parked-domain guard)."""
    tokens = _distinctive_tokens(business)
    if not tokens:
        return ""
    joined = "".join(tokens)
    candidates = [joined]
    if len(tokens) > 1:
        candidates.append("".join(tokens[:2]))
        candidates.append("-".join(tokens))
    verify_tokens = [t for t in tokens if len(t) > 3] or tokens
    for cand in dict.fromkeys(candidates):
        if not (3 <= len(cand) <= 40):
            continue
        try:
            resp = client.get(f"https://{cand}.com", timeout=6, follow_redirects=True)
            if resp.status_code == 200 and any(
                t in resp.text[:8000].lower() for t in verify_tokens
            ):
                return f"https://{httpx.URL(str(resp.url)).host}"
        except httpx.HTTPError:
            continue
    return ""


# ── tier 2: keyless multi-engine search via ddgs (MIT) ──────────────────────

# Valid ddgs 9.x backend names (verified live); startpage excluded, it serves
# captchas to this region, and rotation lets no single engine absorb the batch.
_SEARCH_BACKENDS = ["google", "brave", "duckduckgo", "mojeek", "yahoo"]


def search_website(ddgs_client, business: str, city: str, state: str,
                   index: int) -> str:
    """One paced lookup, rotating engines so no single one absorbs the batch."""
    query = f"{business} {city} {state}"
    delay = 15
    backend = _SEARCH_BACKENDS[index % len(_SEARCH_BACKENDS)]
    for attempt in range(3):
        try:
            results = ddgs_client.text(query, backend=backend, max_results=3)
            hrefs = [r.get("href", "") for r in (results or [])]
            return _first_allowed(hrefs)
        except Exception as exc:  # noqa: BLE001 — ratelimit/timeout: back off, rotate
            log.debug("search backend %s failed for %s: %s", backend, business, exc)
            time.sleep(delay + random.uniform(0, 3))
            delay *= 2
            backend = _SEARCH_BACKENDS[(index + attempt + 1) % len(_SEARCH_BACKENDS)]
    return ""


class RegistryProvider(ProspectProvider):
    name = "registry"

    def search(self, query: str, limit: int) -> list[RawProspect]:
        from engine.prospector_settings import eff_registry_state

        settings = get_settings()
        trade, city, state = parse_registry_query(query, eff_registry_state())
        sources = STATE_SOURCES.get(state)
        if not sources:
            raise ValueError(
                f"No registry source configured for state '{state}'. "
                f"Configured: {list(STATE_SOURCES)}. Use another provider or add it."
            )

        candidates: list[RawProspect] = []
        for source in sources:
            candidates.extend(_rows_for_trade(source, trade, city, limit))
        log.info("Registry %s: %d licensed '%s' businesses in %s before website discovery",
                 state, len(candidates), trade, city or "state")

        discover = settings.registry_discover_websites
        cache = _load_website_cache()
        results: list[RawProspect] = []
        stats = {"cached": 0, "guessed": 0, "searched": 0, "miss": 0}
        ddgs_client = None
        with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
            for i, raw in enumerate(candidates):
                if len(results) >= limit:
                    break
                if discover:
                    key = raw.license_no or f"{raw.name}|{raw.city}"
                    if key in cache:
                        raw.website = cache[key]
                        stats["cached"] += 1
                    else:
                        raw.website = guess_domain(client, raw.name)
                        if raw.website:
                            stats["guessed"] += 1
                        else:
                            if ddgs_client is None:
                                from ddgs import DDGS

                                ddgs_client = DDGS(timeout=8)  # ONE instance, reused
                            raw.website = search_website(ddgs_client, raw.name,
                                                         raw.city, raw.state, i)
                            stats["searched" if raw.website else "miss"] += 1
                            time.sleep(4 + random.uniform(0, 1.5))
                        if raw.website:
                            cache[key] = raw.website  # only cache hits; misses retry
                    if not raw.website:
                        continue  # no website = no email path; skip for now
                results.append(raw)
        if discover:
            _save_website_cache(cache)
        log.info("Registry %s: %d prospects with a website (%s)", state,
                 len(results), stats)
        return results
