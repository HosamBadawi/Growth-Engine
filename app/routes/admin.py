"""Admin panel: connections, LLM providers/roles, campaign, sending rails,
templates, prospector, suppression, data tools. Session-auth on every route."""
import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select

from app.auth import require_auth
from app.routes.dashboard import templates
from db.models import Connection, ConnectionKind
from db.session import new_session
from engine.connections import (EmailConfig, activate, mask, resolve_email,
                                resolve_telegram, test_email_sync,
                                test_telegram)
from engine.events import log_event

router = APIRouter(prefix="/admin", dependencies=[Depends(require_auth)])


def _redirect(msg: str, path: str = "/admin") -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(msg[:300])}", status_code=303)


@router.get("")
async def admin_page(request: Request, msg: str = ""):
    session = new_session()
    try:
        connections = session.execute(
            select(Connection).order_by(Connection.kind, Connection.id)
        ).scalars().all()
        rows = []
        for conn in connections:
            cfg = conn.config_json or {}
            if conn.kind == ConnectionKind.TELEGRAM:
                detail = f"token {mask(cfg.get('token'))} · user id {cfg.get('user_id', '?')}"
            else:
                detail = (f"{cfg.get('from_email', '?')} · SMTP {cfg.get('smtp_host', '?')}"
                          f" · IMAP {cfg.get('imap_host') or 'none'}")
            rows.append({"c": conn, "detail": detail})
        telegram_cfg = resolve_telegram(session)
        email_cfg = resolve_email(session)
        return templates.TemplateResponse(request, "admin.html", {
            "rows": rows, "msg": msg, "active": "admin", "admin_tab": "connections",
            "telegram_source": telegram_cfg.source,
            "telegram_ok": bool(telegram_cfg.token),
            "email_source": email_cfg.source,
            "email_ok": bool(email_cfg.smtp_host),
        })
    finally:
        session.close()


@router.post("/telegram/add")
async def add_telegram(name: str = Form(...), token: str = Form(...),
                       user_id: str = Form("")):
    session = new_session()
    try:
        try:
            uid = int(user_id.strip() or 0)
        except ValueError:
            return _redirect("User id must be a number")
        conn = Connection(kind=ConnectionKind.TELEGRAM, name=name.strip(),
                          config_json={"token": token.strip(), "user_id": uid})
        session.add(conn)
        session.commit()
        log_event(session, "admin", f"Telegram connection '{conn.name}' saved")
        return _redirect(f"Telegram connection '{conn.name}' saved. "
                         "Activate it to use it.")
    finally:
        session.close()


@router.post("/email/add")
async def add_email(
    name: str = Form(...), from_name: str = Form(...), from_email: str = Form(...),
    smtp_host: str = Form(...), smtp_port: str = Form("587"),
    smtp_user: str = Form(...), smtp_password: str = Form(...),
    imap_host: str = Form(""), imap_user: str = Form(""),
    imap_password: str = Form(""), postal_address: str = Form(""),
):
    session = new_session()
    try:
        try:
            port = int(smtp_port.strip() or 587)
        except ValueError:
            return _redirect("SMTP port must be a number")
        conn = Connection(
            kind=ConnectionKind.EMAIL, name=name.strip(),
            config_json={
                "from_name": from_name.strip(), "from_email": from_email.strip().lower(),
                "smtp_host": smtp_host.strip(), "smtp_port": port,
                "smtp_user": smtp_user.strip(), "smtp_password": smtp_password,
                "imap_host": imap_host.strip(), "imap_user": imap_user.strip(),
                "imap_password": imap_password, "postal_address": postal_address.strip(),
            },
        )
        session.add(conn)
        session.commit()
        log_event(session, "admin", f"Email connection '{conn.name}' saved")
        return _redirect(f"Email connection '{conn.name}' saved. Test it, then activate.")
    finally:
        session.close()


