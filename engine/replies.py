"""Module 7, Reply Watcher: IMAP poll -> classify -> Telegram alert with
suggested reply. STOP/unsubscribe -> suppression forever. Bounce -> form path.
"""
import asyncio
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from bot.notify import notify
from db.models import (Prospect, ProspectStatus, Reply, ReplyClass, Touch,
                       TouchStatus)
from engine.events import log_event
from engine.llm import LLMError, llm_chat_json, llm_chat_text
from engine.sender import add_suppression, cancel_sequence, record_bounce

log = logging.getLogger("replies")

BOUNCE_FROM_MARKERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
BOUNCE_SUBJECT_RE = re.compile(
    r"undeliver|delivery status|delivery failure|returned mail|failure notice|"
    r"delivery incomplete|address not found", re.I,
)
AUTO_REPLY_RE = re.compile(
    r"out of (the )?office|auto[ -]?reply|automatic reply|vacation reply|away from",
    re.I,
)
STOP_RE = re.compile(r"\bunsubscribe\b|\bopt[ -]?out\b|^\s*stop\b|\breply stop\b", re.I | re.M)
EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def fetch_unseen() -> list[dict]:
    """Blocking IMAP fetch (run via asyncio.to_thread). Returns simple dicts.

    Does NOT mark messages seen: that happens only after successful handling,
    so a crash mid-processing can never eat a STOP reply or a bounce notice.
    """
    from engine.connections import resolve_email

    email_cfg = resolve_email()
    if not email_cfg.imap_host or not email_cfg.imap_user:
        return []
    from imap_tools import AND, MailBox

    messages = []
    with MailBox(email_cfg.imap_host).login(
        email_cfg.imap_user, email_cfg.imap_password
    ) as mailbox:
        for msg in mailbox.fetch(AND(seen=False), mark_seen=False, bulk=True):
            messages.append(
                {
                    "uid": msg.uid,
                    "from": (msg.from_ or "").lower(),
                    "subject": msg.subject or "",
                    "text": (msg.text or msg.html or "")[:8000],
                }
            )
    return messages


def mark_seen(uids: list[str]) -> None:
    """Blocking: flag successfully handled messages as seen."""
    from engine.connections import resolve_email

    email_cfg = resolve_email()
    if not uids or not email_cfg.imap_host:
        return
    from imap_tools import MailBox, MailMessageFlags

    with MailBox(email_cfg.imap_host).login(
        email_cfg.imap_user, email_cfg.imap_password
    ) as mailbox:
        mailbox.flag(uids, [MailMessageFlags.SEEN], True)


def quick_classify(from_addr: str, subject: str, text: str) -> str | None:
    """Deterministic classification first; None means 'ask the LLM'."""
    if any(m in from_addr for m in BOUNCE_FROM_MARKERS) or BOUNCE_SUBJECT_RE.search(subject):
        return ReplyClass.BOUNCE
    if STOP_RE.search(text[:500]) or STOP_RE.search(subject):
        return ReplyClass.UNSUBSCRIBE
    if AUTO_REPLY_RE.search(subject) or AUTO_REPLY_RE.search(text[:300]):
        return ReplyClass.AUTO_REPLY
    return None


async def classify_with_llm(text: str) -> str:
    try:
        data = await llm_chat_json(
            "classifier",
            "You classify replies to a cold email about an AI missed-call text-back "
            "service for home-service businesses. Reply ONLY with JSON.",
            f"Reply text:\n---\n{text[:2000]}\n---\n"
            f'Classify into exactly one of {ReplyClass.ALL}. '
            'JSON: {"classification": "<label>"}',
            required_keys=["classification"],
            temperature=0.1,
        )
        label = str(data["classification"]).strip().upper()
        return label if label in ReplyClass.ALL else ReplyClass.QUESTION
    except LLMError:
        return ReplyClass.QUESTION


async def suggest_reply_text(prospect: Prospect, text: str) -> str:
    from engine.campaign import get_campaign

    campaign = get_campaign()
    first_name = campaign.sender_name.split()[0] if campaign.sender_name else "me"
    try:
        return (await llm_chat_text(
            "writer",
            f"You draft short reply emails for {campaign.sender_name} of "
            f"{campaign.company}, who sells {campaign.product_pitch} to "
            f"{campaign.target_niche}. Plain text, under 100 words, no dashes of "
            "any kind, no emojis, friendly and direct. If they show interest, "
            f"propose a quick call via {campaign.calendar_url}. Sign as {first_name}.",
            f"They are {prospect.name}, a {prospect.trade} company in "
            f"{(prospect.city or '').title()}. Their reply:\n---\n{text[:2000]}\n---\n"
            "Write the reply email body only.",
            temperature=0.5,
        )).strip()
    except LLMError as exc:
        log.warning("suggest_reply failed: %s", exc)
        return ""


def _match_prospect(session: Session, from_addr: str) -> Prospect | None:
    match = EMAIL_IN_TEXT_RE.search(from_addr)
    addr = match.group(0).lower() if match else from_addr
    return session.execute(
        select(Prospect).where(Prospect.email == addr)
    ).scalar_one_or_none()


def _match_bounced_prospect(session: Session, text: str) -> Prospect | None:
    candidates = set(e.lower() for e in EMAIL_IN_TEXT_RE.findall(text))
    if not candidates:
        return None
    return session.execute(
        select(Prospect).where(Prospect.email.in_(candidates))
    ).scalars().first()


