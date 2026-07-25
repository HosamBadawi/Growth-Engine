# Changelog

## 2.1.0 — 2026-07-25

- **Dedupe exclusion**: `/find` skips already-imported businesses *before* paying
  for website discovery (new indexed `dedupe_key` column, backfilled by
  migration 0002; lazy registry iteration; `skipped known` in run summaries;
  clear exhaustion message when a city runs dry).
- **Discovery robustness**: website cache flushes atomically every 10
  resolutions and on interrupt; 30-day negative cache for businesses with no
  findable site; live progress lines during long discovery runs; cheaper miss
  path (3s domain probes, search backoff capped at one alternate engine).
- **Pipeline-stage UI**: Leads replaced by Prospects / Drafts / In Sequence /
  Replies / Closed — every prospect on exactly one page, stages derived live,
  global cross-stage search, count badges.
- **Web parity**: Actions page runs find (with live progress), drafting,
  queueing, pause/resume from the browser. Going LIVE still requires the
  Telegram confirmation — dual-channel by design.
- **New providers**: `osm` (OpenStreetMap/Overpass — free, keyless, worldwide,
  any niche) and `places` (Google Places API — field-masked, hard daily cap,
  SKU-logged). Per-run provider picker with availability status.

## 2.0.0 — 2026-07-24

- **Multi-provider LLM layer**: local Ollama by default; OpenAI / Anthropic /
  OpenRouter / Groq / any OpenAI-compatible API pluggable from the dashboard,
  assignable per role (writer / classifier / researcher), with a daily API call
  cap and automatic fallback to local, plus per-call usage logging.
- **Web researcher**: real per-prospect web research (`RESEARCH_MODE=web`) with
  ad-result filtering, robots-aware fetching, 7-day card cache, and a
  personalization quality gate that blocks scraped ad noise.
- **Full admin panel**: Models & Providers, Campaign profile, Sending rails
  (tighten-only, server-clamped), email template editor with live preview and
  override directory, Prospector settings, Suppression (no delete by design),
  Data tools (CSV export/import, DRY_RUN purge), health chips + `/healthz`.
- **Campaign profiles**: all sender identity, pitch, links, and offer numbers
  render from a configurable campaign; the codebase is fully brand-free.
- **Hardening**: Alembic migrations run at startup (existing databases adopted
  in place), nightly SQLite backups with retention, scheduler job-error alerts,
  log rotation, version surfacing, offline-safe test suite, GitHub Actions CI.
- Trade-aware job values, first-touch link discipline (demo link only in email 1),
  registry provider as the default prospect source.

## 1.0.0 — 2026-07-21

- Initial engine: Prospector (license registries / Google Maps / CSV) → Enricher
  → Verifier → Researcher → Writer → Sender → Reply Watcher → Reporter →
  Dashboard, commanded from Telegram, local Ollama, hard deliverability rails
  (warm-up ramp, send window, jitter, suppression-forever, bounce breaker,
  DRY_RUN / SANDBOX / LIVE with `/golive` confirmation).