@router.post("/connections/{connection_id}/activate")
async def activate_connection(connection_id: int):
    session = new_session()
    try:
        conn = activate(session, connection_id)
        if not conn:
            return _redirect("Connection not found")
        log_event(session, "admin", f"Connection '{conn.name}' ({conn.kind}) activated")
        note = (" Restart the engine so bot commands switch to the new bot "
                "(alerts switch immediately)." if conn.kind == ConnectionKind.TELEGRAM else
                " Applies immediately to all sending and reply watching.")
        return _redirect(f"'{conn.name}' is now the active {conn.kind} connection.{note}")
    finally:
        session.close()


@router.post("/connections/{connection_id}/deactivate")
async def deactivate_connection(connection_id: int):
    session = new_session()
    try:
        conn = session.get(Connection, connection_id)
        if conn:
            conn.is_active = False
            session.commit()
            log_event(session, "admin", f"Connection '{conn.name}' deactivated (env fallback)")
        return _redirect("Deactivated. The engine falls back to .env values.")
    finally:
        session.close()


@router.post("/connections/{connection_id}/delete")
async def delete_connection(connection_id: int):
    session = new_session()
    try:
        conn = session.get(Connection, connection_id)
        if conn:
            name = conn.name
            session.delete(conn)
            session.commit()
            log_event(session, "admin", f"Connection '{name}' deleted")
        return _redirect("Connection deleted.")
    finally:
        session.close()


@router.post("/connections/{connection_id}/test")
async def test_connection(connection_id: int):
    session = new_session()
    try:
        conn = session.get(Connection, connection_id)
        if not conn:
            return _redirect("Connection not found")
        cfg = conn.config_json or {}
        if conn.kind == ConnectionKind.TELEGRAM:
            ok, detail = await test_telegram(cfg.get("token", ""),
                                             int(cfg.get("user_id") or 0))
        else:
            email_cfg = EmailConfig(
                smtp_host=cfg.get("smtp_host", ""), smtp_port=int(cfg.get("smtp_port") or 587),
                smtp_user=cfg.get("smtp_user", ""), smtp_password=cfg.get("smtp_password", ""),
                imap_host=cfg.get("imap_host", ""),
                imap_user=cfg.get("imap_user") or cfg.get("smtp_user", ""),
                imap_password=cfg.get("imap_password") or cfg.get("smtp_password", ""),
                from_name=cfg.get("from_name", ""), from_email=cfg.get("from_email", ""),
                postal_address=cfg.get("postal_address", ""), source=f"connection:{conn.name}",
            )
            ok, detail = await asyncio.to_thread(test_email_sync, email_cfg)
        log_event(session, "admin",
                  f"Connection '{conn.name}' test: {'OK' if ok else 'FAILED'} ({detail})",
                  level="INFO" if ok else "WARNING")
        return _redirect(f"Test {'passed' if ok else 'FAILED'}: {detail}")
    finally:
        session.close()


# ───────────────────────────── Models & Providers ────────────────────────────

