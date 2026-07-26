"""Pipeline orchestration: /find runs Prospector -> Enricher -> Verifier."""
import asyncio
import logging
from typing import Awaitable, Callable

from sqlalchemy import select

from db.models import Prospect, ProspectStatus, Touch, TouchStatus, TouchType
from db.session import new_session
from engine.enricher import enrich_prospect
from engine.events import log_event
from engine.prospector import run_prospecting
from engine.verifier import verify_prospect
from engine.writer import generate_draft

log = logging.getLogger("pipeline")

ProgressCb = Callable[[str], Awaitable[None]]


async def _report(cb: ProgressCb | None, text: str) -> None:
    log.info(text)
    if cb:
        try:
            await cb(text)
        except Exception:  # noqa: BLE001 — progress updates are best-effort
            pass


async def run_find(trade: str, city: str, limit: int,
                   progress: ProgressCb | None = None,
                   provider_name: str | None = None) -> dict:
    """Full find pipeline. Returns summary counts."""
    session = new_session()
    try:
        query = trade if trade.startswith("csv:") else None
        await _report(progress, f"Prospecting '{trade} {city}' (target {limit})...")

        # Bridge the provider's sync progress calls onto the async callback.
        loop = asyncio.get_running_loop()

        def sync_progress(text: str) -> None:
            asyncio.run_coroutine_threadsafe(_report(progress, text), loop)

        summary = await asyncio.to_thread(
            run_prospecting, session, trade, city, limit, query,
            provider_name, sync_progress,
        )
        await _report(
            progress,
            f"Prospector: found {summary['found']}, kept {summary['kept']}, "
            f"duplicates {summary['duplicates']}, "
            f"skipped known {summary.get('skipped_known', 0)}, "
            f"filtered {sum(summary['skipped'].values())}",
        )
        if summary.get("exhausted") and summary["kept"] < limit:
            known = summary.get("skipped_known", 0)
            await _report(
                progress,
                f"{city.title()} {trade} exhausted: {known} of {known + summary['kept']} "
                f"already known. Try another city or trade.",
            )

        new_prospects = session.execute(
            select(Prospect).where(Prospect.status == ProspectStatus.NEW)
        ).scalars().all()
        await _report(progress, f"Enriching {len(new_prospects)} websites...")
        enriched = 0
        for prospect in new_prospects:
            try:
                await asyncio.to_thread(enrich_prospect, session, prospect)
                enriched += 1
            except Exception as exc:  # noqa: BLE001 — one bad site must not stop the batch
                log.warning("enrich failed for %s: %s", prospect.name, exc)
                session.rollback()

        to_verify = session.execute(
            select(Prospect).where(Prospect.status == ProspectStatus.ENRICHED)
        ).scalars().all()
        await _report(progress, f"Verifying {len(to_verify)} prospects...")
        verified = form_only = failed = 0
        for prospect in to_verify:
            try:
                await asyncio.to_thread(verify_prospect, session, prospect)
                if prospect.status == ProspectStatus.VERIFIED:
                    verified += 1
                elif prospect.status == ProspectStatus.FORM_ONLY:
                    form_only += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("verify failed for %s: %s", prospect.name, exc)
                session.rollback()
                failed += 1

        result = {
            **summary,
            "enriched": enriched,
            "verified": verified,
            "form_only": form_only,
            "unresolved": failed,
        }
        log_event(session, "pipeline", f"/find complete: {result}", meta=result)
        return result
    finally:
        session.close()


async def generate_all_drafts(progress: ProgressCb | None = None) -> list[int]:
    """Write drafts for every VERIFIED prospect without a live EMAIL_1 touch.
    Returns the new touch ids (fetch fresh in the caller's session)."""
    session = new_session()
    try:
        candidates = session.execute(
            select(Prospect).where(Prospect.status == ProspectStatus.VERIFIED)
        ).scalars().all()
        existing = {
            t.prospect_id
            for t in session.execute(
                select(Touch).where(
                    Touch.type == TouchType.EMAIL_1,
                    Touch.status.notin_([TouchStatus.SKIPPED, TouchStatus.CANCELLED]),
                )
            ).scalars().all()
        }
        todo = [p for p in candidates if p.id not in existing]
        await _report(progress, f"Drafting for {len(todo)} verified prospects...")
        touch_ids = []
        for prospect in todo:
            try:
                touch = await generate_draft(session, prospect)
                touch_ids.append(touch.id)
                await _report(progress, f"Drafted: {prospect.name}")
            except Exception as exc:  # noqa: BLE001
                log.warning("draft failed for %s: %s", prospect.name, exc)
                session.rollback()
        return touch_ids
    finally:
        session.close()
