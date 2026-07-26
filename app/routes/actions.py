"""Web parity with the Telegram bot: every bot action available in the browser.

Long-running work (find, draft) runs as a background job with progress polled
by the page, so no HTTP request blocks.

The one deliberate exception is going LIVE: the web UI may INITIATE it, but the
confirmation must still arrive through Telegram to the owner account. Requiring
the operator's phone for the single most irreversible action is a feature.
"""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth import require_auth
from app.jobs import job_state, latest_job_id, start_job
from app.routes.dashboard import templates
from db.models import Touch, TouchStatus
from db.session import new_session
from engine.backfill import FILTERS as BACKFILL_FILTERS
from engine.config import get_settings
from engine.events import log_event
from engine.providers import provider_availability
from engine.reporter import build_report_text
from engine.stages import stage_counts
from engine.state import is_live_confirmed, is_paused, set_paused

router = APIRouter(prefix="/actions", dependencies=[Depends(require_auth)])


def _back(msg: str, job_id: int | None = None) -> RedirectResponse:
    suffix = f"&job={job_id}" if job_id else ""
    return RedirectResponse(f"/actions?msg={quote(msg[:300])}{suffix}", status_code=303)


@router.get("")
async def actions_page(request: Request, msg: str = "", job: int | None = None):
    settings = get_settings()
    session = new_session()
    try:
        counts = stage_counts(session)
        paused = is_paused(session)
        live_confirmed = is_live_confirmed(session)
        report = build_report_text(session)
        approved = session.execute(
            select(Touch).where(Touch.status == TouchStatus.APPROVED)
        ).scalars().all()
        drafts = session.execute(
            select(Touch).where(Touch.status == TouchStatus.DRAFT)
        ).scalars().all()
    finally:
        session.close()
    job_id = job or latest_job_id()
    return templates.TemplateResponse(request, "actions.html", {
        "msg": msg, "counts": counts, "paused": paused,
        "mode": settings.engine_mode.upper(), "live_confirmed": live_confirmed,
        "report": report, "approved_count": len(approved),
        "draft_count": len(drafts), "job_id": job_id, "active": "actions",
        "backfill_filters": BACKFILL_FILTERS,
        "providers": provider_availability(),
        "default_provider": settings.prospect_provider,
    })


@router.get("/job/{job_id}")
async def job_status(job_id: int) -> dict:
    """Polled by the Actions page for live progress."""
    state = job_state(job_id)
    if not state:
        return {"found": False}
    return {"found": True, "title": state["title"], "lines": state["lines"],
            "done": state["done"], "error": state["error"]}


@router.post("/find")
async def action_find(niche: str = Form(...), city: str = Form(...),
                      count: str = Form("10"), provider: str = Form("")):
    try:
        limit = max(1, min(int(count.strip() or 10), 100))
    except ValueError:
        return _back("Count must be a number")
    niche, city = niche.strip(), city.strip()
    if not niche or not city:
        return _back("Niche and city are required")

    from engine.pipeline import run_find

    async def factory(progress):
        return await run_find(niche, city, limit, progress,
                              provider_name=provider or None)

    job_id = start_job(f"find {niche} {city} {limit}", factory)
    return _back(f"Find run started for '{niche} {city}' (target {limit}).", job_id)


@router.post("/draft")
async def action_draft():
    from engine.pipeline import generate_all_drafts

    async def factory(progress):
        ids = await generate_all_drafts(progress)
        return f"{len(ids)} drafts created"

    job_id = start_job("generate drafts", factory)
    return _back("Draft generation started (local LLM, this can take a while).", job_id)


@router.post("/enrich")
async def action_enrich(filter_key: str = Form("no_website"),
                        count: str = Form("25")):
    """Phase 5 backfill from the browser: same ladder, budgets and pacing."""
    import asyncio

    from engine.backfill import FILTERS, run_backfill

    if filter_key not in FILTERS:
        return _back(f"Unknown filter '{filter_key}'.")
    try:
        limit = max(1, min(int(count), 200))
    except ValueError:
        return _back("Count must be a number.")

    async def factory(progress):
        # run_backfill is sync and runs in a worker thread, but `progress` is a
        # coroutine function owned by the event loop. Bridge them, or the
        # progress lines are silently dropped as un-awaited coroutines.
        loop = asyncio.get_running_loop()

        def sync_progress(text: str) -> None:
            asyncio.run_coroutine_threadsafe(progress(text), loop)

        session = new_session()
        try:
            summary = await asyncio.to_thread(run_backfill, session, filter_key,
                                              limit, sync_progress)
            return (f"processed {summary['processed']}, "
                    f"websites +{summary['websites_found']}, "
                    f"emails +{summary['emails_found']}, "
                    f"socials +{summary['socials_found']}, "
                    f"failed {summary['failed']}")
        finally:
            session.close()

    job_id = start_job(f"backfill: {FILTERS[filter_key]}", factory)
    return _back(f"Re-running discovery over up to {limit} prospects "
                 f"({FILTERS[filter_key]}). Paced, so give it time.", job_id)


@router.post("/queue")
async def action_queue():
    from engine.sender import queue_approved

    settings = get_settings()
    session = new_session()
    try:
        count = queue_approved(session)
        log_event(session, "dashboard", f"Queued {count} approved drafts via web UI")
    finally:
        session.close()
    mode = settings.engine_mode.upper()
    note = {"DRY_RUN": "DRY_RUN: .eml files go to /outbox, nothing is sent.",
            "SANDBOX": f"SANDBOX: everything goes to {settings.sandbox_recipient}.",
            "LIVE": "LIVE: real emails, within caps and window."}.get(mode, "")
    return _back(f"Queued {count} approved drafts. {note}")


@router.post("/pause")
async def action_pause():
    session = new_session()
    try:
        set_paused(session, True)
        log_event(session, "dashboard", "Sending PAUSED via web UI")
    finally:
        session.close()
    return _back("All sending paused.")


@router.post("/resume")
async def action_resume():
    session = new_session()
    try:
        set_paused(session, False)
        touches = session.execute(
            select(Touch).where(Touch.status == TouchStatus.PAUSED)
        ).scalars().all()
        for touch in touches:
            touch.status = TouchStatus.QUEUED
        session.commit()
        log_event(session, "dashboard",
                  f"Sending RESUMED via web UI, re-queued {len(touches)}")
    finally:
        session.close()
    return _back(f"Resumed. Re-queued {len(touches)} paused touches.")


@router.post("/golive-request")
async def action_golive_request():
    """Web may initiate; Telegram must confirm. Both keys stay required."""
    settings = get_settings()
    if settings.engine_mode.upper() != "LIVE":
        return _back("ENGINE_MODE is not LIVE. Set ENGINE_MODE=LIVE in .env and "
                     "restart first. Mode is never settable from the web UI.")

    from bot.notify import notify

    sent = await notify(
        "GO LIVE requested from the web dashboard.\n"
        "Send /golive here and confirm to enable real sending. "
        "Ignore this message if it was not you."
    )
    session = new_session()
    try:
        log_event(session, "dashboard", "Go-live confirmation requested via web UI",
                  level="WARNING")
    finally:
        session.close()
    if not sent:
        return _back("Telegram is not configured, so the confirmation cannot be "
                     "delivered. Configure a bot in Admin > Connections; going "
                     "LIVE deliberately requires your phone.")
    return _back("Confirmation request sent to Telegram. Approve it there to go LIVE.")
