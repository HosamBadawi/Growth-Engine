"""Module 6 — Sender. Deliverability is sacred.

Hard rails (code constants, env can only tighten them):
- Warm-up ramp: day 1-7 max 10/day, 8-14 max 20/day, then 30/day.
- Send window 9:00-16:30 US Eastern, Mon-Fri, 3-12 min jitter between sends.
- CAN-SPAM footer on every email. Suppression checked before EVERY send.
- Bounce circuit breaker: >3% over trailing 50 sends pauses everything.
Modes: DRY_RUN (default, .eml to /outbox), SANDBOX (only to my inbox), LIVE.
"""
import logging
import random
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.orm import Session

from bot.notify import notify
from db.models import (Prospect, ProspectStatus, Suppression, Touch,
                       TouchStatus, TouchType, VerificationLevel)
from engine.config import (HARD_JITTER_MIN_SECONDS, HARD_MAX_DAILY,
                           HARD_SEND_TIMEZONE, HARD_SEND_WINDOW_END,
                           HARD_SEND_WINDOW_START, HARD_WARMUP_SCHEDULE,
                           HARD_WARMUP_VOLUME, get_settings)
from engine.events import log_event
from engine.state import (KEY_FIRST_SEND_DATE, KEY_NEXT_SEND_AT, get_state,
                          is_live_confirmed, is_paused, set_paused, set_state)
from engine.util import parse_hhmm, utcnow
from engine.writer import render_email_template, render_footer

log = logging.getLogger("sender")

OUTBOX_DIR = Path("outbox")
DRY_RUN_BATCH_PER_TICK = 5


# ── caps & window (pure functions, unit-tested) ──────────────────────────────

def warmup_cap(day: int) -> int:
    """Hard warm-up cap for 1-based day since first real send."""
    for through_day, cap in HARD_WARMUP_SCHEDULE:
        if day <= through_day:
            return cap
    return HARD_MAX_DAILY


def volume_warmup_cap(total_real_sent: int) -> int:
    """Hard cap by cumulative real sends, so pauses cannot calendar-skip warm-up."""
    for below_total, cap in HARD_WARMUP_VOLUME:
        if total_real_sent < below_total:
            return cap
    return HARD_MAX_DAILY


def effective_daily_cap(day: int, configured: int | None = None,
                        total_real_sent: int | None = None) -> int:
    """Env cap can lower but never raise the hard warm-up cap."""
    hard = warmup_cap(day)
    if total_real_sent is not None:
        hard = min(hard, volume_warmup_cap(total_real_sent))
    if configured and configured > 0:
        return min(configured, hard)
    return hard


def in_send_window(now_utc: datetime | None = None) -> bool:
    """Hard rail: 9:00-16:30 US Eastern, Mon-Fri. Env values can only NARROW it."""
    settings = get_settings()
    now_utc = now_utc or utcnow()
    aware = now_utc.replace(tzinfo=timezone.utc)

    hard_local = aware.astimezone(ZoneInfo(HARD_SEND_TIMEZONE))
    if hard_local.weekday() >= 5:  # Sat/Sun in US Eastern, always blocked
        return False
    if not (parse_hhmm(HARD_SEND_WINDOW_START) <= hard_local.time()
            <= parse_hhmm(HARD_SEND_WINDOW_END)):
        return False

    local = aware.astimezone(ZoneInfo(settings.send_timezone))
    if local.weekday() >= 5:
        return False
    from engine.rails import eff_window

    window_start, window_end = eff_window()
    start = parse_hhmm(window_start)
    end = parse_hhmm(window_end)
    return start <= local.time() <= end


def compute_jitter_seconds() -> int:
    from engine.rails import eff_jitter

    jitter_min, jitter_max = eff_jitter()
    lo = max(jitter_min * 60, HARD_JITTER_MIN_SECONDS)
    hi = max(jitter_max * 60, lo)
    return random.randint(lo, hi)


# ── suppression ──────────────────────────────────────────────────────────────

def is_suppressed(session: Session, email: str | None) -> bool:
    if not email:
        return False
    return session.get(Suppression, email.lower()) is not None


def add_suppression(session: Session, email: str, reason: str) -> None:
    email = email.lower()
    if not session.get(Suppression, email):
        session.add(Suppression(email=email, reason=reason))
        session.commit()
        log_event(session, "sender", f"Suppressed {email} forever ({reason})")


def skip_reason(session: Session, prospect: Prospect) -> str | None:
    """Why this prospect must NOT be emailed right now (checked before EVERY send)."""
    if not prospect.email:
        return "no email address"
    if is_suppressed(session, prospect.email):
        return "suppressed"
    if prospect.email_verification_level == VerificationLevel.FAILED:
        return "email failed verification"
    if prospect.status == ProspectStatus.SUPPRESSED:
        return "prospect suppressed"
    return None


