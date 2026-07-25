"""Module 1 — Prospector: find businesses, apply code-level filters, store NEW prospects."""
import logging
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.models import Prospect, ProspectStatus
from engine.events import log_event
from engine.providers import get_provider
from engine.providers.base import RawProspect, make_dedupe_key

log = logging.getLogger("prospector")


def known_dedupe_keys(session: Session) -> set[str]:
    """Every dedupe key already in the database — one cheap indexed query."""
    rows = session.execute(
        select(Prospect.dedupe_key).where(Prospect.dedupe_key.isnot(None))
    ).scalars().all()
    return {k for k in rows if k}


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
    session: Session, trade: str, city: str, limit: int, query: str | None = None,
    provider_name: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Search, filter, dedupe and insert prospects. Returns a summary dict."""
    from engine.prospector_settings import eff_provider

    provider = get_provider(
        provider_name or ("csv" if (query or "").startswith("csv:") else eff_provider())
    )
    search_query = query or f"{trade} {city}"
    log_event(session, "prospector", f"Searching '{search_query}' via {provider.name}")

    exclude_keys = known_dedupe_keys(session)
    raws = provider.search(search_query, limit, exclude_keys=exclude_keys,
                           progress=progress)
    skipped_known = getattr(provider, "last_skipped_known", 0)
    exhausted = bool(getattr(provider, "last_exhausted", False))
    summary = {"found": len(raws), "kept": 0, "skipped": {}, "duplicates": 0,
               "skipped_known": skipped_known, "exhausted": exhausted}

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
            dedupe_key=make_dedupe_key(raw.license_no, raw.name, raw.city or city),
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
        f"dupes {summary['duplicates']}, skipped known {summary['skipped_known']}, "
        f"filtered {sum(summary['skipped'].values())}"
        + (" — source EXHAUSTED for this query" if exhausted else ""),
        meta=summary,
    )
    return summary
