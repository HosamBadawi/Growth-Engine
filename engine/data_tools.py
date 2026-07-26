"""Data tools behind Admin > Data: CSV exports, CSV import, DRY_RUN purge."""
import csv
import io
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (Prospect, ProspectStatus, Reply, Touch, TouchStatus,
                       TouchType)
from engine.events import log_event
from engine.sender import OUTBOX_DIR

log = logging.getLogger("data_tools")

PROSPECT_COLUMNS = ["id", "name", "trade", "city", "state", "country", "phone",
                    "website", "email", "email_verification_level", "owner_name",
                    "status", "rating", "review_count", "source",
                    "contact_form_url", "social_links", "provenance",
                    "discovery_partial", "created_at"]
TOUCH_COLUMNS = ["id", "prospect_id", "type", "status", "subject",
                 "scheduled_at", "sent_at", "created_at"]
REPLY_COLUMNS = ["id", "prospect_id", "classification", "received_at",
                 "handled", "raw_text"]

EXPORTS = {
    "prospects": (Prospect, PROSPECT_COLUMNS),
    "touches": (Touch, TOUCH_COLUMNS),
    "replies": (Reply, REPLY_COLUMNS),
}


def _cell(value) -> str:
    """Render a value for CSV: dicts/lists as compact JSON, never 'None'."""
    import json

    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def rows_to_csv(rows, columns: list[str], extra=None) -> str:
    """CSV text for any row set. `extra` maps a column name to a callable.

    Written by callers as utf-8-sig (BOM) so Excel renders Portuguese and
    Arabic business names instead of mojibake.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    extra = extra or {}
    for row in rows:
        writer.writerow([
            _cell(extra[col](row)) if col in extra else _cell(getattr(row, col, ""))
            for col in columns
        ])
    return buffer.getvalue()


def export_csv(session: Session, table: str) -> str:
    """Render one whole table as CSV text."""
    model, columns = EXPORTS[table]
    rows = session.execute(select(model).order_by(model.id)).scalars()
    return rows_to_csv(rows, columns)


VIEW_COLUMNS = PROSPECT_COLUMNS + ["stage", "channel"]


def export_prospect_view(prospects: list) -> str:
    """Export exactly the rows the operator is looking at, in their order."""
    from engine.enricher import outreach_channel
    from engine.stages import derive_stage

    return rows_to_csv(prospects, VIEW_COLUMNS,
                       extra={"stage": derive_stage, "channel": outreach_channel})


RUN_COLUMNS = ["outcome", "name", "city", "country", "reason", "phone",
               "website", "emails", "source", "dedupe_key"]


def export_run_csv(session: Session, run_id: int) -> str:
    """Everything a run touched, INCLUDING the rows it threw away.

    A filtered candidate is real data. v2.1 showed the operator the number '10'
    next to the word 'filtered' and discarded the rows; one of them was a lead
    a human found contactable in under a minute.
    """
    from db.models import FindRun, RejectedCandidate

    run = session.get(FindRun, run_id)
    ids = ((run.summary_json or {}).get("prospect_ids") or []) if run else []
    kept = session.execute(
        select(Prospect).where(Prospect.id.in_(ids)).order_by(Prospect.id)
    ).scalars().all() if ids else []
    rejected = session.execute(
        select(RejectedCandidate).where(RejectedCandidate.run_id == run_id)
        .order_by(RejectedCandidate.id)
    ).scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(RUN_COLUMNS)
    for p in kept:
        writer.writerow([_cell(v) for v in ["kept", p.name, p.city, p.country, "",
                                            p.phone, p.website, p.email or "",
                                            p.source, p.dedupe_key]])
    for r in rejected:
        raw = r.raw_json or {}
        writer.writerow([_cell(v) for v in ["rejected", r.name, r.city, r.country,
                                            r.reason, raw.get("phone", ""),
                                            raw.get("website", ""),
                                            raw.get("emails", ""),
                                            raw.get("source", ""),
                                            raw.get("dedupe_key", "")]])
    return buffer.getvalue()


def import_prospects_csv(session: Session, file_bytes: bytes, trade_hint: str = "") -> dict:
    """Save the upload, run it through the CSV provider + normal prospector
    filters/dedupe. Enrichment/verification stay with the /find flow."""
    from engine.prospector import run_prospecting

    imports_dir = Path("data/imports")
    imports_dir.mkdir(parents=True, exist_ok=True)
    from engine.util import utcnow

    path = imports_dir / f"import_{utcnow():%Y%m%d_%H%M%S}.csv"
    path.write_bytes(file_bytes)
    summary = run_prospecting(session, trade_hint or "import", "", limit=10_000,
                              query=f"csv:{path}")
    log_event(session, "admin", f"CSV import {path.name}: {summary}", meta=summary)
    return summary


def purge_dry_run(session: Session) -> dict:
    """Delete DRY_RUN artifacts: dry touches, outbox .eml files, and roll
    prospects whose only 'contact' was dry back to their real pipeline state."""
    dry_touches = [t for t in session.execute(select(Touch)).scalars()
                   if (t.meta_json or {}).get("dry")
                   or (t.meta_json or {}).get("cancel_reason") == "dry-run artifact"]
    affected = {t.prospect_id for t in dry_touches}
    for touch in dry_touches:
        session.delete(touch)
    session.flush()

    reverted = 0
    for pid in affected:
        prospect = session.get(Prospect, pid)
        if not prospect:
            continue
        remaining = [t for t in prospect.touches
                     if t.status in (TouchStatus.SENT, TouchStatus.QUEUED,
                                     TouchStatus.APPROVED, TouchStatus.DRAFT)]
        if not remaining and prospect.status in (ProspectStatus.CONTACTED,
                                                 ProspectStatus.QUEUED,
                                                 ProspectStatus.DRAFTED):
            prospect.status = ProspectStatus.VERIFIED
            reverted += 1
    session.commit()

    eml_deleted = 0
    if OUTBOX_DIR.exists():
        for eml in OUTBOX_DIR.glob("*.eml"):
            try:
                eml.unlink()
                eml_deleted += 1
            except OSError:
                pass

    result = {"touches_deleted": len(dry_touches), "eml_deleted": eml_deleted,
              "prospects_reverted": reverted}
    log_event(session, "admin", f"Purged DRY_RUN artifacts: {result}", meta=result)
    return result
