"""Developer CLI so every module can be demoed without Telegram.

Examples (venv active, from repo root):
  python -m engine.cli find "plumber" "tampa fl" 15
  python -m engine.cli find "csv:leads.csv" "-" 20
  python -m engine.cli draft
  python -m engine.cli verify-email someone@example.com
  python -m engine.cli status
  python -m engine.cli send-tick
  python -m engine.cli benchmark
"""
import argparse
import asyncio
import json

from db.session import new_session
from engine.config import get_settings
from engine.logging_setup import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="growth-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find", help="prospect + enrich + verify")
    p_find.add_argument("niche")
    p_find.add_argument("city")
    p_find.add_argument("limit", type=int, nargs="?", default=15)

    sub.add_parser("draft", help="generate drafts for all VERIFIED prospects")
    sub.add_parser("status", help="funnel summary")
    sub.add_parser("send-tick", help="run one sender tick manually")
    sub.add_parser("benchmark", help="print tokens/sec for configured models")

    p_verify = sub.add_parser("verify-email", help="verify one address")
    p_verify.add_argument("email")

    p_research = sub.add_parser("research", help="build + print the intel card")
    p_research.add_argument("prospect_id", type=int)
    p_research.add_argument("--force", action="store_true",
                            help="ignore the cached card")

    args = parser.parse_args()
    setup_logging(get_settings().log_level)
    from db.migrate import run_migrations

    run_migrations()
    asyncio.run(_dispatch(args))


async def _dispatch(args) -> None:
    if args.cmd == "find":
        from engine.pipeline import run_find

        result = await run_find(args.niche, args.city, args.limit)
        print(json.dumps(result, indent=2))

    elif args.cmd == "draft":
        from sqlalchemy import select

        from db.models import Touch
        from engine.pipeline import generate_all_drafts

        ids = await generate_all_drafts()
        session = new_session()
        try:
            for touch_id in ids:
                touch = session.get(Touch, touch_id)
                print(f"\n=== Draft #{touch.id} for {touch.prospect.name} "
                      f"<{touch.prospect.email}> ===")
                print(f"Subject: {touch.subject}\n\n{touch.body}\n")
        finally:
            session.close()
        print(f"{len(ids)} drafts created (status DRAFT, approve via Telegram or dashboard)")

    elif args.cmd == "research":
        from db.models import Prospect
        from engine.researcher import build_intel_card

        session = new_session()
        try:
            prospect = session.get(Prospect, args.prospect_id)
            if not prospect:
                print(f"No prospect #{args.prospect_id}")
                return
            card = await build_intel_card(session, prospect, force=args.force)
            print(f"Intel card for {prospect.name} "
                  f"(mode={get_settings().research_mode}):")
            print(json.dumps(card, indent=2))
        finally:
            session.close()

    elif args.cmd == "verify-email":
        from engine.verifier import verify_email_address

        result = verify_email_address(args.email)
        print(f"level={result.level} role={result.is_role} detail={result.detail}")

    elif args.cmd == "status":
        from engine.reporter import build_report_text

        session = new_session()
        try:
            print(build_report_text(session))
        finally:
            session.close()

    elif args.cmd == "send-tick":
        from engine.sender import sender_tick

        await sender_tick()
        print("tick done (check /outbox in DRY_RUN)")

    elif args.cmd == "benchmark":
        from engine.ollama_client import benchmark, startup_check

        problems = await startup_check()
        for problem in problems:
            print(f"WARNING: {problem}")
        settings = get_settings()
        for label, model in (("writer", settings.writer_model),
                             ("classifier", settings.classifier_model)):
            try:
                tps = await benchmark(model)
                print(f"{label}: {model} -> {tps} tokens/sec")
            except Exception as exc:  # noqa: BLE001
                print(f"{label}: {model} -> benchmark failed: {exc}")


if __name__ == "__main__":
    main()
