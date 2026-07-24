"""All dashboard pages: overview, leads, outbox, activity, settings, login."""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.auth import COOKIE_NAME, is_authed, make_token, require_auth
from db.models import (Event, Prospect, ProspectStatus, Reply, Touch,
                       TouchStatus, TouchType)
from db.session import new_session
from engine.config import (HARD_MAX_DAILY, HARD_WARMUP_SCHEDULE, get_settings)
from engine.events import log_event
from engine.reporter import compute_stats, per_day_series
from engine.state import is_paused

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["mode_label"] = get_settings().engine_mode.upper()

from engine import __version__  # noqa: E402

templates.env.globals["version"] = __version__


def _campaign_placeholder() -> bool:
    from engine.campaign import get_campaign

    try:
        return get_campaign().is_placeholder
    except Exception:  # noqa: BLE001 — banner must never break a page render
        return False


templates.env.globals["campaign_placeholder"] = _campaign_placeholder

router = APIRouter()


# ── login ────────────────────────────────────────────────────────────────────

@router.get("/login")
async def login_page(request: Request):
    if is_authed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    if password == get_settings().dashboard_password:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, make_token(), httponly=True, max_age=86400 * 14)
        return response
    return templates.TemplateResponse(request, "login.html",
                                      {"error": "Wrong password"}, status_code=401)


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# ── overview ─────────────────────────────────────────────────────────────────

@router.get("/", dependencies=[Depends(require_auth)])
async def overview(request: Request):
    session = new_session()
    try:
        stats = compute_stats(session)
        series = per_day_series(session, days=30)
        funnel = {
            "labels": ["Found", "Verified", "Sent", "Replied", "Interested"],
            "values": [stats["prospects_found"], stats["verified"], stats["contacted"],
                       stats["replied"], stats["interested"] + stats["calls_booked"]],
        }
        return templates.TemplateResponse(request, "overview.html", {
            "stats": stats, "series": series, "funnel": funnel, "active": "overview",
        })
    finally:
        session.close()


# ── leads ────────────────────────────────────────────────────────────────────

@router.get("/leads", dependencies=[Depends(require_auth)])
async def leads(request: Request, status: str = "", trade: str = "", city: str = "",
                verification: str = "", q: str = ""):
    session = new_session()
    try:
        stmt = select(Prospect).order_by(Prospect.created_at.desc())
        if status:
            stmt = stmt.where(Prospect.status == status)
        if trade:
            stmt = stmt.where(Prospect.trade == trade)
        if city:
            stmt = stmt.where(Prospect.city == city)
        if verification:
            stmt = stmt.where(Prospect.email_verification_level == verification)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(Prospect.name).like(like)
                              | func.lower(Prospect.email).like(like))
        prospects = session.execute(stmt.limit(500)).scalars().all()
        trades = [r for (r,) in session.execute(
            select(Prospect.trade).distinct().where(Prospect.trade.isnot(None))).all()]
        cities = [r for (r,) in session.execute(
            select(Prospect.city).distinct().where(Prospect.city.isnot(None))).all()]
        return templates.TemplateResponse(request, "leads.html", {
            "prospects": prospects, "statuses": ProspectStatus.ALL,
            "trades": sorted(trades), "cities": sorted(cities),
            "f": {"status": status, "trade": trade, "city": city,
                  "verification": verification, "q": q},
            "active": "leads",
        })
    finally:
        session.close()


@router.get("/leads/{prospect_id}", dependencies=[Depends(require_auth)])
async def lead_detail(request: Request, prospect_id: int):
    session = new_session()
    try:
        prospect = session.get(Prospect, prospect_id)
        if not prospect:
            return RedirectResponse("/leads", status_code=303)
        next_touch = session.execute(
            select(Touch).where(Touch.prospect_id == prospect_id,
                                Touch.status == TouchStatus.QUEUED)
            .order_by(Touch.scheduled_at).limit(1)
        ).scalar_one_or_none()
        return templates.TemplateResponse(request, "lead_detail.html", {
            "p": prospect, "next_touch": next_touch, "active": "leads",
        })
    finally:
        session.close()