# ── counters & state ─────────────────────────────────────────────────────────

def _local_day_start_utc() -> datetime:
    settings = get_settings()
    tz = ZoneInfo(settings.send_timezone)
    start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def sent_today_count(session: Session) -> int:
    rows = session.execute(
        select(Touch).where(
            Touch.status.in_([TouchStatus.SENT, TouchStatus.BOUNCED]),
            Touch.sent_at >= _local_day_start_utc(),
        )
    ).scalars().all()
    return sum(1 for t in rows if not (t.meta_json or {}).get("dry"))


def total_real_sent_count(session: Session) -> int:
    rows = session.execute(
        select(Touch).where(Touch.status.in_([TouchStatus.SENT, TouchStatus.BOUNCED]),
                            Touch.sent_at.isnot(None))
    ).scalars().all()
    return sum(1 for t in rows if not (t.meta_json or {}).get("dry"))


def warmup_day(session: Session) -> int:
    settings = get_settings()
    first = get_state(session, KEY_FIRST_SEND_DATE)
    today = datetime.now(ZoneInfo(settings.send_timezone)).date()
    if not first:
        return 1
    return max(1, (today - datetime.fromisoformat(first).date()).days + 1)


# ── sequences ────────────────────────────────────────────────────────────────

def create_followups(session: Session, prospect: Prospect, base_time: datetime) -> list[Touch]:
    """Queue day 3 / 6 / 10 follow-ups, rendered now from the editable templates."""
    from engine.writer import SEQUENCE_TEMPLATES  # local import avoids cycle at module load

    card = (prospect.intel_json or {}).get("card")
    created = []
    for touch_type, offset in TouchType.FOLLOWUP_OFFSETS_DAYS.items():
        subject, body = render_email_template(SEQUENCE_TEMPLATES[touch_type], prospect, card)
        touch = Touch(
            prospect_id=prospect.id,
            type=touch_type,
            subject=subject,
            body=body,
            status=TouchStatus.QUEUED,
            scheduled_at=base_time + timedelta(days=offset),
        )
        session.add(touch)
        created.append(touch)
    session.commit()
    log_event(session, "sender",
              f"Queued {len(created)} follow-ups for {prospect.name} (day 3/6/10)")
    return created


def cancel_sequence(session: Session, prospect_id: int, reason: str) -> int:
    """Cancel all pending sequence touches for a prospect (called on any reply)."""
    touches = session.execute(
        select(Touch).where(
            Touch.prospect_id == prospect_id,
            Touch.status.in_([TouchStatus.QUEUED, TouchStatus.PAUSED, TouchStatus.APPROVED,
                              TouchStatus.DRAFT]),
            Touch.type.in_(TouchType.SEQUENCE),
        )
    ).scalars().all()
    for touch in touches:
        touch.status = TouchStatus.CANCELLED
        touch.meta_json = {**(touch.meta_json or {}), "cancel_reason": reason}
    if touches:
        session.commit()
        log_event(session, "sender",
                  f"Cancelled {len(touches)} pending touches for prospect "
                  f"#{prospect_id} ({reason})")
    return len(touches)


def queue_approved(session: Session) -> int:
    """Move APPROVED drafts into the send queue (scheduled now, gates apply later)."""
    touches = session.execute(
        select(Touch).where(Touch.status == TouchStatus.APPROVED)
    ).scalars().all()
    now = utcnow()
    for touch in touches:
        touch.status = TouchStatus.QUEUED
        touch.scheduled_at = touch.scheduled_at or now
        if touch.prospect.status in (ProspectStatus.DRAFTED, ProspectStatus.VERIFIED):
            touch.prospect.status = ProspectStatus.QUEUED
    session.commit()
    if touches:
        log_event(session, "sender", f"Queued {len(touches)} approved drafts for sending")
    return len(touches)


# ── bounce circuit breaker ───────────────────────────────────────────────────

async def check_bounce_breaker(session: Session) -> bool:
    settings = get_settings()
    rows = session.execute(
        select(Touch)
        .where(Touch.status.in_([TouchStatus.SENT, TouchStatus.BOUNCED]),
               Touch.sent_at.isnot(None))
        .order_by(Touch.sent_at.desc())
    ).scalars().all()
    from engine.rails import eff_breaker

    breaker_rate, breaker_window = eff_breaker()
    real = [t for t in rows if not (t.meta_json or {}).get("dry")]
    window = real[:breaker_window]
    if len(window) < 3:
        return False
    bounced = sum(1 for t in window if t.status == TouchStatus.BOUNCED)
    rate = bounced / len(window)
    if rate > breaker_rate:
        set_paused(session, True)
        msg = (f"BOUNCE CIRCUIT BREAKER: {bounced}/{len(window)} bounces "
               f"({rate:.1%}) over trailing sends. ALL SENDING PAUSED. "
               f"Investigate, then /resume all.")
        log_event(session, "sender", msg, level="ERROR")
        await notify(msg)
        return True
    return False


