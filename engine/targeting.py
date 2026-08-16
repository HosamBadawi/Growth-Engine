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
import re
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


# v2.5: words that assign a business to a trade family, independent of
# prospect.trade, because the source can simply be wrong: two construction
# companies sat in an HVAC campaign with trade='hvac' and got HVAC pitches.
# keyword -> trade family it implies
_OFF_TRADE_WORDS = {
    "construction": "construction", "builders": "construction",
    "builder": "construction", "building": "construction",
    "contractor": "construction", "remodel": "construction",
    "roofing": "roofing", "roofer": "roofing", "landscap": "landscaping",
    "restaurant": "restaurant", "catering": "catering",
    "insurance": "insurance", "realty": "real estate",
    "real estate": "real estate",
}
# "licensed contractor" and "your building" are normal prose on a legitimate
# HVAC site; those two only count when they appear in the company NAME.
_NAME_ONLY_WORDS = {"contractor", "building"}

# Vocabulary a company NAME uses to state each trade. A name that states the
# campaign's own trade ("Mcnatt Plumbing Company" in a plumber campaign)
# outranks prose keywords: a plumber's site mentioning the remodels and
# restaurants it serves is a service list, not an identity. First live run of
# the prose check blocked ten companies named Plumbing from a plumbing
# campaign; this map is what separates identity from clientele.
_TRADE_NAME_WORDS = {
    "hvac": r"\b(hvac|air|conditioning|cooling|heating|htg|refrigeration)\b",
    "plumber": r"\b(plumbing|plumbers?|piping|drains?|sewer|septic)\b",
    "plumbing": r"\b(plumbing|plumbers?|piping|drains?|sewer|septic)\b",
    "electrician": r"\b(electric|electrical|electricians?)\b",
    "electrical": r"\b(electric|electrical|electricians?)\b",
    "roofer": r"\broof\w*\b",
    "roofing": r"\broof\w*\b",
    "landscaping": r"\blandscap\w*\b",
    "landscaper": r"\blandscap\w*\b",
}


def _name_claims_own_trade(name: str, own_trade: str) -> bool:
    pattern = _TRADE_NAME_WORDS.get(own_trade)
    if not pattern:
        pattern = rf"\b{re.escape(own_trade)}\b"
    return bool(re.search(pattern, (name or "").lower()))
# Substring stems; everything else matches on word boundaries.
_STEM_WORDS = {"landscap", "remodel"}


def _trade_words_in(text: str, own_trade: str, name_context: bool) -> str | None:
    lowered = (text or "").lower()
    own = (own_trade or "").lower()
    for word, family in _OFF_TRADE_WORDS.items():
        if not name_context and word in _NAME_ONLY_WORDS:
            continue
        if word in own or family in own or own and own in family:
            continue  # their own trade's vocabulary is not a mismatch
        found = (word in lowered if word in _STEM_WORDS
                 else re.search(rf"\b{re.escape(word)}\b", lowered))
        if found:
            return word
    return None


def trade_mismatch_reason(prospect) -> str | None:
    """Why the business itself looks like a different trade than the campaign
    thinks, or None. Checks the company name and the intel card text, never
    prospect.trade alone: that field says what the SOURCE claimed. When in
    doubt, do not draft; a skipped prospect costs nothing."""
    own = (prospect.trade or "").strip().lower()
    if not own:
        return None
    word = _trade_words_in(prospect.name or "", own, name_context=True)
    if word:
        return (f"company name says '{word}' "
                f"({_OFF_TRADE_WORDS[word]}), campaign trade is '{own}'")
    if _name_claims_own_trade(prospect.name or "", own):
        # The name itself states the campaign's trade; prose keywords in the
        # intel describe who they work FOR, not what they are.
        return None
    intel = prospect.intel_json or {}
    card = intel.get("card") or {}
    card_text = " ".join([
        str(card.get("human_detail") or ""),
        str(card.get("pain_signal") or ""),
        " ".join(str(b) for b in (card.get("bullets") or [])),
        str(intel.get("about_text") or "")[:1000],
    ])
    word = _trade_words_in(card_text, own, name_context=False)
    if word:
        return (f"intel text says '{word}' "
                f"({_OFF_TRADE_WORDS[word]}), campaign trade is '{own}'")
    return None
