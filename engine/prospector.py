"""Module 1 — Prospector: find businesses, apply code-level filters, store NEW prospects."""
import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.models import Prospect, ProspectStatus
from engine.events import log_event
from engine.providers import get_provider
from engine.providers.base import RawProspect

log = logging.getLogger("prospector")


def _filter_reason(raw: RawProspect) -> str | None:
    from engine.prospector_settings import eff_franchise_keywords, eff_review_bounds

    min_reviews, max_reviews = eff_review_bounds()
    if not raw.website:
        return "no website"
    if raw.review_count is not None:
        if raw.review_count < min_reviews:
            return f"too few reviews ({raw.review_count})"
        if raw.review_count > max_reviews:
            return f"too many reviews ({raw.review_count}), likely big/franchise"
    lowered = raw.name.lower()
    for keyword in eff_franchise_keywords():
        if keyword and keyword in lowered:
            return f"franchise keyword '{keyword}'"
    return None


def _is_duplicate(session: Session, raw: RawProspect) -> bool:
    conds = []
    if raw.website:
        conds.append(Prospect.website == raw.website)
    if raw.phone:
        conds.append(Prospect.phone == raw.phone)
    conds.append((Prospect.name == raw.name) & (Prospect.city == raw.city))
    return session.execute(select(Prospect.id).where(or_(*conds)).limit(1)).first() is not None


def run_prospecting(
    session: Session, trade: str, city: str, limit: int, query: str | None = None
) -> dict:
    """Search, filter, dedupe and insert prospects. Returns a summary dict."""
    from engine.prospector_settings import eff_provider

    provider = get_provider(
        "csv" if (query or "").startswith("csv:") else eff_provider()
    )
    search_query = query or f"{trade} {city}"
    log_event(session, "prospector", f"Searching '{search_query}' via {provider.name}")

    raws = provider.search(search_query, limit)
    summary = {"found": len(raws), "kept": 0, "skipped": {}, "duplicates": 0}

    for raw in raws:
        if summary["kept"] >= limit:
            break
        reason = _filter_reason(raw)
        if reason:
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
            log.info("Skipped '%s': %s", raw.name, reason)
            continue
        if _is_duplicate(session, raw):
            summary["duplicates"] += 1
            continue
        effective_trade = (raw.category.lower()
                           if provider.name == "csv" and raw.category else trade.lower())
        intel = {"category": raw.category, "address": raw.address,
                 "provider_emails": raw.emails}
        if raw.license_no:
            intel["license_no"] = raw.license_no
        prospect = Prospect(
            name=raw.name,
            trade=effective_trade,
            city=(raw.city or city).lower(),
            state=raw.state,
            phone=raw.phone,
            website=raw.website,
            maps_url=raw.maps_url,
            rating=raw.rating,
            review_count=raw.review_count,
            owner_name=raw.owner_name or None,
            email=(raw.emails[0].lower() if raw.emails else None),
            source=raw.source or provider.name,
            status=ProspectStatus.NEW,
            intel_json=intel,
        )
        session.add(prospect)
        summary["kept"] += 1

    session.commit()
    log_event(
        session, "prospector",
        f"Query '{search_query}': found {summary['found']}, kept {summary['kept']}, "
        f"dupes {summary['duplicates']}, skipped {sum(summary['skipped'].values())}",
        meta=summary,
    )
    return summary
