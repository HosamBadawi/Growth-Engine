"""Telegram command center (aiogram 3). Only TELEGRAM_USER_ID may command it."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select

from db.models import (Prospect, Reply, Touch, TouchStatus)
from db.session import new_session
from engine.config import get_settings
from engine.events import log_event
from engine.reporter import build_report_text
from engine.sender import cancel_sequence, queue_approved
from engine.state import KEY_LIVE_CONFIRMED, set_paused, set_state

log = logging.getLogger("bot")

router = Router()


class OwnerOnly(BaseFilter):
    async def __call__(self, event) -> bool:
        from engine.connections import resolve_telegram

        uid = resolve_telegram().user_id
        return bool(uid) and getattr(event.from_user, "id", None) == uid


router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


class EditFlow(StatesGroup):
    draft = State()   # waiting for corrected draft text; data: touch_id
    reply = State()   # waiting for corrected reply text; data: reply_id


def draft_keyboard(touch_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"appr:{touch_id}"),
        InlineKeyboardButton(text="✏ Edit", callback_data=f"edit:{touch_id}"),
        InlineKeyboardButton(text="⏭ Skip", callback_data=f"skip:{touch_id}"),
    ]])


def format_draft(touch: Touch) -> str:
    p = touch.prospect
    level = p.email_verification_level
    quality = (p.intel_json or {}).get("email_quality", "")
    return (f"Draft #{touch.id}: {p.name} ({(p.city or '?').title()}, {p.trade})\n"
            f"To: {p.email} [{level}{', role address' if quality == 'role' else ''}]\n"
            f"Subject: {touch.subject}\n"
            f"{'-' * 30}\n{touch.body}")


# ── commands ────────────────────────────────────────────────────────────────

@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    settings = get_settings()
    await message.answer(
        f"Growth Engine [{settings.engine_mode.upper()}]\n\n"
        "/find <niche> <city> <n>  find + enrich + verify\n"
        "/draft  write drafts for all verified prospects\n"
        "/send  queue approved drafts (caps + window apply)\n"
        "/status  funnel summary\n"
        "/models  LLM role assignments + API usage\n"
        "/report  full daily report now\n"
        "/pause all | /pause <email>\n"
        "/resume all\n"
        "/golive  one-time confirmation required for LIVE mode"
    )


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 3:
        await message.answer("Usage: /find <niche> <city...> <count>\n"
                             "Example: /find hvac phoenix az 20")
        return
    niche, count_str = args[0], args[-1]
    city = " ".join(args[1:-1])
    try:
        limit = max(1, min(int(count_str), 100))
    except ValueError:
        await message.answer(f"Last argument must be a number, got '{count_str}'")
        return

    from engine.pipeline import run_find

    async def progress(text: str) -> None:
        await message.answer(text)

    await message.answer(f"Running pipeline for '{niche} {city}' (target {limit})...")

    async def job() -> None:
        try:
            result = await run_find(niche, city, limit, progress)
            await message.answer(
                f"Done. Kept {result['kept']} new prospects, enriched {result['enriched']}, "
                f"verified {result['verified']}, form only {result['form_only']}, "
                f"unresolved {result['unresolved']}.\nNext: /draft"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("/find failed")
            await message.answer(f"Pipeline failed: {exc}")

    asyncio.create_task(job())


@router.message(Command("draft"))
async def cmd_draft(message: Message) -> None:
    from engine.pipeline import generate_all_drafts

    await message.answer("Generating drafts (local LLM, this can take a while)...")

    async def job() -> None:
        try:
            touch_ids = await generate_all_drafts()
            if not touch_ids:
                await message.answer("No verified prospects without a draft. Run /find first.")
                return
            session = new_session()
            try:
                for touch_id in touch_ids:
                    touch = session.get(Touch, touch_id)
                    await message.answer(format_draft(touch)[:4000],
                                         reply_markup=draft_keyboard(touch.id))
            finally:
                session.close()
            await message.answer(f"{len(touch_ids)} drafts ready for review.")
        except Exception as exc:  # noqa: BLE001
            log.exception("/draft failed")
            await message.answer(f"Draft generation failed: {exc}")

    asyncio.create_task(job())


@router.message(Command("send"))
async def cmd_send(message: Message) -> None:
    settings = get_settings()
    session = new_session()
    try:
        count = queue_approved(session)
    finally:
        session.close()
    mode = settings.engine_mode.upper()
    note = {"DRY_RUN": "DRY_RUN: they will be written to /outbox as .eml files.",
            "SANDBOX": f"SANDBOX: everything goes to {settings.sandbox_recipient}.",
            "LIVE": "LIVE MODE: real emails to real prospects."}.get(mode, "")
    await message.answer(f"Queued {count} approved drafts. {note}\n"
                         f"Send window {settings.send_window_start}-{settings.send_window_end} "
                         f"US Eastern, Mon-Fri, warm-up caps apply.")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    session = new_session()
    try:
        await message.answer(build_report_text(session))
    finally:
        session.close()


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    await cmd_status(message)


@router.message(Command("models"))
async def cmd_models(message: Message) -> None:
    from engine.llm import ROLES, resolve_role, usage_today_summary

    session = new_session()
    try:
        lines = ["LLM role assignments:"]
        for role in ROLES:
            provider, model = await resolve_role(role, session)
            lines.append(f"  {role}: {model} via {provider.label} "
                         f"({provider.provider_type})")
        usage = usage_today_summary(session)
        cap = get_settings().api_daily_call_cap
        if usage:
            lines.append("\nCalls today:")
            for label, u in usage.items():
                cap_note = "" if u["provider_type"] == "ollama" else f" / cap {cap}"
                lines.append(f"  {label}: {u['calls']}{cap_note} "
                             f"({u['tokens_in']} in / {u['tokens_out']} out tokens)")
        else:
            lines.append("\nNo LLM calls today.")
        await message.answer("\n".join(lines))
    finally:
        session.close()


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()
    session = new_session()
    try:
        if arg in ("all", ""):
            set_paused(session, True)
            log_event(session, "bot", "Sending PAUSED via Telegram")
            await message.answer("All sending paused. /resume all to continue.")
            return
        prospect = session.execute(
            select(Prospect).where(Prospect.email == arg)
        ).scalar_one_or_none()
        if not prospect:
            await message.answer(f"No prospect with email {arg}")
            return
        touches = session.execute(
            select(Touch).where(Touch.prospect_id == prospect.id,
                                Touch.status == TouchStatus.QUEUED)
        ).scalars().all()
        for touch in touches:
            touch.status = TouchStatus.PAUSED
        session.commit()
        log_event(session, "bot", f"Paused {len(touches)} touches for {arg}")
        await message.answer(f"Paused {len(touches)} queued touches for {prospect.name}.")
    finally:
        session.close()


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    session = new_session()
    try:
        set_paused(session, False)
        touches = session.execute(
            select(Touch).where(Touch.status == TouchStatus.PAUSED)
        ).scalars().all()
        for touch in touches:
            touch.status = TouchStatus.QUEUED
        session.commit()
        log_event(session, "bot", f"Sending RESUMED, re-queued {len(touches)} paused touches")
        await message.answer(f"Resumed. Re-queued {len(touches)} paused touches.")
    finally:
        session.close()


@router.message(Command("golive"))
async def cmd_golive(message: Message) -> None:
    settings = get_settings()
    if settings.engine_mode.upper() != "LIVE":
        await message.answer(
            f"ENGINE_MODE is {settings.engine_mode.upper()}. Set ENGINE_MODE=LIVE in .env, "
            "restart, then run /golive again to confirm."
        )
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="YES, go live", callback_data="live:yes"),
        InlineKeyboardButton(text="Cancel", callback_data="live:no"),
    ]])
    await message.answer(
        "You are about to enable REAL sending to REAL prospects.\n"
        "Checklist: SPF/DKIM/DMARC set, SANDBOX ran clean for 3 days, warm-up understood.\n"
        "Confirm?", reply_markup=markup)


# ── draft approval callbacks ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("appr:"))
async def cb_approve(callback: CallbackQuery) -> None:
    touch_id = int(callback.data.split(":")[1])
    session = new_session()
    try:
        touch = session.get(Touch, touch_id)
        if not touch or touch.status != TouchStatus.DRAFT:
            await callback.answer("Not a pending draft anymore")
            return
        touch.status = TouchStatus.APPROVED
        session.commit()
        log_event(session, "bot", f"Draft #{touch_id} approved ({touch.prospect.name})")
        await callback.answer("Approved")
        await callback.message.edit_text(
            callback.message.text + "\n\n>> APPROVED (queue with /send)")
    finally:
        session.close()


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery) -> None:
    touch_id = int(callback.data.split(":")[1])
    session = new_session()
    try:
        touch = session.get(Touch, touch_id)
        if touch:
            touch.status = TouchStatus.SKIPPED
            session.commit()
            log_event(session, "bot", f"Draft #{touch_id} skipped")
        await callback.answer("Skipped")
        await callback.message.edit_text(callback.message.text + "\n\n>> SKIPPED")
    finally:
        session.close()


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    touch_id = int(callback.data.split(":")[1])
    await state.set_state(EditFlow.draft)
    await state.update_data(touch_id=touch_id)
    await callback.answer()
    await callback.message.answer(
        f"Send me the corrected text for draft #{touch_id}.\n"
        "First line starting with 'Subject:' replaces the subject; "
        "the rest becomes the body. It will be auto approved.")


@router.message(EditFlow.draft)
async def on_edit_draft_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    touch_id = data.get("touch_id")
    text = (message.text or "").strip()
    if not text:
        await message.answer("Empty text, edit cancelled.")
        return
    session = new_session()
    try:
        touch = session.get(Touch, touch_id)
        if not touch:
            await message.answer("Draft vanished, sorry.")
            return
        lines = text.splitlines()
        if lines and lines[0].lower().startswith("subject:"):
            touch.subject = lines[0].split(":", 1)[1].strip()
            text = "\n".join(lines[1:]).strip()
        touch.body = text
        touch.status = TouchStatus.APPROVED
        touch.meta_json = {**(touch.meta_json or {}), "edited": True}
        session.commit()
        log_event(session, "bot", f"Draft #{touch_id} edited + approved")
        await message.answer(f"Draft #{touch_id} updated and approved. Queue with /send.")
    finally:
        session.close()


# ── live confirmation callbacks ─────────────────────────────────────────────

@router.callback_query(F.data == "live:yes")
async def cb_live_yes(callback: CallbackQuery) -> None:
    session = new_session()
    try:
        set_state(session, KEY_LIVE_CONFIRMED, "1")
        log_event(session, "bot", "LIVE mode confirmed via Telegram", level="WARNING")
    finally:
        session.close()
    await callback.answer("LIVE confirmed")
    await callback.message.edit_text("LIVE mode confirmed. The engine can now send "
                                     "real email within caps and window.")


@router.callback_query(F.data == "live:no")
async def cb_live_no(callback: CallbackQuery) -> None:
    await callback.answer("Cancelled")
    await callback.message.edit_text("Go-live cancelled. Still safe.")


# ── reply suggestion callbacks ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("rok:"))
async def cb_reply_ok(callback: CallbackQuery) -> None:
    reply_id = int(callback.data.split(":")[1])
    session = new_session()
    try:
        reply = session.get(Reply, reply_id)
        if not reply or not reply.suggested_reply:
            await callback.answer("No suggested reply stored")
            return
        from engine.replies import send_suggested_reply

        ok, err = await send_suggested_reply(session, reply, reply.suggested_reply)
        await callback.answer("Queued" if ok else f"Failed: {err[:150]}")
        await callback.message.edit_text(
            callback.message.text
            + f"\n\n>> REPLY {'QUEUED (window/caps apply)' if ok else 'FAILED: ' + err[:150]}")
    finally:
        session.close()


@router.callback_query(F.data.startswith("redit:"))
async def cb_reply_edit(callback: CallbackQuery, state: FSMContext) -> None:
    reply_id = int(callback.data.split(":")[1])
    await state.set_state(EditFlow.reply)
    await state.update_data(reply_id=reply_id)
    await callback.answer()
    await callback.message.answer("Send me the reply text to send instead.")


@router.message(EditFlow.reply)
async def on_edit_reply_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    reply_id = data.get("reply_id")
    text = (message.text or "").strip()
    if not text:
        await message.answer("Empty text, cancelled.")
        return
    session = new_session()
    try:
        reply = session.get(Reply, reply_id)
        if not reply:
            await message.answer("Reply record vanished, sorry.")
            return
        from engine.replies import send_suggested_reply

        ok, err = await send_suggested_reply(session, reply, text)
        await message.answer("Reply queued (window and caps apply)." if ok
                             else f"Failed: {err[:200]}")
    finally:
        session.close()


@router.callback_query(F.data.startswith("rignore:"))
async def cb_reply_ignore(callback: CallbackQuery) -> None:
    reply_id = int(callback.data.split(":")[1])
    session = new_session()
    try:
        reply = session.get(Reply, reply_id)
        if reply:
            reply.handled = True
            session.commit()
    finally:
        session.close()
    await callback.answer("Ignored")


async def run_bot() -> None:
    from engine.connections import resolve_telegram

    cfg = resolve_telegram()
    if not cfg.token:
        log.warning("No Telegram bot configured (admin page or TELEGRAM_BOT_TOKEN), "
                    "bot disabled (dashboard still works)")
        return
    if not cfg.user_id:
        log.warning("No Telegram user id configured, bot will ignore everyone")
    log.info("Telegram bot polling started (%s)", cfg.source)
    bot = Bot(cfg.token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot, handle_signals=False)