async def record_bounce(session: Session, prospect: Prospect,
                        touch: Touch | None = None) -> None:
    """Mark the bounced touch, invalidate email, fall back to form path."""
    if touch is None:
        touch = session.execute(
            select(Touch).where(Touch.prospect_id == prospect.id,
                                Touch.status == TouchStatus.SENT)
            .order_by(Touch.sent_at.desc()).limit(1)
        ).scalar_one_or_none()
    if touch:
        touch.status = TouchStatus.BOUNCED
        if touch.sent_at is None:
            touch.sent_at = utcnow()  # visible to the trailing-window breaker
    prospect.email_verification_level = VerificationLevel.FAILED
    prospect.status = ProspectStatus.FORM_ONLY
    cancel_sequence(session, prospect.id, "bounced")
    session.commit()
    log_event(session, "sender",
              f"Bounce recorded for {prospect.name} <{prospect.email}>, "
              f"reverted to contact form path", level="WARNING")
    await check_bounce_breaker(session)


# ── the actual send ──────────────────────────────────────────────────────────

def _smtp_error_code(exc: Exception) -> int | None:
    """Extract an SMTP status code from aiosmtplib exception shapes."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    recipients = getattr(exc, "recipients", None)  # SMTPRecipientsRefused
    if recipients:
        first = recipients[0] if isinstance(recipients, (list, tuple)) else None
        code = getattr(first, "code", None)
        if isinstance(code, int):
            return code
    return None


def _build_message(touch: Touch, to_addr: str, subject: str) -> EmailMessage:
    from engine.connections import resolve_email

    email_cfg = resolve_email()
    msg = EmailMessage()
    msg["From"] = formataddr((email_cfg.from_name, email_cfg.from_email or "dryrun@localhost"))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=(email_cfg.from_email.split("@")[-1]
                                           if "@" in email_cfg.from_email else "localhost"))
    if email_cfg.from_email:
        msg["List-Unsubscribe"] = f"<mailto:{email_cfg.from_email}?subject=unsubscribe>"
    msg.set_content(f"{touch.body}\n\n{render_footer()}")
    return msg


async def send_touch(session: Session, touch: Touch) -> tuple[bool, str]:
    """Send one touch according to ENGINE_MODE. Returns (ok, error)."""
    settings = get_settings()
    mode = settings.engine_mode.upper()
    prospect = touch.prospect

    reason = skip_reason(session, prospect)
    if reason:
        touch.status = TouchStatus.CANCELLED
        touch.meta_json = {**(touch.meta_json or {}), "cancel_reason": reason}
        session.commit()
        log_event(session, "sender",
                  f"Refused send to {prospect.name}: {reason}", level="WARNING")
        return False, reason

    to_addr, subject = prospect.email, touch.subject or "(no subject)"

    if mode == "DRY_RUN":
        msg = _build_message(touch, to_addr, subject)
        OUTBOX_DIR.mkdir(exist_ok=True)
        fname = OUTBOX_DIR / f"{utcnow():%Y%m%d_%H%M%S}_{touch.id}_{touch.type}.eml"
        fname.write_bytes(bytes(msg))
        touch.status = TouchStatus.SENT
        touch.sent_at = utcnow()
        touch.meta_json = {**(touch.meta_json or {}), "dry": True, "eml": str(fname)}
        _after_send(session, touch)
        log_event(session, "sender", f"DRY_RUN: wrote {fname.name} for {prospect.name}")
        return True, ""

    if mode == "SANDBOX":
        if not settings.sandbox_recipient:
            return False, "SANDBOX_RECIPIENT not set"
        subject = f"[SANDBOX for {to_addr}] {subject}"
        to_addr = settings.sandbox_recipient
    elif mode == "LIVE":
        if not is_live_confirmed(session):
            msg_txt = "LIVE mode set but not confirmed. Run /golive in Telegram first."
            log_event(session, "sender", msg_txt, level="ERROR")
            await notify(msg_txt)
            return False, "LIVE not confirmed"
    else:
        return False, f"Unknown ENGINE_MODE '{mode}'"

    from engine.connections import resolve_email

    email_cfg = resolve_email(session)
    if not email_cfg.smtp_host or not email_cfg.smtp_user:
        return False, "SMTP not configured (admin page or .env)"

    msg = _build_message(touch, to_addr, subject)
    try:
        await aiosmtplib.send(
            msg,
            hostname=email_cfg.smtp_host,
            port=email_cfg.smtp_port,
            username=email_cfg.smtp_user,
            password=email_cfg.smtp_password,
            start_tls=(email_cfg.smtp_port == 587),
            use_tls=(email_cfg.smtp_port == 465),
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 — classify, then FAILED or BOUNCED
        code = _smtp_error_code(exc)
        touch.meta_json = {**(touch.meta_json or {}), "error": str(exc)[:500],
                           **({"smtp_code": code} if code else {})}
        if code and 500 <= code < 600:
            # Permanent rejection = a bounce: must feed the circuit breaker.
            session.commit()
            log_event(session, "sender",
                      f"Permanent SMTP rejection ({code}) for {prospect.name}: {exc}",
                      level="ERROR")
            await record_bounce(session, prospect, touch=touch)
            return False, f"permanent rejection {code}"
        touch.status = TouchStatus.FAILED
        session.commit()
        log_event(session, "sender",
                  f"Send FAILED to {prospect.name}: {exc}", level="ERROR")
        return False, str(exc)

    touch.status = TouchStatus.SENT
    touch.sent_at = utcnow()
    _after_send(session, touch)
    log_event(session, "sender", f"{mode}: sent {touch.type} to {prospect.name} <{to_addr}>")
    return True, ""


def _after_send(session: Session, touch: Touch) -> None:
    prospect = touch.prospect
    if touch.type == TouchType.EMAIL_1:
        prospect.status = ProspectStatus.CONTACTED
        followups = create_followups(session, prospect, touch.sent_at)
        if (touch.meta_json or {}).get("dry"):
            # Dry sends spawn dry follow-ups; real modes cancel them on sight so a
            # DRY_RUN week can never leak "following up" emails after going LIVE.
            for followup in followups:
                followup.meta_json = {**(followup.meta_json or {}), "dry": True}
    elif touch.type in TouchType.SEQUENCE:
        if prospect.status == ProspectStatus.QUEUED:
            prospect.status = ProspectStatus.CONTACTED
    session.commit()


# ── scheduler tick ───────────────────────────────────────────────────────────

async def sender_tick() -> None:
    """Runs every minute. Sends at most one email per tick in real modes."""
    from db.session import new_session

    session = new_session()
    try:
        settings = get_settings()
        mode = settings.engine_mode.upper()
        if is_paused(session):
            return
        now = utcnow()
        due = session.execute(
            select(Touch)
            .where(Touch.status == TouchStatus.QUEUED, Touch.scheduled_at <= now)
            .order_by(Touch.scheduled_at)
        ).scalars().all()
        if not due:
            return

        if mode != "DRY_RUN":
            if not in_send_window(now):
                return
            gate = get_state(session, KEY_NEXT_SEND_AT)
            if gate and now < datetime.fromisoformat(gate):
                return
            from engine.rails import eff_daily_cap

            cap = effective_daily_cap(warmup_day(session), eff_daily_cap(),
                                      total_real_sent_count(session))
            if sent_today_count(session) >= cap:
                return

        sent = 0
        for touch in due:
            if mode != "DRY_RUN":
                if is_paused(session):  # breaker may fire mid-loop
                    break
                if (touch.meta_json or {}).get("dry"):
                    touch.status = TouchStatus.CANCELLED
                    touch.meta_json = {**touch.meta_json, "cancel_reason": "dry-run artifact"}
                    session.commit()
                    log_event(session, "sender",
                              f"Cancelled DRY_RUN artifact touch #{touch.id} "
                              f"({touch.type} for prospect #{touch.prospect_id})")
                    continue

            ok, _err = await send_touch(session, touch)

            if mode != "DRY_RUN":
                attempted_smtp = ok or touch.status in (TouchStatus.FAILED,
                                                        TouchStatus.BOUNCED)
                if ok and not get_state(session, KEY_FIRST_SEND_DATE):
                    today_local = datetime.now(ZoneInfo(settings.send_timezone)).date()
                    set_state(session, KEY_FIRST_SEND_DATE, today_local.isoformat())
                if attempted_smtp:
                    # Success AND failure both consume the tick + jitter gate, so a
                    # rejecting provider can never trigger a rapid-fire cascade.
                    set_state(
                        session, KEY_NEXT_SEND_AT,
                        (now + timedelta(seconds=compute_jitter_seconds())).isoformat(),
                    )
                    break
                continue  # touch was cancelled (suppressed etc.), try next candidate

            if ok:
                sent += 1
            if sent >= DRY_RUN_BATCH_PER_TICK:
                break
    finally:
        session.close()
