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
from engine.stages import (STAGE_LABELS, STAGE_PROSPECTS, STAGE_REPLIES,
                           STAGES, closed_reason, derive_stage, next_touch_at,
                           prospects_for_stage, search_all_stages,
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


@router.get("/{stage}")
async def pipeline_stage(request: Request, stage: str):
    if stage not in STAGES:
        return RedirectResponse("/pipeline", status_code=303)
    session = new_session()
    try:
        prospects = prospects_for_stage(session, stage)
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
        })
    finally:
        session.close()