async def handle_unsubscribe(session: Session, prospect: Prospect) -> None:
    add_suppression(session, prospect.email, "STOP/unsubscribe reply")
    prospect.status = ProspectStatus.SUPPRESSED
    cancel_sequence(session, prospect.id, "unsubscribed")
    session.commit()
    log_event(session, "replies", f"{prospect.name} unsubscribed, suppressed forever")


async def handle_message(session: Session, msg: dict) -> None:
    from_addr, subject, text = msg["from"], msg["subject"], msg["text"]
    label = quick_classify(from_addr, subject, text)

    if label == ReplyClass.BOUNCE:
        prospect = _match_bounced_prospect(session, text)
        if prospect:
            session.add(Reply(prospect_id=prospect.id, classification=label,
                              raw_text=text[:5000]))
            session.commit()
            await record_bounce(session, prospect)
            await notify(f"Bounce: {prospect.name} <{prospect.email}>. "
                         f"Email marked invalid, prospect moved to contact form path.")
        else:
            log_event(session, "replies",
                      f"Bounce from {from_addr}, no matching prospect", level="WARNING")
        return

    prospect = _match_prospect(session, from_addr)
    if prospect is None:
        log_event(session, "replies",
                  f"Reply from unknown sender {from_addr}: {subject!r}", level="INFO")
        return

    if label is None:
        label = await classify_with_llm(text)

    suggested = ""
    if label in (ReplyClass.INTERESTED, ReplyClass.QUESTION):
        suggested = await suggest_reply_text(prospect, text)

    reply = Reply(prospect_id=prospect.id, classification=label,
                  raw_text=text[:5000], suggested_reply=suggested or None)
    session.add(reply)
    session.commit()

    if label == ReplyClass.UNSUBSCRIBE:
        await handle_unsubscribe(session, prospect)
        await notify(f"Unsubscribe from {prospect.name}. Suppressed forever.")
        return
    if label == ReplyClass.AUTO_REPLY:
        log_event(session, "replies", f"Auto-reply from {prospect.name}, ignored")
        return

    cancel_sequence(session, prospect.id, f"reply received ({label})")
    prospect.status = (ProspectStatus.INTERESTED if label == ReplyClass.INTERESTED
                       else ProspectStatus.REPLIED)
    session.commit()
    log_event(session, "replies", f"Reply from {prospect.name}: {label}")

    markup = None
    if suggested:
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Approve send", callback_data=f"rok:{reply.id}"),
                InlineKeyboardButton(text="Edit", callback_data=f"redit:{reply.id}"),
                InlineKeyboardButton(text="Ignore", callback_data=f"rignore:{reply.id}"),
            ]])
        except ImportError:
            markup = None
    alert = (f"{label}: {prospect.name} ({(prospect.city or '').title()}, {prospect.trade})\n"
             f"From: {prospect.email}\n"
             f"----\n{text[:1200]}\n----\n")
    if suggested:
        alert += f"\nSuggested reply:\n{suggested[:1200]}"
    await notify(alert, reply_markup=markup)


_fail_counts: dict[str, int] = {}
MAX_HANDLE_ATTEMPTS = 3


async def process_inbox() -> None:
    """Scheduler job: poll IMAP, handle everything new, mark seen only on success."""
    from db.session import new_session

    try:
        messages = await asyncio.to_thread(fetch_unseen)
    except Exception as exc:  # noqa: BLE001 (IMAP hiccups must not kill the loop)
        log.warning("IMAP poll failed: %s", exc)
        return
    if not messages:
        return
    session = new_session()
    handled_uids: list[str] = []
    try:
        for msg in messages:
            uid = msg.get("uid")
            try:
                await handle_message(session, msg)
                if uid:
                    handled_uids.append(uid)
                    _fail_counts.pop(uid, None)
            except Exception as exc:  # noqa: BLE001
                log.exception("handling reply failed: %s", exc)
                session.rollback()
                if uid:
                    _fail_counts[uid] = _fail_counts.get(uid, 0) + 1
                    if _fail_counts[uid] >= MAX_HANDLE_ATTEMPTS:
                        handled_uids.append(uid)  # stop retrying, but alert loudly
                        log_event(session, "replies",
                                  f"Giving up on inbox message uid {uid} after "
                                  f"{MAX_HANDLE_ATTEMPTS} attempts: {exc}", level="ERROR")
                        await notify(f"Reply watcher could not process message uid {uid} "
                                     f"({MAX_HANDLE_ATTEMPTS} attempts). Check the inbox "
                                     f"manually: {msg.get('subject', '')[:100]}")
    finally:
        session.close()
    if handled_uids:
        try:
            await asyncio.to_thread(mark_seen, handled_uids)
        except Exception as exc:  # noqa: BLE001 (worst case: reprocess next poll)
            log.warning("mark_seen failed (messages will be reprocessed): %s", exc)


async def send_suggested_reply(session: Session, reply: Reply, body: str) -> tuple[bool, str]:
    """Queue an approved reply as a REPLY_OUT touch.

    Deliberately NOT sent inline: sender_tick delivers it, so pause flag,
    circuit breaker, send window, daily caps, jitter, suppression and mode
    gating ALL apply to human-approved replies too.
    """
    from engine.util import utcnow

    touch = Touch(
        prospect_id=reply.prospect_id,
        type="REPLY_OUT",
        subject=f"re: {reply.prospect.touches[0].subject if reply.prospect.touches else 'your message'}",
        body=body,
        status=TouchStatus.QUEUED,
        scheduled_at=utcnow(),
    )
    session.add(touch)
    reply.handled = True
    session.commit()
    log_event(session, "replies",
              f"Approved reply to {reply.prospect.name} queued as touch #{touch.id} "
              f"(sender rails apply)")
    return True, "queued"