@router.get("/models")
async def models_page(request: Request, msg: str = ""):
    import json as _json

    from db.models import ModelRole
    from engine.config import get_settings
    from engine.llm import ROLES, resolve_role, usage_today_summary
    from engine.state import get_state

    session = new_session()
    try:
        llm_conns = session.execute(
            select(Connection).where(Connection.kind == ConnectionKind.LLM)
            .order_by(Connection.id)
        ).scalars().all()
        conn_rows = []
        for conn in llm_conns:
            cfg = conn.config_json or {}
            conn_rows.append({
                "c": conn,
                "detail": (f"{cfg.get('provider_type', '?')} · "
                           f"{cfg.get('base_url') or 'default url'} · "
                           f"key {mask(cfg.get('api_key')) or 'none'}"),
            })

        roles = {}
        for role in ROLES:
            try:
                provider, model = await resolve_role(role, session)
                roles[role] = {"provider": provider.label, "model": model,
                               "provider_type": provider.provider_type}
            except Exception as exc:  # noqa: BLE001
                roles[role] = {"provider": "error", "model": str(exc)[:80],
                               "provider_type": "?"}
        assignments = {r.role: r for r in session.execute(select(ModelRole)).scalars()}

        try:
            from engine.llm import _local_ollama

            installed = await asyncio.wait_for(_local_ollama().list_models(), timeout=5)
        except Exception:  # noqa: BLE001
            installed = []

        benchmarks = {}
        raw_bench = get_state(session, "model_benchmarks")
        if raw_bench:
            try:
                benchmarks = _json.loads(raw_bench)
            except ValueError:
                benchmarks = {}

        settings = get_settings()
        return templates.TemplateResponse(request, "admin_models.html", {
            "msg": msg, "active": "admin", "admin_tab": "models",
            "conn_rows": conn_rows, "roles": roles, "assignments": assignments,
            "installed": installed, "benchmarks": benchmarks,
            "usage": usage_today_summary(session),
            "cap": settings.api_daily_call_cap,
            "env_models": {"writer": settings.writer_model,
                           "classifier": settings.classifier_model,
                           "researcher": settings.effective_researcher_model},
        })
    finally:
        session.close()


@router.post("/llm/add")
async def add_llm(name: str = Form(...), provider_type: str = Form(...),
                  base_url: str = Form(""), api_key: str = Form("")):
    from engine.llm import PROVIDER_TYPES

    if provider_type not in PROVIDER_TYPES:
        return _redirect(f"Unknown provider type '{provider_type}'", "/admin/models")
    session = new_session()
    try:
        conn = Connection(
            kind=ConnectionKind.LLM, name=name.strip(), is_active=True,
            config_json={"provider_type": provider_type,
                         "base_url": base_url.strip(), "api_key": api_key.strip()},
        )
        session.add(conn)
        session.commit()
        log_event(session, "admin", f"LLM connection '{conn.name}' ({provider_type}) saved")
        return _redirect(f"LLM connection '{conn.name}' saved. Test it, then assign roles.",
                         "/admin/models")
    finally:
        session.close()


@router.post("/llm/{connection_id}/test")
async def test_llm(connection_id: int):
    from engine.llm import provider_from_connection

    session = new_session()
    try:
        conn = session.get(Connection, connection_id)
        if not conn or conn.kind != ConnectionKind.LLM:
            return _redirect("LLM connection not found", "/admin/models")
        try:
            provider = provider_from_connection(conn)
            ok, detail = await provider.test()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)[:300]
        log_event(session, "admin",
                  f"LLM connection '{conn.name}' test: {'OK' if ok else 'FAILED'} ({detail})",
                  level="INFO" if ok else "WARNING")
        return _redirect(f"Test {'passed' if ok else 'FAILED'}: {detail}", "/admin/models")
    finally:
        session.close()


@router.post("/llm/{connection_id}/delete")
async def delete_llm(connection_id: int):
    from db.models import ModelRole

    session = new_session()
    try:
        conn = session.get(Connection, connection_id)
        if conn and conn.kind == ConnectionKind.LLM:
            name = conn.name
            for row in session.execute(
                select(ModelRole).where(ModelRole.llm_connection_id == connection_id)
            ).scalars():
                session.delete(row)  # roles fall back to local env defaults
            session.delete(conn)
            session.commit()
            log_event(session, "admin", f"LLM connection '{name}' deleted, "
                      f"dependent roles reset to local defaults")
        return _redirect("LLM connection deleted (dependent roles reset to local).",
                         "/admin/models")
    finally:
        session.close()


