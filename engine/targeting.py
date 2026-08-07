"""v2.4 geo and niche gate: is this prospect even in the campaign's market?

A real run drafted a US missed-call pitch at a restaurant in Rio de Janeiro.
The email was well formed and factually absurd. This module answers one
question, cheaply and deterministically: does this prospect belong to the
configured TARGET_COUNTRY and TARGET_TRADES at all?

Checked at prospecting time (cheap rejection with a stored reason) and again
in generate_draft() as a backstop, because enrichment can attach a foreign
website or email to a prospect long after prospecting let it through.
"""
import logging
from urllib.parse import urlparse

from engine.config import get_settings

log = logging.getLogger("targeting")

# ccTLD -> ISO country. Deliberately NOT exhaustive: only TLDs that really are
# national markers. Repurposed-generic TLDs (.co .io .ai .me .tv .fm .app .dev)
# are excluded on purpose; treating .io as British Indian Ocean Territory would
# reject half of the software industry.
CCTLD_COUNTRY = {
    "br": "BR", "uk": "GB", "de": "DE", "fr": "FR", "au": "AU", "ca": "CA",
    "mx": "MX", "ar": "AR", "cl": "CL", "pe": "PE", "in": "IN", "cn": "CN",
    "jp": "JP", "ru": "RU", "es": "ES", "it": "IT", "nl": "NL", "pt": "PT",
    "pl": "PL", "se": "SE", "no": "NO", "dk": "DK", "fi": "FI", "ie": "IE",
    "nz": "NZ", "za": "ZA", "ae": "AE", "sa": "SA", "eg": "EG", "tr": "TR",
    "gr": "GR", "ch": "CH", "at": "AT", "be": "BE", "cz": "CZ", "hu": "HU",
    "ro": "RO", "il": "IL", "kr": "KR", "th": "TH", "vn": "VN", "id": "ID",
    "my": "MY", "sg": "SG", "ph": "PH", "us": "US",
}

_COUNTRY_ALIASES = {"UK": "GB"}


def _normalize_country(code: str) -> str:
    code = (code or "").strip().upper()
    return _COUNTRY_ALIASES.get(code, code)


def _host_of(website_or_host: str) -> str:
    value = (website_or_host or "").strip().lower()
    if not value:
        return ""
    if "//" not in value:
        value = "//" + value
    return urlparse(value).netloc.split(":")[0]


def cctld_country(website: str = "", email: str = "") -> tuple[str, str]:
    """(country, tld) implied by a national ccTLD on the site or address,
    or ("", "") when the TLD is generic."""
    host = _host_of(website)
    if not host and email and "@" in email:
        host = email.rsplit("@", 1)[1].strip().lower()
    label = host.rsplit(".", 1)[-1] if "." in host else ""
    return CCTLD_COUNTRY.get(label, ""), label


def targeting_block_reason(country: str | None, website: str | None,
                           email: str | None, trade: str | None) -> str | None:
    """Why this prospect is outside the campaign's market, or None if it fits.

    TARGET_COUNTRY="" disables the geo check, TARGET_TRADES="" the niche check,
    so worldwide or any-niche discovery keeps working when the operator says so.
    """
    settings = get_settings()
    target = _normalize_country(settings.target_country)
    if target:
        prospect_country = _normalize_country(country or "")
        if prospect_country and prospect_country != target:
            return f"country {prospect_country} outside target {target}"
        for source, value in (("website", website), ("email", email)):
            check_site = value if source == "website" else ""
            check_mail = value if source == "email" else ""
            implied, tld = cctld_country(check_site or "", check_mail or "")
            if implied and implied != target:
                return f"{source} .{tld} ccTLD outside target {target}"
    trades = settings.target_trade_list
    if trades and trade and trade.strip().lower() not in trades:
        return f"trade '{trade}' not in TARGET_TRADES"
    return None
