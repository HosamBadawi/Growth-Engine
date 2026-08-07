"""Module 4, Researcher: turn scraped intel into a 3-bullet personalization card.

v1: summarize enricher output via local Ollama.
v2 (interface ready): plug in gpt-researcher (Apache-2.0, works with Ollama +
SearXNG retriever) or a self-hosted SearXNG deep-search adapter here.
"""
import logging
import re
from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from db.models import Prospect
from engine.config import get_settings
from engine.events import log_event
from engine.llm import LLMError, llm_chat_json

log = logging.getLogger("researcher")

# Tokens that mean the "detail" is scraped ad/SEO noise, not a human fact.
# Real observed failure: "best of the bay area sponsored search" passed into a
# draft as if it were a genuine compliment.
_NOISE_TOKENS = (
    "sponsored", "ppc", "seo", "search result", "top result", "google",
    "listing", "ranked", "#1 on",
)
_NOISE_WORD_RE = re.compile(r"\b(ads?|advert\w*)\b", re.I)
_SUPERLATIVE_RE = re.compile(
    r"\b(best|top rated|top-rated|leading|number one|no\.? ?1)\b", re.I
)

# ── v2.4 grounding: a detail the source cannot back does not exist ───────────
# Real failures this gate answers: "prom dresses and bridal wear available" on
# an HVAC company, "20 years serving San Diego" in a Tampa campaign, and the
# same "20 years serving Tampa" asserted about eight unrelated businesses.

_STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has "
    "have he her his how i if in into is it its just me more most my no not of "
    "on one or our out she so some than that the their them then there they "
    "this to up was we were what when where which who will with would you your "
    "across about over under after before while also very really".split()
)

# Vocabulary that belongs to another industry entirely. A topic is skipped when
# it appears in the prospect's own trade (a catering campaign may talk catering).
_BANNED_TOPICS = ("bridal", "prom", "wedding", "pageant", "insurance",
                  "underwriting", "legal defense", "attorney", "law firm",
                  "catering", "real estate", "realtor", "mortgage")

# Compact list of recognizable city names for the wrong-city check. Substring
# scan over the lowercase detail; the prospect's own city is always allowed.
_KNOWN_CITIES = (
    "new york", "los angeles", "chicago", "houston", "phoenix", "philadelphia",
    "san antonio", "san diego", "dallas", "san jose", "austin", "jacksonville",
    "fort worth", "columbus", "charlotte", "san francisco", "indianapolis",
    "seattle", "denver", "boston", "nashville", "detroit", "portland",
    "las vegas", "memphis", "louisville", "baltimore", "milwaukee",
    "albuquerque", "tucson", "sacramento", "kansas city", "atlanta", "miami",
    "orlando", "tampa", "st petersburg", "new orleans", "cleveland",
    "pittsburgh", "cincinnati", "minneapolis", "salt lake city", "san juan",
    "toronto", "london", "rio de janeiro", "sao paulo", "mexico city",
)

_RECENT_DETAILS: dict[str, int] = {}  # normalized detail -> first prospect id
_RECENT_MAX = 50


def reset_detail_memory() -> None:
    """Called at the start of a drafting run (and per test)."""
    _RECENT_DETAILS.clear()


def is_repeated_detail(detail: str, prospect_id: int | None) -> bool:
    """True when this exact detail was already used for a DIFFERENT prospect
    in the current run. "20 years serving Tampa" on eight businesses is not
    research, it is a hallucinated default."""
    key = re.sub(r"\s+", " ", (detail or "").strip().lower())
    if not key:
        return False
    first = _RECENT_DETAILS.get(key)
    if first is not None and first != (prospect_id or 0):
        return True
    if first is None:
        if len(_RECENT_DETAILS) >= _RECENT_MAX:
            _RECENT_DETAILS.pop(next(iter(_RECENT_DETAILS)))
        _RECENT_DETAILS[key] = prospect_id or 0
    return False