# ── outbox ───────────────────────────────────────────────────────────────────

@router.get("/outbox", dependencies=[Depends(require_auth)])
async def outbox(request: Request):
    session = new_session()
    try:
        drafts = session.execute(
            select(Touch).where(Touch.status == TouchStatus.DRAFT).order_by(Touch.id)
        ).scalars().all()
        approved = session.execute(
            select(Touch).where(Touch.status == TouchStatus.APPROVED).order_by(Touch.id)
        ).scalars().all()
        queued = session.execute(
            select(Touch).where(Touch.status.in_([TouchStatus.QUEUED, TouchStatus.PAUSED]))
            .order_by(Touch.scheduled_at).limit(100)
        ).scalars().all()
        return templates.TemplateResponse(request, "outbox.html", {
            "drafts": drafts, "approved": approved, "queued": queued, "active": "outbox",
        })
    finally:
        session.close()


@router.post("/outbox/{touch_id}/approve", dependencies=[Depends(require_auth)])
async def outbox_approve(touch_id: int):
    session = new_session()
    try:
        touch = session.get(Touch, touch_id)
        if touch and touch.status == TouchStatus.DRAFT:
            touch.status = TouchStatus.APPROVED
            session.commit()
            log_event(session, "dashboard", f"Draft #{touch_id} approved via dashboard")
    finally:
        session.close()
    return RedirectResponse("/outbox", status_code=303)


@router.post("/outbox/{touch_id}/skip", dependencies=[Depends(require_auth)])
async def outbox_skip(touch_id: int):
    session = new_session()
    try:
        touch = session.get(Touch, touch_id)
        if touch and touch.status in (TouchStatus.DRAFT, TouchStatus.APPROVED):
            touch.status = TouchStatus.SKIPPED
            session.commit()
            log_event(session, "dashboard", f"Draft #{touch_id} skipped via dashboard")
    finally:
        session.close()
    return RedirectResponse("/outbox", status_code=303)


@router.post("/outbox/{touch_id}/edit", dependencies=[Depends(require_auth)])
async def outbox_edit(touch_id: int, subject: str = Form(""), body: str = Form("")):
    session = new_session()
    try:
        touch = session.get(Touch, touch_id)
        if touch and touch.status in (TouchStatus.DRAFT, TouchStatus.APPROVED):
            touch.subject = subject.strip() or touch.subject
            touch.body = body.strip() or touch.body
            touch.meta_json = {**(touch.meta_json or {}), "edited": True}
            session.commit()
            log_event(session, "dashboard", f"Draft #{touch_id} edited via dashboard")
    finally:
        session.close()
    return RedirectResponse("/outbox", status_code=303)


# ── activity & settings ──────────────────────────────────────────────────────

@router.get("/activity", dependencies=[Depends(require_auth)])
async def activity(request: Request, module: str = ""):
    session = new_session()
    try:
        stmt = select(Event).order_by(Event.ts.desc()).limit(200)
        if module:
            stmt = stmt.where(Event.module == module)
        events = session.execute(stmt).scalars().all()
        modules = [m for (m,) in session.execute(select(Event.module).distinct()).all()]
        return templates.TemplateResponse(request, "activity.html", {
            "events": events, "modules": sorted(modules), "module": module,
            "active": "activity",
        })
    finally:
        session.close()


@router.get("/settings", dependencies=[Depends(require_auth)])
async def settings_page(request: Request):
    settings = get_settings()
    session = new_session()
    try:
        paused = is_paused(session)
    finally:
        session.close()
    return templates.TemplateResponse(request, "settings.html", {
        "s": settings, "paused": paused,
        "warmup": HARD_WARMUP_SCHEDULE, "hard_max": HARD_MAX_DAILY,
        "active": "settings",
    })
