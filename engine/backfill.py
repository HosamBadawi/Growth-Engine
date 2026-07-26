"""Phase 5: re-run the discovery ladder over prospects that predate it.

A discovery upgrade that only applies to future finds is worth a fraction of one
you can point at rows you already have. This walks a selectable filter, respects
the same budgets and pacing as a live run, reports progress the same way, and is
resumable: it processes in id order and skips anything already resolved, so
re-running after an interrupt continues rather than restarts.

It never overwrites a higher-confidence value with a lower-confidence one:
`_apply_discovery` compares provenance, so a verified fact always beats a guess.
"""
import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.models import Prospect, ProspectStatus
from engine.events import log_event

log = logging.getLogger("backfill")

# filter key -> (label, SQLAlchemy criteria builder)
FILTERS: dict[str, str] = {
    "no_website": "No website found yet",
    "no_email": "No email address yet",
    "never_enriched": "Never enriched (status NEW)",
    "no_contact": "No contact of any kind",
    "unreachable": "Previously marked UNREACHABLE",
}


def _criteria(filter_key: str):
    if filter_key == "no_website":
        return or_(Prospect.website.is_(None), Prospect.website == "")
    if filter_key == "no_email":
        return or_(Prospect.email.is_(None), Prospect.email == "")
    if filter_key == "never_enriched":
        return Prospect.status == ProspectStatus.NEW
    if filter_key == "unreachable":
        return Prospect.status == ProspectStatus.UNREACHABLE
    # no_contact: nothing to reach them by at all
    return (
        or_(Prospect.email.is_(None), Prospect.email == "")
        & or_(Prospect.phone.is_(None), Prospect.phone == "")
        & or_(Prospect.contact_form_url.is_(None), Prospect.contact_form_url == "")
    )


def candidates(session: Session, filter_key: str, limit: int = 100) -> list[Prospect]:
    criteria = _criteria(filter_key)
    return session.execute(
        select(Prospect).where(criteria,
                               Prospect.status != ProspectStatus.SUPPRESSED)
        .order_by(Prospect.id).limit(limit)
    ).scalars().all()


def run_backfill(session: Session, filter_key: str = "no_website",
                 limit: int = 25, progress=None) -> dict:
    """Re-run discovery over matching prospects. Sync: call via asyncio.to_thread."""
    from engine.enricher import enrich_prospect, outreach_channel

    if filter_key not in FILTERS:
        raise ValueError(f"Unknown backfill filter '{filter_key}'. "
                         f"Options: {list(FILTERS)}")

    rows = candidates(session, filter_key, limit)
    summary = {"filter": filter_key, "considered": len(rows), "processed": 0,
               "websites_found": 0, "emails_found": 0, "socials_found": 0,
               "failed": 0}

    def emit(text: str) -> None:
        log.info(text)
        if progress:
            try:
                progress(text)
            except Exception:  # noqa: BLE001 (progress is best-effort)
                pass

    emit(f"Backfill '{FILTERS[filter_key]}': {len(rows)} prospects to re-check")

    for index, prospect in enumerate(rows, start=1):
        had_site = bool(prospect.website)
        had_email = bool(prospect.email)
        had_social = bool(prospect.social_links)
        try:
            enrich_prospect(session, prospect, index=index)
            summary["processed"] += 1
            if not had_site and prospect.website:
                summary["websites_found"] += 1
            if not had_email and prospect.email:
                summary["emails_found"] += 1
            if not had_social and prospect.social_links:
                summary["socials_found"] += 1
            # A prospect that was written off can come back: new evidence beats
            # an old verdict, and the attempt counter resets with it.
            if prospect.status == ProspectStatus.UNREACHABLE and (
                    prospect.website or prospect.email):
                prospect.status = ProspectStatus.ENRICHED
                prospect.attempt_count = 0
                session.commit()
        except Exception as exc:  # noqa: BLE001 (one bad row must not stop the batch)
            log.warning("backfill failed for %s: %s", prospect.name, exc)
            session.rollback()
            summary["failed"] += 1
        emit(f"  [{index}/{len(rows)}] {prospect.name} -> "
             f"{prospect.website or 'no site'} ({outreach_channel(prospect)})")

    log_event(session, "backfill",
              f"Backfill '{filter_key}': processed {summary['processed']}, "
              f"websites +{summary['websites_found']}, "
              f"emails +{summary['emails_found']}, "
              f"socials +{summary['socials_found']}, failed {summary['failed']}",
              meta=summary)
    return summary
