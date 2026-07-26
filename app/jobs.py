"""In-process background jobs for the web UI (find runs, drafting).

Same pattern the Telegram bot already uses (asyncio.create_task) plus a small
registry so page reloads and polling can observe progress. Local single-user
app: process memory is the right scope, no queue dependency needed.
"""
import asyncio
import itertools
from typing import Awaitable, Callable

_jobs: dict[int, dict] = {}
_counter = itertools.count(1)
_KEEP = 20


def start_job(title: str,
              factory: Callable[[Callable[[str], Awaitable[None]]], Awaitable]) -> int:
    """factory receives an async progress(text) callback; returns the job id."""
    job_id = next(_counter)
    _jobs[job_id] = {"title": title, "lines": [], "done": False, "error": None}

    async def progress(text: str) -> None:
        _jobs[job_id]["lines"].append(str(text))

    async def runner() -> None:
        try:
            result = await factory(progress)
            if result is not None:
                _jobs[job_id]["lines"].append(f"Result: {result}")
        except Exception as exc:  # noqa: BLE001 (surfaced to the page, never raised)
            _jobs[job_id]["error"] = str(exc)[:300]
        finally:
            _jobs[job_id]["done"] = True

    asyncio.create_task(runner())
    for old_id in sorted(_jobs)[:-_KEEP]:
        _jobs.pop(old_id, None)
    return job_id


def job_state(job_id: int) -> dict | None:
    return _jobs.get(job_id)


def latest_job_id() -> int | None:
    return max(_jobs) if _jobs else None
