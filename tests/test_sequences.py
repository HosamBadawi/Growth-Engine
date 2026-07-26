"""Follow-up sequences: day 3/6/10 creation and instant cancellation on reply."""
from datetime import timedelta

from db.models import (ProspectStatus, Touch, TouchStatus, TouchType)
from engine.sender import cancel_sequence, create_followups, queue_approved
from engine.util import utcnow


def _email1(session, prospect, status=TouchStatus.SENT):
    touch = Touch(prospect_id=prospect.id, type=TouchType.EMAIL_1,
                  subject="quick question", body="hello", status=status,
                  sent_at=utcnow())
    session.add(touch)
    session.commit()
    return touch


def test_followups_created_at_day_3_6_10(session, prospect):
    email1 = _email1(session, prospect)
    created = create_followups(session, prospect, email1.sent_at)
    assert len(created) == 3
    by_type = {t.type: t for t in created}
    assert set(by_type) == {TouchType.FOLLOWUP_2, TouchType.FOLLOWUP_3, TouchType.BREAKUP}
    for touch_type, offset in TouchType.FOLLOWUP_OFFSETS_DAYS.items():
        expected = email1.sent_at + timedelta(days=offset)
        assert by_type[touch_type].scheduled_at == expected
        assert by_type[touch_type].status == TouchStatus.QUEUED
        assert by_type[touch_type].body  # rendered from template, not empty
        assert by_type[touch_type].subject


def test_reply_cancels_pending_sequence(session, prospect):
    email1 = _email1(session, prospect)
    create_followups(session, prospect, email1.sent_at)

    cancelled = cancel_sequence(session, prospect.id, "reply received")
    assert cancelled == 3
    statuses = [t.status for t in session.query(Touch)
                .filter(Touch.prospect_id == prospect.id,
                        Touch.type != TouchType.EMAIL_1).all()]
    assert statuses == [TouchStatus.CANCELLED] * 3
    # the already-sent email is untouched
    assert email1.status == TouchStatus.SENT


def test_cancel_does_not_touch_other_prospects(session, prospect):
    from db.models import Prospect
    other = Prospect(name="Other Co", email="o@other.example",
                     status=ProspectStatus.VERIFIED)
    session.add(other)
    session.commit()
    email1 = _email1(session, prospect)
    create_followups(session, prospect, email1.sent_at)
    other_touch = Touch(prospect_id=other.id, type=TouchType.FOLLOWUP_2,
                        subject="s", body="b", status=TouchStatus.QUEUED,
                        scheduled_at=utcnow())
    session.add(other_touch)
    session.commit()

    cancel_sequence(session, prospect.id, "reply")
    session.refresh(other_touch)
    assert other_touch.status == TouchStatus.QUEUED


async def test_dry_run_followups_are_tagged_and_never_go_live(session, prospect, tmp_path, monkeypatch):
    """DRY_RUN follow-ups must carry the dry marker so LIVE can cancel them."""
    import engine.sender as sender_mod
    monkeypatch.setattr(sender_mod, "OUTBOX_DIR", tmp_path)

    touch = Touch(prospect_id=prospect.id, type=TouchType.EMAIL_1,
                  subject="s", body="b", status=TouchStatus.QUEUED,
                  scheduled_at=utcnow())
    session.add(touch)
    session.commit()

    ok, _ = await sender_mod.send_touch(session, touch)
    assert ok
    assert (touch.meta_json or {}).get("dry")
    followups = session.query(Touch).filter(
        Touch.prospect_id == prospect.id, Touch.type != TouchType.EMAIL_1).all()
    assert len(followups) == 3
    for f in followups:
        assert (f.meta_json or {}).get("dry"), f"{f.type} missing dry marker"


async def test_approved_reply_is_queued_not_sent_inline(session, prospect):
    """The reply approve button must go through sender rails, never send directly."""
    from db.models import Reply
    from engine.replies import send_suggested_reply

    reply = Reply(prospect_id=prospect.id, classification="INTERESTED",
                  raw_text="sounds good")
    session.add(reply)
    session.commit()

    ok, info = await send_suggested_reply(session, reply, "Great, here is my calendar.")
    assert ok and info == "queued"
    out = session.query(Touch).filter(Touch.type == "REPLY_OUT").one()
    assert out.status == TouchStatus.QUEUED  # sender_tick delivers it, gates apply
    assert reply.handled


def test_queue_approved_moves_drafts(session, prospect):
    touch = Touch(prospect_id=prospect.id, type=TouchType.EMAIL_1,
                  subject="s", body="b", status=TouchStatus.APPROVED)
    session.add(touch)
    session.commit()
    count = queue_approved(session)
    assert count == 1
    session.refresh(touch)
    assert touch.status == TouchStatus.QUEUED
    assert touch.scheduled_at is not None
    assert touch.prospect.status == ProspectStatus.QUEUED
