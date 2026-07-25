"""Module 9 — Dashboard: FastAPI + Jinja2 + Tailwind (CDN) + Chart.js (CDN)."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes.actions import router as actions_router
from app.routes.admin import router as admin_router
from app.routes.dashboard import router as dashboard_router
from app.routes.pipeline_ui import router as pipeline_router
from engine import __version__

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from db.migrate import run_migrations

    run_migrations()
    yield


app = FastAPI(title="Growth Engine", lifespan=lifespan)
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(dashboard_router)
app.include_router(admin_router)
app.include_router(pipeline_router)
app.include_router(actions_router)


@app.get("/healthz")
async def healthz() -> dict:
    """Unauthenticated health JSON (dashboard binds localhost by default)."""
    from db.session import new_session
    from engine.config import get_settings
    from engine.connections import resolve_email, resolve_telegram
    from engine.llm import ROLES, resolve_role
    from engine.state import get_state, is_paused

    settings = get_settings()
    health: dict = {"version": __version__, "mode": settings.engine_mode.upper()}

    session = new_session()
    try:
        health["db"] = "ok"
        health["paused"] = is_paused(session)
        health["last_report_at"] = get_state(session, "last_report_at")
        roles = {}
        for role in ROLES:
            provider, model = await resolve_role(role, session)
            roles[role] = f"{model} via {provider.label}"
        health["active_llm_roles"] = roles
        email_cfg = resolve_email(session)
        health["smtp"] = "configured" if email_cfg.smtp_host else "not configured"
        health["imap"] = "configured" if email_cfg.imap_host else "not configured"
        health["bot"] = ("configured" if resolve_telegram(session).token
                         else "not configured")
    except Exception as exc:  # noqa: BLE001 — health must report, not raise
        health["db"] = f"error: {str(exc)[:200]}"
    finally:
        session.close()

    try:
        from engine.llm import _local_ollama

        await asyncio.wait_for(_local_ollama().list_models(), timeout=3)
        health["ollama"] = "up"
    except Exception:  # noqa: BLE001
        health["ollama"] = "down"
    return health
