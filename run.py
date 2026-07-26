"""Growth Engine — single entry point.

Starts: dashboard (localhost:8080) + APScheduler (sender/replies/report) +
Telegram bot (if token configured). DRY_RUN is the default mode.
"""
import asyncio
import logging

import uvicorn

from db.session import new_session
from engine.config import ENGINE_MODES, get_settings
from engine.logging_setup import setup_logging
from engine.state import KEY_BENCHMARKED, get_state, set_state

log = logging.getLogger("run")


class DashboardServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # main loop owns Ctrl+C
        pass


async def startup_checks() -> None:
    settings = get_settings()
    mode = settings.engine_mode.upper()
    if mode not in ENGINE_MODES:
        raise SystemExit(f"ENGINE_MODE must be one of {ENGINE_MODES}, got '{mode}'")

    print("=" * 62)
    from engine import __version__

    print(f"  Growth Engine v{__version__}")
    print(f"  Mode: {mode}" + ("  (nothing will be sent, .eml files go to /outbox)"
                               if mode == "DRY_RUN" else ""))
    if mode == "LIVE":
        print("  LIVE MODE: real sending. Requires one-time /golive in Telegram.")
    print(f"  Dashboard: http://{settings.dashboard_host}:{settings.dashboard_port}")
    print("=" * 62)

    from engine.ollama_client import OllamaError, benchmark, startup_check

    try:
        problems = await startup_check()
    except OllamaError as exc:
        problems = [str(exc)]
    for problem in problems:
        print(f"  WARNING: {problem}")

    session = new_session()
    try:
        if not problems and not get_state(session, KEY_BENCHMARKED):
            print("  First run: measuring tokens/sec (one-time, may take a minute)...")
            for label, model in (("writer", settings.writer_model),
                                 ("classifier", settings.classifier_model)):
                try:
                    tps = await benchmark(model)
                    print(f"    {label} model {model}: {tps} tokens/sec")
                except Exception as exc:  # noqa: BLE001
                    print(f"    {label} model {model}: benchmark failed ({exc})")
            set_state(session, KEY_BENCHMARKED, "1")
    finally:
        session.close()

    from engine.llm import ROLES, resolve_role

    session = new_session()
    try:
        for role in ROLES:
            try:
                provider, model = await resolve_role(role, session)
                if provider.provider_type != "ollama":
                    ok, detail = await provider.test()
                    if not ok:
                        print(f"  WARNING: LLM role '{role}' -> provider "
                              f"'{provider.label}' not usable ({detail}); "
                              f"calls will fall back to local Ollama")
            except Exception as exc:  # noqa: BLE001 — startup must warn, never crash
                print(f"  WARNING: LLM role '{role}' check failed: {exc}")
    finally:
        session.close()

    if not settings.telegram_bot_token:
        print("  NOTE: TELEGRAM_BOT_TOKEN empty, bot disabled (dashboard + CLI still work)")
    if not settings.smtp_host and settings.engine_mode.upper() != "DRY_RUN":
        print("  WARNING: SMTP not configured, real sending will fail")
    if not settings.imap_host:
        print("  NOTE: IMAP not configured, reply watcher idle")


async def main() -> None:
    from db.migrate import run_migrations

    settings = get_settings()
    setup_logging(settings.log_level)
    run_migrations()
    await startup_checks()

    from bot.telegram_bot import run_bot
    from engine.scheduler import build_scheduler

    scheduler = build_scheduler()
    scheduler.start()

    server = DashboardServer(uvicorn.Config(
        "app.main:app", host=settings.dashboard_host, port=settings.dashboard_port,
        log_level="warning",
    ))

    try:
        await asyncio.gather(server.serve(), run_bot())
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
