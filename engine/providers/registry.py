"""RegistryProvider: US state contractor license registries (public records).

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
from engine.providers.base import (ProspectProvider, RawProspect,
                                   tlds_for_country)
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


def _iter_rows_for_trade(source: RegistrySource, trade: str, city: str,
                         seen: set[str]):
    """Lazily yield every matching business in the extract.

    A generator instead of a capped list: with dedupe exclusion in front of
    website discovery, any fixed headroom multiplier can be consumed entirely
    by already-known businesses. Lazy iteration walks as deep as needed and
    stops naturally when the caller has enough or the file ends.
    """
    prefixes = None
    for key, pset in source.trade_prefixes.items():
        if key in trade or trade in key:
            prefixes = pset
            break
    if prefixes is None:
        return
    path = ensure_extract(source)
    city_u = city.upper().strip()
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
            raw = RawProspect(
                name=_title_case_business(business),
                category=trade,
                city=row[source.col_city].strip().title(),
                state=row[source.col_state].strip().upper() or source.state,
                country="US",  # US state licence registries by definition
                address=", ".join(p for p in [
                    row[source.col_addr].strip(),
                    row[source.col_city].strip().title(),
                    f"{row[source.col_state].strip().upper()} "
                    f"{row[source.col_zip].strip()}".strip(),
                ] if p),
                source=f"registry:{source.state}",
                owner_name=_title_case_owner(owner) if owner else "",
                license_no=license_no,
            )
            if raw.dedupe_key in seen:
                continue  # duplicate row within/across extracts
            seen.add(raw.dedupe_key)
            yield raw


_WEBSITE_CACHE_FILE = REGISTRY_DIR / "website_cache.json"
_MISS_CACHE_FILE = REGISTRY_DIR / "website_misses.json"
_MISS_TTL_DAYS = 30
_FLUSH_EVERY = 10  # persist mid-run so an interrupt never loses discovered domains


def _load_website_cache() -> dict:
    if _WEBSITE_CACHE_FILE.exists():
        try:
            import json

            return json.loads(_WEBSITE_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    import json
    import os

    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=0), encoding="utf-8")
    os.replace(tmp, path)  # atomic: an interrupt mid-write can't corrupt the file


def _save_website_cache(cache: dict) -> None:
    _atomic_write_json(_WEBSITE_CACHE_FILE, cache)


def _load_miss_cache() -> dict:
    """{dedupe_key: iso_timestamp} of businesses recently found to have no site."""
    if not _MISS_CACHE_FILE.exists():
        return {}
    try:
        import json

        raw = json.loads(_MISS_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cutoff = utcnow().timestamp() - _MISS_TTL_DAYS * 86400
    fresh = {}
    for key, ts in raw.items():
        try:
            from datetime import datetime

            if datetime.fromisoformat(ts).timestamp() >= cutoff:
                fresh[key] = ts
        except (ValueError, TypeError):
            continue
    return fresh


def _save_miss_cache(misses: dict) -> None:
    _atomic_write_json(_MISS_CACHE_FILE, misses)


# ── website discovery ────────────────────────────────────────────────────────
# The ladder itself lives in engine/discovery.py so every caller (registry,
# enricher, backfill) climbs the SAME rungs. These re-exports keep the historic
# names importable from this module.
from engine.discovery import (MAX_DOMAIN_PROBES, DiscoveryBudget,  # noqa: E402
                              DiscoveryInput, discover_contacts,
                              domain_candidates, guess_domain,
                              search_candidates)


def search_website(ddgs_client, business: str, city: str, state: str,
                   index: int) -> str:
    """First acceptable (non-directory) search hit for a business."""
    from engine.discovery import is_aggregator, is_linkinbio

    hits = search_candidates(ddgs_client, f"{business} {city} {state}", index,
                             max_results=3)
    for url in hits:
        if url.startswith("http") and not is_aggregator(url) and not is_linkinbio(url):
            return url
    return ""



class RegistryProvider(ProspectProvider):
    name = "registry"

    def __init__(self) -> None:
        self.last_skipped_known = 0
        self.last_exhausted = False

    def search(self, query: str, limit: int,
               exclude_keys: set[str] | None = None,
               progress=None) -> list[RawProspect]:
        from engine.prospector_settings import eff_registry_state

        settings = get_settings()
        exclude_keys = exclude_keys or set()
        self.last_skipped_known = 0
        self.last_exhausted = False

        trade, city, state = parse_registry_query(query, eff_registry_state())
        sources = STATE_SOURCES.get(state)
        if not sources:
            raise ValueError(
                f"No registry source configured for state '{state}'. "
                f"Configured: {list(STATE_SOURCES)}. Use another provider or add it."
            )

        def emit(text: str) -> None:
            log.info(text)
            if progress:
                try:
                    progress(text)
                except Exception:  # noqa: BLE001 (progress is best-effort)
                    pass

        discover = settings.registry_discover_websites
        cache = _load_website_cache()
        misses = _load_miss_cache() if discover else {}
        results: list[RawProspect] = []
        stats = {"cached": 0, "guessed": 0, "searched": 0, "miss": 0, "known_miss": 0}
        checked = 0
        dirty = 0
        started = time.monotonic()
        ddgs_client = None
        seen: set[str] = set()

        def flush() -> None:
            if discover:
                _save_website_cache(cache)
                _save_miss_cache(misses)

        try:
            with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
                exhausted_sources = 0
                for source in sources:
                    source_done = True
                    for raw in _iter_rows_for_trade(source, trade, city, seen):
                        if len(results) >= limit:
                            source_done = False
                            break

                        # Cheap exclusion BEFORE any network work: the whole point.
                        if raw.dedupe_key in exclude_keys:
                            self.last_skipped_known += 1
                            continue

                        if not discover:
                            results.append(raw)
                            continue

                        key = raw.dedupe_key
                        checked += 1
                        if key in cache:
                            raw.website = cache[key]
                            stats["cached"] += 1
                        elif key in misses:
                            stats["known_miss"] += 1  # no site last month; skip cheaply
                        else:
                            raw.website = guess_domain(client, raw.name,
                                                       raw.country or "US")
                            if raw.website:
                                stats["guessed"] += 1
                            else:
                                if ddgs_client is None:
                                    from ddgs import DDGS

                                    ddgs_client = DDGS(timeout=8)  # ONE instance
                                raw.website = search_website(
                                    ddgs_client, raw.name, raw.city, raw.state, checked)
                                stats["searched" if raw.website else "miss"] += 1
                                time.sleep(4 + random.uniform(0, 1.5))
                            if raw.website:
                                cache[key] = raw.website
                            else:
                                misses[key] = utcnow().isoformat()
                            dirty += 1
                            if dirty >= _FLUSH_EVERY:
                                flush()  # survive Ctrl-C / crash mid-run
                                dirty = 0

                        if checked % _FLUSH_EVERY == 0:
                            per = (time.monotonic() - started) / max(checked, 1)
                            remaining = max(limit - len(results), 0)
                            eta_min = int(per * remaining * 2 / 60)  # rough, honest
                            emit(f"Discovering websites: {checked} checked, "
                                 f"{len(results)} found"
                                 + (f", ~{eta_min} min left" if eta_min else ""))

                        if not raw.website:
                            continue  # no website = no email path
                        results.append(raw)
                    if source_done:
                        exhausted_sources += 1
                self.last_exhausted = (exhausted_sources == len(sources)
                                       and len(results) < limit)
        finally:
            flush()  # always persist, even on interrupt

        log.info("Registry %s: %d prospects with a website (%s, skipped known %d%s)",
                 state, len(results), stats, self.last_skipped_known,
                 ", source exhausted" if self.last_exhausted else "")
        return results
