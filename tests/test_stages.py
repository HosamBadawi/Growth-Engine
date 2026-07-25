"""Stage derivation: exactly one page per prospect, for every state."""
from db.models import (Prospect, ProspectStatus, Reply, ReplyClass, Touch,
                       TouchStatus, TouchType)
from engine.stages import (STAGE_CLOSED, STAGE_DRAFTS, STAGE_IN_SEQUENCE,
                           STAGE_PROSPECTS, STAGE_REPLIES, closed_reason,
                           derive_stage, next_touch_at, prospects_for_stage,
                           search_all_stages, sequence_position, stage_counts)
from engine.util import utcnow


def _prospect(session, status=ProspectStatus.NEW, **kw):
    p = Prospect(name=kw.pop("name", "Test Co"), status=status,
                 city="tampa", trade="hvac", **kw)
    session.add(p)
    session.commit()
    return p


def _touch(session, p, status, type_=TouchType.EMAIL_1, **kw):
    t = Touch(prospect_id=p.id, type=type_, subject="s", body="b",
              status=status, **kw)
    session.add(t)
    session.commit()
    session.refresh(p)
    return t


def test_never_contacted_is_prospects(session):
    for status in (ProspectStatus.NEW, ProspectStatus.ENRICHED,
                   ProspectStatus.VERIFIED, ProspectStatus.FORM_ONLY):
        p = _prospect(session, status, name=f"P-{status}")
        assert derive_stage(p) == STAGE_PROSPECTS, status


def test_draft_awaiting_action_is_drafts(session):
    p = _prospect(session, ProspectStatus.DRAFTED)
    _touch(session, p, TouchStatus.DRAFT)
    assert derive_stage(p) == STAGE_DRAFTS
    p2 = _prospect(session, ProspectStatus.DRAFTED, name="Approved Co")
    _touch(session, p2, TouchStatus.APPROVED)
    assert derive_stage(p2) == STAGE_DRAFTS


def test_sent_no_reply_is_in_sequence(session):
    p = _prospect(session, ProspectStatus.CONTACTED)
    _touch(session, p, TouchStatus.SENT, sent_at=utcnow())
    _touch(session, p, TouchStatus.QUEUED, type_=TouchType.FOLLOWUP_2,
           scheduled_at=utcnow())
    assert derive_stage(p) == STAGE_IN_SEQUENCE
    sent, total = sequence_position(p)
    assert sent == 1 and total == 2
    assert next_touch_at(p) is not None


def test_inbound_reply_is_replies(session):
    p = _prospect(session, ProspectStatus.REPLIED)
    _touch(session, p, TouchStatus.SENT, sent_at=utcnow())
    session.add(Reply(prospect_id=p.id, classification=ReplyClass.QUESTION,
                      raw_text="tell me more"))
    session.commit()
    session.refresh(p)
    assert derive_stage(p) == STAGE_REPLIES


def test_closed_statuses_and_reasons(session):
    for status in (ProspectStatus.INTERESTED, ProspectStatus.CALL_BOOKED,
                   ProspectStatus.WON, ProspectStatus.LOST,
                   ProspectStatus.SUPPRESSED):
        p = _prospect(session, status, name=f"C-{status}")
        assert derive_stage(p) == STAGE_CLOSED, status
        assert closed_reason(p)


def test_bounced_touch_is_closed_even_without_status(session):
    p = _prospect(session, ProspectStatus.FORM_ONLY)
    _touch(session, p, TouchStatus.BOUNCED, sent_at=utcnow())
    assert derive_stage(p) == STAGE_CLOSED
    assert closed_reason(p) == "bounced"


def test_bounce_reply_alone_does_not_put_on_replies_page(session):
    p = _prospect(session, ProspectStatus.CONTACTED)
    _touch(session, p, TouchStatus.SENT, sent_at=utcnow())
    session.add(Reply(prospect_id=p.id, classification=ReplyClass.BOUNCE,
                      raw_text="undeliverable"))
    session.commit()
    session.refresh(p)
    # bounce reply is not an inbound conversation; sequence view still applies
    assert derive_stage(p) == STAGE_IN_SEQUENCE


def test_every_prospect_on_exactly_one_page(session):
    _prospect(session, ProspectStatus.NEW, name="A")
    drafted = _prospect(session, ProspectStatus.DRAFTED, name="B")
    _touch(session, drafted, TouchStatus.DRAFT)
    closed = _prospect(session, ProspectStatus.WON, name="C")
    counts = stage_counts(session)
    assert sum(counts.values()) == 3
    assert counts[STAGE_PROSPECTS] == 1
    assert counts[STAGE_DRAFTS] == 1
    assert counts[STAGE_CLOSED] == 1


def test_replies_sorted_newest_first(session):
    from datetime import timedelta

    now = utcnow()
    old = _prospect(session, ProspectStatus.REPLIED, name="Old Reply")
    session.add(Reply(prospect_id=old.id, classification=ReplyClass.QUESTION,
                      raw_text="x", received_at=now - timedelta(hours=2)))
    new = _prospect(session, ProspectStatus.REPLIED, name="New Reply")
    session.add(Reply(prospect_id=new.id, classification=ReplyClass.QUESTION,
                      raw_text="y", received_at=now))
    session.commit()
    rows = prospects_for_stage(session, STAGE_REPLIES)
    assert [p.name for p in rows][0] == "New Reply"


def test_global_search_spans_stages(session):
    _prospect(session, ProspectStatus.NEW, name="Findme Plumbing")
    won = _prospect(session, ProspectStatus.WON, name="Findme Electric")
    hits = search_all_stages(session, "findme")
    assert len(hits) == 2
    stages = {stage for _, stage in hits}
    assert stages == {STAGE_PROSPECTS, STAGE_CLOSED}
