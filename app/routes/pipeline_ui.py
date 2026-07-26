"""Pipeline-stage pages: each prospect appears on exactly one page.

Replaces the single Leads table. Stages are derived (engine/stages.py), never
stored. /pipeline redirects to Replies when there is money on the table,
else to Prospects.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.auth import require_auth
from app.routes.dashboard import templates
from db.session import new_session
from engine.stages import (SEGMENT_LABELS, STAGE_LABELS, STAGE_PROSPECTS,
                           STAGE_REPLIES, STAGES, closed_reason, derive_stage,
                           next_touch_at, prospects_for_stage,
                           search_all_stages, segment_counts,
                           sequence_position, stage_counts)

router = APIRouter(prefix="/pipeline", dependencies=[Depends(require_auth)])


@router.get("")
async def pipeline_home():
    session = new_session()
    try:
        counts = stage_counts(session)
    finally:
        session.close()
    # Replies is the money page: land there whenever it is non-empty.
    target = STAGE_REPLIES if counts.get(STAGE_REPLIES) else STAGE_PROSPECTS
    return RedirectResponse(f"/pipeline/{target}", status_code=303)


@router.get("/search")
async def pipeline_search(request: Request, q: str = ""):
    session = new_session()
    try:
        hits = search_all_stages(session, q)
        counts = stage_counts(session)
        rows = [{
            "p": prospect, "stage": stage, "stage_label": STAGE_LABELS[stage],
        } for prospect, stage in hits]
        return templates.TemplateResponse(request, "pipeline_search.html", {
            "rows": rows, "q": q, "counts": counts, "stage": None,
            "labels": STAGE_LABELS, "active": "pipeline",
        })
    finally:
        session.close()


# Registered BEFORE the catch-all /{stage} route: FastAPI matches in
# registration order, so '/pipeline/runs' would otherwise be read as a stage.
@router.get("/runs")
async def runs_page(request: Request, msg: str = ""):
    """Every find run, with its summary and an export including what it threw away."""
    from sqlalchemy import select

    from db.models import FindRun, RejectedCandidate

    session = new_session()
    try:
        runs = session.execute(
            select(FindRun).order_by(FindRun.id.desc()).limit(50)
        ).scalars().all()
        rejects = session.execute(
            select(RejectedCandidate).order_by(RejectedCandidate.id.desc()).limit(200)
        ).scalars().all()
        return templates.TemplateResponse(request, "pipeline_runs.html", {
            "runs": runs, "rejects": rejects, "msg": msg,
            "active": "pipeline", "counts": stage_counts(session),
            "labels": STAGE_LABELS, "stage": None, "q": "",
        })
    finally:
        session.close()


@router.get("/{stage}")
async def pipeline_stage(request: Request, stage: str, segment: str = ""):
    if stage not in STAGES:
        return RedirectResponse("/pipeline", status_code=303)
    session = new_session()
    try:
        prospects = prospects_for_stage(session, stage, segment)
        counts = stage_counts(session)
        rows = []
        for prospect in prospects:
            row = {"p": prospect}
            if stage == "in_sequence":
                sent, total = sequence_position(prospect)
                row["sequence"] = f"touch {sent} of {total}"
                row["next_at"] = next_touch_at(prospect)
            elif stage == "closed":
                row["reason"] = closed_reason(prospect)
            elif stage == "replies":
                latest = max(prospect.replies, key=lambda r: r.received_at,
                             default=None)
                row["latest_reply"] = latest
            elif stage == "drafts":
                row["draft"] = next(
                    (t for t in prospect.touches
                     if t.status in ("DRAFT", "APPROVED")), None)
            rows.append(row)
        return templates.TemplateResponse(request, "pipeline_stage.html", {
            "rows": rows, "stage": stage, "labels": STAGE_LABELS,
            "counts": counts, "q": "", "active": "pipeline",
            "segment": segment, "segment_labels": SEGMENT_LABELS,
            "segment_counts": segment_counts(session, stage),
        })
    finally:
        session.close()


# ── Phase 3: getting the data out ────────────────────────────────────────────

def _csv_response(text: str, filename: str):
    """utf-8-sig: Excel needs the BOM or Portuguese/Arabic names become mojibake."""
    from fastapi.responses import Response

    return Response(
        content=text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/{stage}.csv")
async def export_stage(stage: str, q: str = "", segment: str = ""):
    """Export the view the operator is looking at, in the same order."""
    from engine.data_tools import export_prospect_view

    session = new_session()
    try:
        if stage == "search":
            # search_all_stages yields (prospect, stage) pairs; export the rows.
            rows = [pair[0] for pair in search_all_stages(session, q)]
            name = f"search-{q or 'all'}.csv"
        elif stage in STAGES:
            rows = prospects_for_stage(session, stage, segment)
            name = f"{stage}{'-' + segment if segment else ''}.csv"
        else:
            return RedirectResponse("/pipeline", status_code=303)
        return _csv_response(export_prospect_view(rows), name)
    finally:
        session.close()


@router.get("/runs/{run_id}.csv")
async def export_run(run_id: int):
    from engine.data_tools import export_run_csv

    session = new_session()
    try:
        return _csv_response(export_run_csv(session, run_id), f"run-{run_id}.csv")
    finally:
        session.close()


@router.post("/rejects/{reject_id}/retry")
async def retry_reject(reject_id: int):
    """Re-run discovery on one rejected candidate and keep it if it now resolves."""
    from urllib.parse import quote

    from db.models import Prospect, ProspectStatus, RejectedCandidate
    from engine.events import log_event
    from engine.prospector import _try_website
    from engine.providers.base import RawProspect, make_dedupe_key

    session = new_session()
    try:
        row = session.get(RejectedCandidate, reject_id)
        if not row:
            return RedirectResponse("/pipeline/runs?msg=not+found", status_code=303)
        raw_json = row.raw_json or {}
        raw = RawProspect(name=row.name, city=row.city or "",
                          country=row.country or "", phone=raw_json.get("phone", ""),
                          website=raw_json.get("website", ""),
                          address=raw_json.get("address", ""),
                          category=raw_json.get("category", ""),
                          emails=list(raw_json.get("emails") or []),
                          source=raw_json.get("source", "retry"))
        if not raw.website:
            raw.website = _try_website(raw)
        row.retried = True
        if not raw.website:
            session.commit()
            msg = f"No website found for {row.name}; still rejected."
        else:
            prospect = Prospect(
                name=raw.name, city=(raw.city or "").lower(), country=raw.country or None,
                phone=raw.phone, website=raw.website, trade=raw.category or None,
                email=(raw.emails[0].lower() if raw.emails else None),
                dedupe_key=make_dedupe_key("", raw.name, raw.city),
                source=raw.source, status=ProspectStatus.NEW,
                intel_json={"retried_from_reject": reject_id},
            )
            session.add(prospect)
            session.commit()
            log_event(session, "pipeline",
                      f"Recovered rejected candidate {row.name} -> {raw.website}")
            msg = f"{row.name} recovered: {raw.website}"
        return RedirectResponse(f"/pipeline/runs?msg={quote(msg[:200])}",
                                status_code=303)
    finally:
        session.close()
