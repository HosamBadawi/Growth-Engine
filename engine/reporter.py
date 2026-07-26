"""Reporter (module 8): daily Telegram summary + shared stats for the dashboard."""
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bot.notify import notify
from db.models import (Prospect, ProspectStatus, Reply, ReplyClass, Touch,
                       TouchStatus, VerificationLevel)
from engine.config import get_settings
from engine.state import is_paused
from engine.util import utcnow

log = logging.getLogger("reporter")

VERIFIED_LEVELS = (VerificationLevel.SYNTAX, VerificationLevel.MX, VerificationLevel.SMTP_OK)


def compute_stats(session: Session) -> dict:
    def count(stmt) -> int:
        return session.execute(stmt).scalar() or 0

    day_ago = utcnow() - timedelta(hours=24)
    sent_statuses = [TouchStatus.SENT, TouchStatus.BOUNCED]

    total_sent = count(select(func.count(Touch.id)).where(Touch.status.in_(sent_statuses)))
    bounced = count(select(func.count(Touch.id)).where(Touch.status == TouchStatus.BOUNCED))

    recent = session.execute(
        select(Touch.status).where(Touch.status.in_(sent_statuses), Touch.sent_at.isnot(None))
        .order_by(Touch.sent_at.desc()).limit(get_settings().bounce_breaker_window)
    ).scalars().all()
    bounce_rate = (sum(1 for s in recent if s == TouchStatus.BOUNCED) / len(recent)
                   if recent else 0.0)

    replies_by_class = dict(
        session.execute(
            select(Reply.classification, func.count(Reply.id)).group_by(Reply.classification)
        ).all()
    )

    return {
        "prospects_found": count(select(func.count(Prospect.id))),
        "found_today": count(select(func.count(Prospect.id))
                             .where(Prospect.created_at >= day_ago)),
        "enriched": count(select(func.count(Prospect.id))
                          .where(Prospect.status != ProspectStatus.NEW)),
        "verified": count(select(func.count(Prospect.id))
                          .where(Prospect.email_verification_level.in_(VERIFIED_LEVELS))),
        "form_only": count(select(func.count(Prospect.id))
                           .where(Prospect.status == ProspectStatus.FORM_ONLY)),
        "drafts_pending": count(select(func.count(Touch.id))
                                .where(Touch.status == TouchStatus.DRAFT)),
        "approved": count(select(func.count(Touch.id))
                          .where(Touch.status == TouchStatus.APPROVED)),
        "queued": count(select(func.count(Touch.id))
                        .where(Touch.status == TouchStatus.QUEUED)),
        "sent_today": count(select(func.count(Touch.id))
                            .where(Touch.status.in_(sent_statuses), Touch.sent_at >= day_ago)),
        "total_sent": total_sent,
        "bounced": bounced,
        "bounce_rate": round(bounce_rate * 100, 2),
        "replies_total": count(select(func.count(Reply.id))),
        "replies_by_class": replies_by_class,
        "interested": count(select(func.count(Prospect.id))
                            .where(Prospect.status == ProspectStatus.INTERESTED)),
        "calls_booked": count(select(func.count(Prospect.id))
                              .where(Prospect.status == ProspectStatus.CALL_BOOKED)),
        "contacted": count(select(func.count(Prospect.id)).where(
            Prospect.status.in_([ProspectStatus.CONTACTED, ProspectStatus.REPLIED,
                                 ProspectStatus.INTERESTED, ProspectStatus.CALL_BOOKED,
                                 ProspectStatus.WON]))),
        "replied": count(select(func.count(Prospect.id)).where(
            Prospect.status.in_([ProspectStatus.REPLIED, ProspectStatus.INTERESTED,
                                 ProspectStatus.CALL_BOOKED, ProspectStatus.WON]))),
        "paused": is_paused(session),
        "mode": get_settings().engine_mode.upper(),
    }


def per_day_series(session: Session, days: int = 30) -> dict:
    """Sends and replies per day for the line chart."""
    start = utcnow() - timedelta(days=days)
    sends: dict[str, int] = {}
    for (sent_at,) in session.execute(
        select(Touch.sent_at).where(Touch.sent_at >= start,
                                    Touch.status.in_([TouchStatus.SENT, TouchStatus.BOUNCED]))
    ).all():
        key = sent_at.date().isoformat()
        sends[key] = sends.get(key, 0) + 1
    replies: dict[str, int] = {}
    for (received_at,) in session.execute(
        select(Reply.received_at).where(Reply.received_at >= start)
    ).all():
        key = received_at.date().isoformat()
        replies[key] = replies.get(key, 0) + 1

    labels = [(utcnow() - timedelta(days=d)).date().isoformat() for d in range(days, -1, -1)]
    return {
        "labels": labels,
        "sent": [sends.get(day, 0) for day in labels],
        "replies": [replies.get(day, 0) for day in labels],
    }


def build_report_text(session: Session) -> str:
    s = compute_stats(session)
    replies = s["replies_by_class"]
    reply_lines = "\n".join(
        f"    {cls}: {replies[cls]}" for cls in ReplyClass.ALL if replies.get(cls)
    ) or "    none yet"
    return (
        f"Growth Engine daily report [{s['mode']}]"
        f"{' *** PAUSED ***' if s['paused'] else ''}\n"
        f"Found: {s['prospects_found']} total ({s['found_today']} today)\n"
        f"Enriched: {s['enriched']} | Verified: {s['verified']} | Form only: {s['form_only']}\n"
        f"Drafts awaiting approval: {s['drafts_pending']} | Approved: {s['approved']} "
        f"| Queued: {s['queued']}\n"
        f"Sent today: {s['sent_today']} | Total sent: {s['total_sent']}\n"
        f"Replies ({s['replies_total']}):\n{reply_lines}\n"
        f"Interested: {s['interested']} | Calls booked: {s['calls_booked']}\n"
        f"Bounce rate (trailing {get_settings().bounce_breaker_window}): {s['bounce_rate']}%"
    )


async def send_daily_report() -> None:
    from db.session import new_session

    session = new_session()
    try:
        await notify(build_report_text(session))
    finally:
        session.close()