@router.post("/models/roles")
async def save_role(role: str = Form(...), connection_id: str = Form(""),
                    model_name: str = Form("")):
    from db.models import ModelRole
    from engine.llm import ROLES

    if role not in ROLES:
        return _redirect(f"Unknown role '{role}'", "/admin/models")
    session = new_session()
    try:
        conn_id = int(connection_id) if connection_id.strip() else None
        if conn_id is not None:
            conn = session.get(Connection, conn_id)
            if not conn or conn.kind != ConnectionKind.LLM:
                return _redirect("Connection not found", "/admin/models")
        row = session.get(ModelRole, role)
        if row is None:
            row = ModelRole(role=role)
            session.add(row)
        row.llm_connection_id = conn_id
        row.model_name = model_name.strip()
        session.commit()
        log_event(session, "admin",
                  f"Role '{role}' -> connection {conn_id or 'local'}, "
                  f"model '{model_name or '(env default)'}'")
        return _redirect(f"Role '{role}' updated.", "/admin/models")
    finally:
        session.close()


@router.post("/models/roles/{role}/reset")
async def reset_role(role: str):
    from db.models import ModelRole

    session = new_session()
    try:
        row = session.get(ModelRole, role)
        if row:
            session.delete(row)
            session.commit()
            log_event(session, "admin", f"Role '{role}' reset to env default")
        return _redirect(f"Role '{role}' reset to the .env default.", "/admin/models")
    finally:
        session.close()


@router.post("/models/benchmark")
async def rebenchmark():
    import json as _json

    from engine.llm import _local_ollama
    from engine.state import set_state
    from engine.util import utcnow

    session = new_session()
    try:
        provider = _local_ollama()
        try:
            installed = await provider.list_models()
        except Exception as exc:  # noqa: BLE001
            return _redirect(f"Ollama unreachable: {str(exc)[:200]}", "/admin/models")
        results = {}
        for model in installed:
            try:
                results[model] = {"tps": await provider.benchmark(model),
                                  "at": f"{utcnow():%Y-%m-%d %H:%M}"}
            except Exception as exc:  # noqa: BLE001
                results[model] = {"tps": 0, "at": f"error: {str(exc)[:80]}"}
        set_state(session, "model_benchmarks", _json.dumps(results))
        log_event(session, "admin", f"Benchmarked {len(results)} local models")
        return _redirect(f"Benchmarked {len(results)} local models.", "/admin/models")
    finally:
        session.close()


# ──────────────────────────────── Campaign ───────────────────────────────────

@router.get("/campaign")
async def campaign_page(request: Request, msg: str = ""):
    from dataclasses import asdict

    from engine.campaign import CAMPAIGN_FIELDS, get_campaign

    session = new_session()
    try:
        campaign = get_campaign(session)
        return templates.TemplateResponse(request, "admin_campaign.html", {
            "msg": msg, "active": "admin", "admin_tab": "campaign",
            "fields": CAMPAIGN_FIELDS, "values": asdict(campaign),
            "placeholder": campaign.is_placeholder, "source": campaign.source,
        })
    finally:
        session.close()


@router.post("/campaign/save")
async def campaign_save(request: Request):
    from engine.campaign import CAMPAIGN_FIELDS

    form = await request.form()
    data = {key: str(form.get(key, "")).strip() for key, _ in CAMPAIGN_FIELDS}
    for int_key in ("default_job_value", "missed_calls_per_week"):
        try:
            data[int_key] = int(data[int_key] or 0)
        except ValueError:
            return _redirect(f"'{int_key}' must be a number", "/admin/campaign")
    session = new_session()
    try:
        conn = session.execute(
            select(Connection).where(Connection.kind == ConnectionKind.CAMPAIGN)
            .limit(1)
        ).scalar_one_or_none()
        if conn is None:
            conn = Connection(kind=ConnectionKind.CAMPAIGN, name="campaign",
                              is_active=True, config_json=data)
            session.add(conn)
        else:
            conn.config_json = data
            conn.is_active = True
        session.commit()
        log_event(session, "admin", f"Campaign profile saved ({data.get('company')})")
        return _redirect("Campaign saved. New drafts use it immediately.",
                         "/admin/campaign")
    finally:
        session.close()


