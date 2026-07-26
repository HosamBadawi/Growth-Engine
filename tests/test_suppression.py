"""Suppression: STOP means forever, checked before EVERY send."""
import pytest

from db.models import (ProspectStatus, Suppression, Touch, TouchStatus, TouchType)
from engine.replies import handle_unsubscribe, quick_classify
from engine.sender import add_suppression, is_suppressed, send_touch, skip_reason
from engine.util import utcnow


def test_add_and_check_suppression(session):
    add_suppression(session, "Owner@Example.COM", "STOP reply")
    assert is_suppressed(session, "owner@example.com")
    assert is_suppressed(session, "OWNER@EXAMPLE.COM")
    assert not is_suppressed(session, "someone-else@example.com")


def test_suppression_is_idempotent(session):
    add_suppression(session, "x@y.example", "STOP")
    add_suppression(session, "x@y.example", "STOP again")
    assert session.query(Suppression).count() == 1


def test_skip_reason_blocks_suppressed(session, prospect):
    assert skip_reason(session, prospect) is None
    add_suppression(session, prospect.email, "STOP reply")
    assert skip_reason(session, prospect) == "suppressed"


async def test_send_refuses_suppressed_even_in_dry_run(session, prospect):
    add_suppression(session, prospect.email, "STOP reply")
    touch = Touch(prospect_id=prospect.id, type=TouchType.EMAIL_1,
                  subject="s", body="b", status=TouchStatus.QUEUED,
                  scheduled_at=utcnow())
    session.add(touch)
    session.commit()
    ok, reason = await send_touch(session, touch)
    assert not ok
    assert reason == "suppressed"
    session.refresh(touch)
    assert touch.status == TouchStatus.CANCELLED


def test_stop_reply_classified_without_llm():
    assert quick_classify("a@b.c", "re: hi", "STOP") == "UNSUBSCRIBE"
    assert quick_classify("a@b.c", "re: hi", "please unsubscribe me") == "UNSUBSCRIBE"
    assert quick_classify("a@b.c", "unsubscribe", "whatever") == "UNSUBSCRIBE"
    assert quick_classify("a@b.c", "re: hi", "sounds interesting, tell me more") is None


async def test_unsubscribe_flow_suppresses_forever(session, prospect):
    touch = Touch(prospect_id=prospect.id, type=TouchType.FOLLOWUP_2,
                  subject="s", body="b", status=TouchStatus.QUEUED,
                  scheduled_at=utcnow())
    session.add(touch)
    session.commit()

    await handle_unsubscribe(session, prospect)

    assert is_suppressed(session, prospect.email)
    assert prospect.status == ProspectStatus.SUPPRESSED
    session.refresh(touch)
    assert touch.status == TouchStatus.CANCELLED
    # and the sender refuses from now on
    assert skip_reason(session, prospect) is not None
