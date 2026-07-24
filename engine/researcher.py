"""Module 4 — Researcher: turn scraped intel into a 3-bullet personalization card.

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


async def build_intel_card(session: Session, prospect: Prospect,
                           force: bool = False) -> dict:
    """Build (or return cached, <7 days old) intel card for a prospect."""
    intel = dict(prospect.intel_json or {})
    if not force and _card_is_fresh(intel):
        return intel["card"]

    researcher = get_researcher()
    try:
        card = await researcher.build_card(prospect)
    except Exception as exc:  # noqa: BLE001 — a failed card must never stop drafting
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

    facts = {
        "rating": prospect.rating,
        "review_count": prospect.review_count,
        "years_in_business": intel.get("years_in_business"),
    }
    if not is_clean_detail(str(card.get("human_detail") or ""), facts):
        log.info("%s: human_detail %r failed the quality gate, blanking it",
                 prospect.name, card.get("human_detail"))
        card["human_detail"] = ""  # writer falls back to deterministic details

    if card.get("owner_name") and not prospect.owner_name:
        prospect.owner_name = str(card["owner_name"])
    intel["card"] = card
    from engine.util import utcnow

    intel["card_fetched_at"] = utcnow().isoformat()
    prospect.intel_json = intel
    session.commit()
    log_event(session, "researcher", f"{prospect.name}: intel card ready")
    return card
