"""Provider interface so the prospect source can be swapped (registry, CSV,
OSM, Places, ...) without touching the rest of the engine."""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# Optional sync progress hook a provider may call with human-readable lines.
SyncProgress = Callable[[str], None]


@dataclass
class RawProspect:
    name: str
    category: str = ""
    phone: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    rating: float | None = None
    review_count: int | None = None
    maps_url: str = ""
    emails: list[str] = field(default_factory=list)
    source: str = ""
    owner_name: str = ""
    license_no: str = ""
    country: str = ""  # ISO-3166 alpha-2 when the source knows it; drives TLD guessing

    @property
    def dedupe_key(self) -> str:
        """Cheap, stable identity computable BEFORE any network work.

        Must match Prospect.dedupe_key so the exclusion set from the DB can be
        applied to raw rows: license number when the source has one, else
        name|city lowercased.
        """
        return make_dedupe_key(self.license_no, self.name, self.city)


def make_dedupe_key(license_no: str | None, name: str | None, city: str | None) -> str:
    if license_no and license_no.strip():
        return license_no.strip().lower()
    return f"{(name or '').strip()}|{(city or '').strip()}".lower()


class ProspectProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search(self, query: str, limit: int,
               exclude_keys: set[str] | None = None,
               progress: SyncProgress | None = None) -> list[RawProspect]:
        """Run a search like 'hvac contractor dallas tx' and return raw prospects.

        exclude_keys: dedupe keys already in the database. Providers that can
        compute a key cheaply (registry) MUST skip those candidates before any
        expensive per-candidate work (website discovery). Others may ignore it:
        the prospector's _is_duplicate() remains the correctness backstop.

        progress: optional sync callback for human-readable progress lines
        during long runs. Providers may ignore it.
        """


# Country/TLD helpers live in engine.discovery (the lower-level module, so
# providers can import from it without a cycle). Re-exported here because
# providers and their tests have always imported them from this module.
from engine.discovery import (COUNTRY_TLDS, country_from_address,  # noqa: E402
                              tlds_for_country)

_STATE_RE = re.compile(r",\s*([A-Za-z .]+?),\s*([A-Z]{2})\b")


def parse_city_state(address: str) -> tuple[str, str]:
    """Best-effort parse of '123 Main St, Dallas, TX 75201, United States'."""
    if not address:
        return "", ""
    match = _STATE_RE.search(address)
    if match:
        return match.group(1).strip(), match.group(2)
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 2:
        tail = parts[-2] if parts[-1].lower() in ("united states", "usa") else parts[-1]
        tokens = tail.split()
        if len(tokens) >= 2 and len(tokens[0]) == 2 and tokens[0].isupper():
            return parts[-3] if len(parts) >= 3 else "", tokens[0]
    return "", ""