def _content_words(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9.'+]+", (text or "").lower())
    return [t.strip(".'") for t in tokens
            if t.strip(".'") and t.strip(".'") not in _STOPWORDS
            and (len(t.strip(".'")) > 2 or any(c.isdigit() for c in t))]


def grounding_source(prospect: Prospect, intel: dict | None = None) -> str:
    """The text a human_detail may legitimately be built from: the prospect's
    own structured facts plus whatever the enricher actually fetched."""
    intel = intel if intel is not None else (prospect.intel_json or {})
    facts = [prospect.name, prospect.trade, prospect.city, prospect.state,
             prospect.owner_name,
             "family owned" if intel.get("family_owned") else ""]
    # Structured facts carry their natural phrasing so "your 4.8 star rating
    # from 57 customers" grounds cleanly; the NUMBERS stay load-bearing, so a
    # fabricated "4.9 stars" still fails the overlap.
    if prospect.rating is not None:
        facts.append(f"{prospect.rating} star stars rating rated")
    if prospect.review_count is not None:
        facts.append(f"{prospect.review_count} reviews review customers")
    if intel.get("years_in_business"):
        facts.append(f"{intel['years_in_business']} years in business")
    return (" ".join(str(f) for f in facts if f not in (None, ""))
            + " " + str(intel.get("about_text") or ""))


def detail_discard_reason(detail: str, source_text: str, prospect: Prospect,
                          min_overlap: float = 0.7) -> str | None:
    """Why this detail must not reach a draft, or None when it is grounded.

    Three checks, cheapest first: wrong industry vocabulary, wrong city, and
    source overlap (>=70% of the detail's content words must appear in the
    text actually fetched for THIS prospect)."""
    lowered = (detail or "").strip().lower()
    if not lowered:
        return None
    trade = (prospect.trade or "").lower()
    for topic in _BANNED_TOPICS:
        if topic in lowered and topic not in trade:
            return f"off-industry topic '{topic}'"
    own_city = (prospect.city or "").strip().lower()
    for city in _KNOWN_CITIES:
        if city in lowered and city != own_city \
                and (not own_city or (city not in own_city and own_city not in city)):
            return f"names city '{city}', prospect is in '{own_city or 'unknown'}'"
    words = _content_words(lowered)
    if words:
        source_words = set(_content_words(source_text))
        present = sum(1 for w in words if w in source_words)
        overlap = present / len(words)
        if overlap < min_overlap:
            return (f"ungrounded ({present} of {len(words)} content words "
                    f"found in the fetched source)")
    return None


def is_clean_detail(text: str, facts: dict | None = None) -> bool:
    """Quality gate for a personalization detail before it can reach a draft.

    Rejects ad/SEO noise, unbacked superlatives, empties and over-long phrases.
    A rejected detail is blanked so the writer's deterministic fallback chain
    (rating+reviews -> years -> local reputation) takes over.
    """
    text = (text or "").strip()
    if not text:
        return False
    if len(text.split()) > 10:
        return False
    lowered = text.lower()
    if any(tok in lowered for tok in _NOISE_TOKENS):
        return False
    if _NOISE_WORD_RE.search(lowered):
        return False
    if _SUPERLATIVE_RE.search(lowered):
        # Superlatives pass only when tied to a countable fact from the data
        # (e.g. "4.9 stars across 120 reviews"), never as bare puffery.
        facts = facts or {}
        has_countable = bool(facts.get("rating") or facts.get("review_count")
                             or facts.get("years_in_business"))
        has_digits = any(ch.isdigit() for ch in text)
        if not (has_countable and has_digits):
            return False
    return True


class ResearchProvider(ABC):
    """v2 adapters (gpt-researcher, SearXNG) implement this same interface."""

    @abstractmethod
    async def build_card(self, prospect: Prospect) -> dict:
        """Return {"owner_name": str|None, "human_detail": str, "pain_signal": str,
        "bullets": [str, str, str]}"""


class LocalOllamaResearcher(ResearchProvider):
    async def build_card(self, prospect: Prospect) -> dict:
        intel = prospect.intel_json or {}
        facts = {
            "company": prospect.name,
            "trade": prospect.trade,
            "city": prospect.city,
            "state": prospect.state,
            "rating": prospect.rating,
            "review_count": prospect.review_count,
            "owner_name_guess": prospect.owner_name,
            "years_in_business": intel.get("years_in_business"),
            "family_owned": intel.get("family_owned"),
            "has_chat_widget": intel.get("has_chat_widget"),
            "has_online_booking": intel.get("has_online_booking"),
            "about_text": (intel.get("about_text") or "")[:2000],
        }
        system = (
            "You extract sales personalization intel for cold outreach to US home-service "
            "businesses. Be factual: only use what is in the provided data, never invent. "
            "Reply with ONLY a JSON object."
        )
        user = (
            f"Data about a {prospect.trade} company:\n{facts}\n\n"
            "Produce JSON with keys:\n"
            '- "owner_name": first+last name of owner if present in the data, else null\n'
            '- "human_detail": one specific human detail (years in business, family story, '
            "a value they state, a specialty) as a SHORT lowercase phrase under 10 words "
            "that completes the sentence 'their ___ stood out', "
            'e.g. "20 years serving Tampa" or "4.9 stars across 120 reviews"\n'
            '- "pain_signal": one signal they may miss calls or respond slowly '
            "(no chat widget on site, no online booking, small team, etc.). One sentence.\n"
            '- "bullets": array of exactly 3 short strings summarizing the above for a human.\n'
            "Note: has_chat_widget=false and has_online_booking=false are GOOD pain signals "
            "(they have no automated way to catch customers)."
        )
        card = await llm_chat_json(
            "researcher",
            system,
            user,
            required_keys=["human_detail", "pain_signal", "bullets"],
        )
        # Exactly what the model was shown: the grounding gate verifies the
        # proposed detail against this, nothing else.
        card["_source_text"] = grounding_source(prospect, intel)
        return card


CARD_TTL_DAYS = 7


def _card_is_fresh(intel: dict) -> bool:
    card = intel.get("card")
    if not card:
        return False
    fetched_at = intel.get("card_fetched_at")
    if not fetched_at:
        return True  # legacy card without timestamp: keep it
    from datetime import datetime, timedelta

    from engine.util import utcnow

    try:
        return utcnow() - datetime.fromisoformat(fetched_at) < timedelta(days=CARD_TTL_DAYS)
    except ValueError:
        return True


def get_researcher() -> ResearchProvider:
    """RESEARCH_MODE=web -> WebResearcher, anything else -> local summarizer."""
    if get_settings().research_mode.lower() == "web":
        from engine.research_web import WebResearcher

        return WebResearcher()
    return LocalOllamaResearcher()


def _gate_cached_detail(session: Session, prospect: Prospect,
                        intel: dict) -> dict:
    """Grounding also applies to CACHED cards: a card built before this gate
    existed (or by an older version) may still carry an ungrounded detail, and
    a cache must never be a loophole. Cheap: no LLM involved."""
    card = dict(intel.get("card") or {})
    detail = str(card.get("human_detail") or "")
    if not detail:
        return card
    facts = {"rating": prospect.rating, "review_count": prospect.review_count,
             "years_in_business": intel.get("years_in_business")}
    reason = None
    if not is_clean_detail(detail, facts):
        # A card cached before a gate existed still carries the old noise
        # ("best of the bay area sponsored search"); every gate applies on read.
        reason = "failed the ad/superlative quality gate"
    if reason is None:
        reason = detail_discard_reason(detail, grounding_source(prospect, intel),
                                       prospect)
    if reason is None and is_repeated_detail(detail, prospect.id):
        reason = "same detail already used for another prospect this run"
    if reason:
        log_event(session, "researcher",
                  f"{prospect.name}: discarded cached human_detail {detail!r} "
                  f"({reason})", level="WARNING")
        card["human_detail"] = ""
        intel["card"] = card
        prospect.intel_json = dict(intel)
        session.commit()
    return card


async def build_intel_card(session: Session, prospect: Prospect,
                           force: bool = False) -> dict:
    """Build (or return cached, <7 days old) intel card for a prospect."""
    intel = dict(prospect.intel_json or {})
    if not force and _card_is_fresh(intel):
        return _gate_cached_detail(session, prospect, intel)

    researcher = get_researcher()
    try:
        card = await researcher.build_card(prospect)
    except Exception as exc:  # noqa: BLE001 (a failed card must never stop drafting)
        if not isinstance(exc, (LLMError, LookupError)):
            log.exception("researcher crashed for %s", prospect.name)
        log_event(session, "researcher",
                  f"{prospect.name}: intel card failed ({exc}), using fallback",
                  level="WARNING")
        card = {
            "owner_name": prospect.owner_name,
            "human_detail": intel.get("years_in_business")
            or f"a {prospect.trade} company in {prospect.city}",
            "pain_signal": "no chat widget on their website"
            if not intel.get("has_chat_widget")
            else "customers likely reach voicemail after hours",
            "bullets": [],
        }

    source_text = str(card.pop("_source_text", "") or "") \
        or grounding_source(prospect, intel)

    facts = {
        "rating": prospect.rating,
        "review_count": prospect.review_count,
        "years_in_business": intel.get("years_in_business"),
    }
    detail = str(card.get("human_detail") or "")
    if detail and not is_clean_detail(detail, facts):
        log.info("%s: human_detail %r failed the quality gate, blanking it",
                 prospect.name, detail)
        card["human_detail"] = detail = ""
    if detail:
        # v2.4 grounding: a detail the fetched source cannot back, a detail
        # naming someone else's city, a detail from another industry, or a
        # detail already used on a different prospect this run, does not ship.
        # An email with no personalization beats an email with a wrong fact.
        reason = detail_discard_reason(detail, source_text, prospect)
        if reason is None and is_repeated_detail(detail, prospect.id):
            reason = "same detail already used for another prospect this run"
        if reason:
            log_event(session, "researcher",
                      f"{prospect.name}: discarded human_detail {detail!r} ({reason})",
                      level="WARNING")
            card["human_detail"] = ""

    if card.get("owner_name") and not prospect.owner_name:
        # Traceability: an owner name the model proposed must literally appear
        # in this prospect's own fetched text, or it is treated as invented.
        proposed = str(card["owner_name"]).strip()
        if proposed and proposed.lower() in source_text.lower():
            prospect.owner_name = proposed
        else:
            log_event(session, "researcher",
                      f"{prospect.name}: discarded owner_name {proposed!r} "
                      f"(not found in this prospect's own source)",
                      level="WARNING")
            card["owner_name"] = None
    intel["card"] = card
    from engine.util import utcnow

    intel["card_fetched_at"] = utcnow().isoformat()
    prospect.intel_json = intel
    session.commit()
    log_event(session, "researcher", f"{prospect.name}: intel card ready")
    return card
