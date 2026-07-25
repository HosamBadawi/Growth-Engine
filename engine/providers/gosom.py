"""Wrapper around the gosom/google-maps-scraper binary (MIT, Go, run as subprocess).

Download the Windows release (google_maps_scraper-<version>-windows-amd64.exe)
from https://github.com/gosom/google-maps-scraper/releases into bin/ and point
GOSOM_BINARY at it.
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from engine.config import get_settings
from engine.providers.base import ProspectProvider, RawProspect, parse_city_state

log = logging.getLogger("prospector.gosom")


def _first(record: dict, *keys, default=""):
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return default


def _to_int(value) -> int | None:
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


def parse_results_file(path: Path, source: str = "gosom") -> list[RawProspect]:
    """Parse gosom -json output: JSON lines, with fallback to a JSON array."""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    records: list[dict] = []
    try:
        data = json.loads(text)
        records = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    prospects = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        name = _first(rec, "title", "name")
        if not name:
            continue
        address = _first(rec, "address")
        complete = rec.get("complete_address") or {}
        city = _first(complete, "city") if isinstance(complete, dict) else ""
        state = _first(complete, "state") if isinstance(complete, dict) else ""
        if not city or not state:
            parsed_city, parsed_state = parse_city_state(address)
            city = city or parsed_city
            state = state or parsed_state
        emails = rec.get("emails") or []
        if isinstance(emails, str):
            emails = [e.strip() for e in emails.split(",") if e.strip()]
        prospects.append(
            RawProspect(
                name=str(name).strip(),
                category=str(_first(rec, "category", "categories")).strip(),
                phone=str(_first(rec, "phone", "phone_number")).strip(),
                website=str(_first(rec, "website", "web_site", "site")).strip(),
                address=str(address).strip(),
                city=str(city).strip(),
                state=str(state).strip(),
                rating=_to_float(_first(rec, "review_rating", "rating", default=None)),
                review_count=_to_int(_first(rec, "review_count", "reviews", default=None)),
                maps_url=str(_first(rec, "link", "url", "place_url")).strip(),
                emails=[str(e) for e in emails],
                source=source,
            )
        )
    return prospects


class GosomProvider(ProspectProvider):
    name = "gosom"

    def search(self, query: str, limit: int,
               exclude_keys: set[str] | None = None,
               progress=None) -> list[RawProspect]:
        # exclude_keys/progress accepted for interface compatibility and ignored:
        # this provider's dedupe key needs the scraped name+city, so exclusion
        # cannot happen before the work. _is_duplicate() remains the backstop.
        settings = get_settings()
        binary = Path(settings.gosom_binary)
        if not binary.exists():
            raise FileNotFoundError(
                f"gosom binary not found at '{binary}'. Download the windows-amd64 "
                "release from https://github.com/gosom/google-maps-scraper/releases "
                "into bin/ (see README) or set PROSPECT_PROVIDER=csv."
            )

        with tempfile.TemporaryDirectory(prefix="gosom_") as tmp:
            tmp_path = Path(tmp)
            input_file = tmp_path / "queries.txt"
            results_file = tmp_path / "results.json"
            input_file.write_text(query + "\n", encoding="utf-8")

            cmd = [
                str(binary),
                "-input", str(input_file),
                "-results", str(results_file),
                "-json",
                "-depth", str(settings.gosom_depth),
                "-c", "2",
                "-lang", "en",
                "-exit-on-inactivity", "3m",
            ]
            if settings.gosom_extra_args:
                cmd.extend(settings.gosom_extra_args.split())

            log.info("Running gosom: %s", " ".join(cmd))
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",  # gosom prints UTF-8; Windows default cp1252 crashes
                    errors="replace",
                    timeout=settings.gosom_timeout_seconds,
                )
                if proc.returncode != 0:
                    log.warning("gosom exited %s: %s", proc.returncode,
                                (proc.stderr or "")[-2000:])
            except subprocess.TimeoutExpired:
                proc = None
                log.warning("gosom timed out after %ss; using partial results",
                            settings.gosom_timeout_seconds)

            if not results_file.exists() or not results_file.read_text(
                    encoding="utf-8", errors="replace").strip():
                if proc is not None:
                    log.warning("gosom produced no results. stdout tail: %s | stderr tail: %s",
                                (proc.stdout or "")[-1500:], (proc.stderr or "")[-1500:])
                return []
            prospects = parse_results_file(results_file, source=self.name)
            return prospects[: limit * 3]  # headroom, code-level filters trim later
