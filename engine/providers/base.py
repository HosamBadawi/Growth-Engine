"""Provider interface so the prospect source can be swapped (gosom binary, CSV,
or a paid API later) without touching the rest of the engine."""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


class ProspectProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search(self, query: str, limit: int) -> list[RawProspect]:
        """Run a search like 'hvac contractor dallas tx' and return raw prospects."""


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
