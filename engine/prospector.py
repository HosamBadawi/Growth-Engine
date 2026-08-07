"""Prospector (module 1): find businesses, apply code-level filters, store NEW prospects."""
import logging
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.models import FindRun, Prospect, ProspectStatus, RejectedCandidate
from engine.events import log_event
from engine.providers import get_provider
from engine.providers.base import RawProspect, make_dedupe_key
from engine.util import utcnow

log = logging.getLogger("prospector")


def known_dedupe_keys(session: Session) -> set[str]:
    """Every dedupe key already in the database: one cheap indexed query."""
    rows = session.execute(
        select(Prospect.dedupe_key).where(Prospect.dedupe_key.isnot(None))
    ).scalars().all()
    return {k for k in rows if k}


def _filter_reason(raw: RawProspect, require_website: bool = True,
                   trade: str = "") -> str | None:
    from engine.prospector_settings import eff_franchise_keywords, eff_review_bounds
    from engine.targeting import targeting_block_reason

    # v2.4 geo/niche gate first: a Brazilian restaurant must never even enter
    # a US home-service campaign, whatever its reviews look like.
    block = targeting_block_reason(raw.country, raw.website,
                                   raw.emails[0] if raw.emails else "",
                                   raw.category or trade)
    if block:
        return block

    min_reviews, max_reviews = eff_review_bounds()
    if require_website and not raw.website:
        # Correct for a cold-email campaign, catastrophic for everything else:
        # this single line discarded 10/10 of a successful international search
        # in v2.1. Off by default only when the operator opts in (Phase 4).
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


def _try_website(raw: RawProspect) -> str:
    """Cheap website-only pass of the ladder (rungs B/C/E, no site crawl).

    Used only when require_website is on, so the filter's verdict reflects what
    the engine can actually find rather than what the provider happened to tag.
    """
    import httpx

    from engine.discovery import (USER_AGENT, DiscoveryBudget, DiscoveryInput,
                                  discover_contacts)

    budget = DiscoveryBudget(total_seconds=20, allow_site_crawl=False)
    try:
        with httpx.Client(timeout=8, headers={"User-Agent": USER_AGENT},
                          follow_redirects=True) as client:
            found = discover_contacts(
                client,
                DiscoveryInput(name=raw.name, city=raw.city, state=raw.state,
                               country=raw.country, niche=raw.category,
                               phone=raw.phone, emails=raw.emails,
                               key=raw.dedupe_key),
                budget=budget)
        if found.emails and not raw.emails:
            raw.emails = found.emails
        return found.website
    except Exception as exc:  # noqa: BLE001 (discovery must never break a run)
        log.warning("website rescue failed for %s: %s", raw.name, exc)
        return ""


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

    from engine.prospector_settings import eff_require_website

    provider = get_provider(
        provider_name or ("csv" if (query or "").startswith("csv:") else eff_provider())
    )
    search_query = query or f"{trade} {city}"
    require_website = eff_require_website()
    log_event(session, "prospector", f"Searching '{search_query}' via {provider.name}")

    run = FindRun(query=search_query, provider=provider.name, target=limit)
    session.add(run)
    session.commit()

    exclude_keys = known_dedupe_keys(session)
    raws = provider.search(search_query, limit, exclude_keys=exclude_keys,
                           progress=progress)
    skipped_known = getattr(provider, "last_skipped_known", 0)
    exhausted = bool(getattr(provider, "last_exhausted", False))
    summary = {"found": len(raws), "kept": 0, "skipped": {}, "duplicates": 0,
               "skipped_known": skipped_known, "exhausted": exhausted,
               "require_website": require_website, "run_id": run.id,
               "prospect_ids": []}

    rescued = 0
    for raw in raws:
        if summary["kept"] >= limit:
            break
        # With require_website=True the filter must judge us AFTER we have tried
        # to find a site, not before: rejecting on raw provider data alone throws
        # away every business whose website the ladder could have recovered.
        if require_website and not raw.website:
            found_site = _try_website(raw)
            if found_site:
                raw.website = found_site
                rescued += 1
        reason = _filter_reason(raw, require_website, trade=trade)
        if reason:
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
            log.info("Skipped '%s': %s", raw.name, reason)
            # Keep the row: a filtered candidate is still real, useful data and
            # the operator must be able to inspect and re-try it (Phase 3).
            session.add(RejectedCandidate(
                run_id=run.id, name=raw.name, city=raw.city or city,
                country=raw.country or None, reason=reason,
                raw_json={"phone": raw.phone, "website": raw.website,
                          "address": raw.address, "category": raw.category,
                          "maps_url": raw.maps_url, "emails": raw.emails,
                          "source": raw.source, "dedupe_key": raw.dedupe_key},
            ))
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
            country=(raw.country or None),
            source=raw.source or provider.name,
            status=ProspectStatus.NEW,
            intel_json=intel,
        )
        session.add(prospect)
        session.flush()  # need the id to scope this run's pipeline stages
        summary["prospect_ids"].append(prospect.id)
        summary["kept"] += 1

    summary["websites_recovered"] = rescued
    run.finished_at = utcnow()
    # prospect_ids are kept so a run can be exported (kept + rejected) later.
    run.summary_json = dict(summary)
    session.commit()
    log_event(
        session, "prospector",
        f"Query '{search_query}': found {summary['found']}, kept {summary['kept']}, "
        f"dupes {summary['duplicates']}, skipped known {summary['skipped_known']}, "
        f"filtered {sum(summary['skipped'].values())} "
        f"(require_website={require_website})"
        + (", source EXHAUSTED for this query" if exhausted else ""),
        meta=run.summary_json,
    )
    return summary
