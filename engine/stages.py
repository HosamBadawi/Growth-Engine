"""Pipeline stages: every prospect appears on exactly ONE page.

The stage is DERIVED from status + touch/reply state at read time, never
stored, so it can never drift out of sync with reality.

Precedence (first match wins):
  closed       terminal outcomes: interested/call booked/won/lost/suppressed,
               or a bounced touch (the reason is shown on the page)
  replies      any inbound non-bounce reply, sequence still working
  in_sequence  at least one touch sent, no reply yet
  drafts       a draft/approved touch awaiting action
  prospects    never contacted at all
"""
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import (Prospect, ProspectStatus, Reply, ReplyClass, Touch,
                       TouchStatus)

STAGE_PROSPECTS = "prospects"
STAGE_DRAFTS = "drafts"
STAGE_IN_SEQUENCE = "in_sequence"
STAGE_REPLIES = "replies"
STAGE_CLOSED = "closed"

STAGES = [STAGE_PROSPECTS, STAGE_DRAFTS, STAGE_IN_SEQUENCE, STAGE_REPLIES, STAGE_CLOSED]

STAGE_LABELS = {
    STAGE_PROSPECTS: "Prospects",
    STAGE_DRAFTS: "Drafts",
    STAGE_IN_SEQUENCE: "In Sequence",
    STAGE_REPLIES: "Replies",
    STAGE_CLOSED: "Closed",
}

_CLOSED_STATUSES = {
    ProspectStatus.INTERESTED, ProspectStatus.CALL_BOOKED,
    ProspectStatus.WON, ProspectStatus.LOST, ProspectStatus.SUPPRESSED,
}


def closed_reason(prospect: Prospect) -> str:
    """Human-readable reason shown on the Closed page."""
    if prospect.status in _CLOSED_STATUSES:
        return prospect.status.replace("_", " ").lower()
    if any(t.status == TouchStatus.BOUNCED for t in prospect.touches):
        return "bounced"
    unsub = any(r.classification == ReplyClass.UNSUBSCRIBE for r in prospect.replies)
    return "unsubscribed" if unsub else ""


def derive_stage(prospect: Prospect) -> str:
    touches = prospect.touches or []
    replies = prospect.replies or []

    if prospect.status in _CLOSED_STATUSES:
        return STAGE_CLOSED
    if any(t.status == TouchStatus.BOUNCED for t in touches):
        return STAGE_CLOSED

    if any(r.classification != ReplyClass.BOUNCE for r in replies):
        return STAGE_REPLIES

    if any(t.status == TouchStatus.SENT for t in touches):
        return STAGE_IN_SEQUENCE

    if any(t.status in (TouchStatus.DRAFT, TouchStatus.APPROVED) for t in touches):
        return STAGE_DRAFTS

    return STAGE_PROSPECTS


def _all_with_relations(session: Session) -> list[Prospect]:
    return session.execute(
        select(Prospect).options(selectinload(Prospect.touches),
                                 selectinload(Prospect.replies))
    ).scalars().all()


def stage_counts(session: Session) -> dict[str, int]:
    counts = {stage: 0 for stage in STAGES}
    for prospect in _all_with_relations(session):
        counts[derive_stage(prospect)] += 1
    return counts


def prospects_for_stage(session: Session, stage: str) -> list[Prospect]:
    rows = [p for p in _all_with_relations(session) if derive_stage(p) == stage]
    if stage == STAGE_REPLIES:
        # money page: newest inbound reply first
        rows.sort(key=lambda p: max((r.received_at for r in p.replies),
                                    default=p.created_at), reverse=True)
    elif stage == STAGE_IN_SEQUENCE:
        rows.sort(key=lambda p: next_touch_at(p) or p.created_at)
    else:
        rows.sort(key=lambda p: p.created_at, reverse=True)
    return rows


def sequence_position(prospect: Prospect) -> tuple[int, int]:
    """(touches sent, total planned) for the In Sequence page."""
    sent = sum(1 for t in prospect.touches if t.status == TouchStatus.SENT)
    live = sum(1 for t in prospect.touches
               if t.status in (TouchStatus.SENT, TouchStatus.QUEUED, TouchStatus.PAUSED))
    return sent, max(live, sent)


def next_touch_at(prospect: Prospect):
    """When the next queued touch fires, or None."""
    pending = [t.scheduled_at for t in prospect.touches
               if t.status == TouchStatus.QUEUED and t.scheduled_at]
    return min(pending) if pending else None


def search_all_stages(session: Session, q: str) -> list[tuple[Prospect, str]]:
    """Global search across every stage; returns (prospect, stage) pairs."""
    needle = q.strip().lower()
    if not needle:
        return []
    out = []
    for prospect in _all_with_relations(session):
        haystack = " ".join(filter(None, [
            prospect.name, prospect.email, prospect.city, prospect.trade,
            prospect.owner_name, prospect.phone,
        ])).lower()
        if needle in haystack:
            out.append((prospect, derive_stage(prospect)))
    return out