# ────────────────────────────── Sending rails ────────────────────────────────

@router.get("/rails")
async def rails_page(request: Request, msg: str = ""):
    from engine.config import (HARD_JITTER_MIN_SECONDS, HARD_MAX_DAILY,
                               HARD_SEND_TIMEZONE, HARD_SEND_WINDOW_END,
                               HARD_SEND_WINDOW_START, HARD_WARMUP_SCHEDULE,
                               HARD_WARMUP_VOLUME, get_settings)
    from engine.rails import eff_breaker, eff_daily_cap, eff_jitter, eff_window

    settings = get_settings()
    window_start, window_end = eff_window()
    jitter_min, jitter_max = eff_jitter()
    breaker_rate, breaker_window = eff_breaker()
    return templates.TemplateResponse(request, "admin_rails.html", {
        "msg": msg, "active": "admin", "admin_tab": "rails",
        "hard": {"warmup": HARD_WARMUP_SCHEDULE, "volume": HARD_WARMUP_VOLUME,
                 "max_daily": HARD_MAX_DAILY,
                 "window": f"{HARD_SEND_WINDOW_START}-{HARD_SEND_WINDOW_END} "
                           f"{HARD_SEND_TIMEZONE}, Mon-Fri",
                 "jitter_min": HARD_JITTER_MIN_SECONDS // 60},
        "eff": {"daily_cap": eff_daily_cap(), "window_start": window_start,
                "window_end": window_end, "jitter_min": jitter_min,
                "jitter_max": jitter_max, "breaker_rate": breaker_rate,
                "breaker_window": breaker_window},
        "mode": settings.engine_mode.upper(),
    })


@router.post("/rails/save")
async def rails_save(daily_send_cap: str = Form(""), send_window_start: str = Form(""),
                     send_window_end: str = Form(""), jitter_min_minutes: str = Form(""),
                     jitter_max_minutes: str = Form(""), bounce_breaker_rate: str = Form(""),
                     bounce_breaker_window: str = Form("")):
    from engine.rails import save_overrides

    session = new_session()
    try:
        stored = save_overrides(session, {
            "daily_send_cap": daily_send_cap, "send_window_start": send_window_start,
            "send_window_end": send_window_end, "jitter_min_minutes": jitter_min_minutes,
            "jitter_max_minutes": jitter_max_minutes,
            "bounce_breaker_rate": bounce_breaker_rate,
            "bounce_breaker_window": bounce_breaker_window,
        })
        log_event(session, "admin", f"Sending rails tightened: {stored}")
        return _redirect("Rails saved (values are clamped tighten-only, server side).",
                         "/admin/rails")
    finally:
        session.close()


# ─────────────────────────────── Templates ───────────────────────────────────

def _sample_prospect():
    """Transient prospect for template preview; never persisted."""
    from db.models import Prospect

    return Prospect(
        name="Bayshore Cooling And Heating", trade="hvac", city="tampa",
        state="FL", owner_name="Mike Rivera", rating=4.8, review_count=57,
        email="mike@bayshorecooling.example",
        intel_json={"years_in_business": "since 2009",
                    "card": {"human_detail": "in business since 2009"}},
    )


@router.get("/templates")
async def templates_page(request: Request, msg: str = "", name: str = "initial.j2"):
    from engine.writer import (EDITABLE_TEMPLATES, SEQUENCE_TEMPLATES,
                               check_style, read_template_source,
                               render_email_template, render_footer)
    from db.models import TouchType

    editable = list(EDITABLE_TEMPLATES)
    if name not in editable:
        name = "initial.j2"
    source, is_override = read_template_source(name)

    preview_subject = preview_body = render_error = ""
    violations: list[str] = []
    sample = _sample_prospect()
    if name != "footer.j2":
        # Every initial_* variant is an EMAIL_1 for link-discipline purposes.
        touch_type = ({v: k for k, v in SEQUENCE_TEMPLATES.items()}.get(name)
                      or TouchType.EMAIL_1)
        try:
            preview_subject, preview_body = render_email_template(name, sample)
            violations = check_style(preview_subject, preview_body, sample, touch_type)
        except Exception as exc:  # noqa: BLE001 (broken override must not 500)
            render_error = str(exc)[:300]
    else:
        try:
            preview_body = render_footer()
        except Exception as exc:  # noqa: BLE001
            render_error = str(exc)[:300]

    return templates.TemplateResponse(request, "admin_templates.html", {
        "msg": msg, "active": "admin", "admin_tab": "templates",
        "editable": editable, "name": name, "source": source,
        "is_override": is_override, "preview_subject": preview_subject,
        "preview_body": preview_body, "violations": violations,
        "render_error": render_error,
    })


@router.post("/templates/save")
async def templates_save(name: str = Form(...), source: str = Form(...)):
    from engine.writer import save_template_override

    session = new_session()
    try:
        try:
            save_template_override(name, source)
        except ValueError as exc:
            return _redirect(str(exc), "/admin/templates")
        log_event(session, "admin", f"Template '{name}' override saved")
        return _redirect(f"'{name}' saved to data/templates/ (repo default untouched).",
                         f"/admin/templates?name={name}")
    finally:
        session.close()


@router.post("/templates/reset")
async def templates_reset(name: str = Form(...)):
    from engine.writer import reset_template_override

    session = new_session()
    try:
        existed = reset_template_override(name)
        if existed:
            log_event(session, "admin", f"Template '{name}' override removed")
        return _redirect(
            f"'{name}' {'restored to the repo default' if existed else 'had no override'}.",
            f"/admin/templates?name={name}")
    finally:
        session.close()


# ─────────────────────────────── Prospector ──────────────────────────────────

@router.get("/prospector")
async def prospector_page(request: Request, msg: str = ""):
    from engine.providers import provider_availability
    from engine.prospector_settings import (eff_franchise_keywords,
                                            eff_provider, eff_registry_state,
                                            eff_require_website,
                                            eff_review_bounds, valid_providers)

    min_reviews, max_reviews = eff_review_bounds()
    return templates.TemplateResponse(request, "admin_prospector.html", {
        "msg": msg, "active": "admin", "admin_tab": "prospector",
        "providers": valid_providers(), "provider": eff_provider(),
        "availability": provider_availability(),
        "registry_state": eff_registry_state(),
        "require_website": eff_require_website(),
        "min_reviews": min_reviews, "max_reviews": max_reviews,
        "franchise_keywords": ", ".join(eff_franchise_keywords()),
    })


@router.post("/prospector/save")
async def prospector_save(provider: str = Form(""), registry_state: str = Form(""),
                          require_website: str = Form(""),
                          min_review_count: str = Form(""),
                          max_review_count: str = Form(""),
                          franchise_keywords: str = Form("")):
    from engine.prospector_settings import save_overrides

    session = new_session()
    try:
        stored = save_overrides(session, {
            "provider": provider, "registry_state": registry_state,
            "require_website": bool(require_website),
            "min_review_count": min_review_count,
            "max_review_count": max_review_count,
            "franchise_keywords": franchise_keywords,
        })
        log_event(session, "admin", f"Prospector settings saved: {stored}")
        return _redirect("Prospector settings saved.", "/admin/prospector")
    finally:
        session.close()


# ─────────────────────────────── Suppression ─────────────────────────────────

@router.get("/suppression")
async def suppression_page(request: Request, msg: str = "", q: str = ""):
    from sqlalchemy import func

    from db.models import Suppression

    session = new_session()
    try:
        stmt = select(Suppression).order_by(Suppression.added_at.desc())
        if q:
            stmt = stmt.where(Suppression.email.like(f"%{q.lower()}%"))
        entries = session.execute(stmt.limit(500)).scalars().all()
        total = session.execute(select(func.count(Suppression.email))).scalar() or 0
        return templates.TemplateResponse(request, "admin_suppression.html", {
            "msg": msg, "active": "admin", "admin_tab": "suppression",
            "entries": entries, "total": total, "q": q,
        })
    finally:
        session.close()


@router.post("/suppression/add")
async def suppression_add(email: str = Form(...), reason: str = Form("manual")):
    from engine.sender import add_suppression

    email = email.strip().lower()
    if "@" not in email:
        return _redirect("That does not look like an email address",
                         "/admin/suppression")
    session = new_session()
    try:
        add_suppression(session, email, f"manual: {reason.strip() or 'admin'}")
        return _redirect(f"{email} suppressed forever.", "/admin/suppression")
    finally:
        session.close()


@router.get("/suppression/export.csv")
async def suppression_export():
    import csv as _csv
    import io

    from db.models import Suppression

    session = new_session()
    try:
        buffer = io.StringIO()
        writer = _csv.writer(buffer, lineterminator="\n")
        writer.writerow(["email", "reason", "added_at"])
        for entry in session.execute(
            select(Suppression).order_by(Suppression.added_at)
        ).scalars():
            writer.writerow([entry.email, entry.reason, entry.added_at])
        return PlainTextResponse(buffer.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": "attachment; filename=suppression.csv"})
    finally:
        session.close()


# ────────────────────────────── Data tools ───────────────────────────────────

@router.get("/data")
async def data_page(request: Request, msg: str = ""):
    from sqlalchemy import func

    from db.models import Prospect, Reply, Touch

    session = new_session()
    try:
        counts = {
            "prospects": session.execute(select(func.count(Prospect.id))).scalar() or 0,
            "touches": session.execute(select(func.count(Touch.id))).scalar() or 0,
            "replies": session.execute(select(func.count(Reply.id))).scalar() or 0,
        }
        dry = sum(1 for t in session.execute(select(Touch)).scalars()
                  if (t.meta_json or {}).get("dry"))
        return templates.TemplateResponse(request, "admin_data.html", {
            "msg": msg, "active": "admin", "admin_tab": "data",
            "counts": counts, "dry_touches": dry,
        })
    finally:
        session.close()


@router.get("/data/export/{table}.csv")
async def data_export(table: str):
    from engine.data_tools import EXPORTS, export_csv

    if table not in EXPORTS:
        return _redirect(f"Unknown table '{table}'", "/admin/data")
    session = new_session()
    try:
        return PlainTextResponse(export_csv(session, table), media_type="text/csv",
                                 headers={"Content-Disposition":
                                          f"attachment; filename={table}.csv"})
    finally:
        session.close()


@router.post("/data/import")
async def data_import(file: UploadFile = File(...), trade: str = Form("")):
    from engine.data_tools import import_prospects_csv

    payload = await file.read()
    if not payload:
        return _redirect("Empty file", "/admin/data")
    session = new_session()
    try:
        summary = import_prospects_csv(session, payload, trade.strip())
        return _redirect(
            f"Imported: kept {summary['kept']} of {summary['found']} "
            f"({summary['duplicates']} duplicates). Run /find or the pipeline "
            f"to enrich + verify.", "/admin/data")
    except Exception as exc:  # noqa: BLE001 (bad CSVs must not 500 the panel)
        return _redirect(f"Import failed: {str(exc)[:200]}", "/admin/data")
    finally:
        session.close()


@router.post("/data/purge-dry")
async def data_purge_dry():
    from engine.data_tools import purge_dry_run

    session = new_session()
    try:
        result = purge_dry_run(session)
        return _redirect(
            f"Purged {result['touches_deleted']} dry touches, "
            f"{result['eml_deleted']} .eml files; reverted "
            f"{result['prospects_reverted']} prospects.", "/admin/data")
    finally:
        session.close()
