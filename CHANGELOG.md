# Changelog

## 2.2.0 (2026-07-26)

**Turning a bare business name into a reachable contact.**

- **Contact discovery ladder** (`engine/discovery.py`): rungs A-G, cheapest
  first, short-circuiting. The rungs are provider data, country-aware domain
  guess, scored web search, social extraction, link-in-bio resolution, site
  crawl, phone consolidation. Every populated field records the rung, source and
  confidence that produced it. Hard per-business wall-clock ceiling returning
  partial results (measured worst case 24.0s against a 25s budget over 20 live
  businesses; the same code measured 33.0s before the ceiling was structural).
- **A wrong website is worse than none.** The domain must resemble the business,
  not merely mention it: directories, magazines, dining clubs and domain-parking
  pages are rejected by host. Five wrong sites returned by a live Rio run are
  now regression tests. Stale directory URLs stored by earlier versions are
  cleared on re-enrichment.
- **v2.1 defect fixes**: provider list derived from the registry (osm/places are
  saveable again), Places `nextPageToken` pagination with per-page billing and a
  clean stop at the cap, `require_website` toggle, country-aware TLD guessing
  (every real Brazilian site is a `.com.br`; a hardcoded `.com` could not
  succeed outside the US), corrected Enterprise-SKU cost documentation with the
  daily cap lowered to 30.
- **No-website segment**: derived segments (no website / emailable / manual
  only) with counts, filtering and export. Prospects without a verified address
  are refused at queue time and at send time: a safety property, not a UI hint.
  A second campaign profile can serve that segment its own offer and templates.
- **Backfill** (`/enrich`, Actions page): re-run the ladder over prospects you
  already have, by filter, resumable, budgeted, provenance-respecting.
- **Data out**: export the view you are looking at (stage, segment, search) and
  export a run *including the rows it rejected*, with reasons. A Runs & rejects
  page makes filtered candidates inspectable and retryable. All CSV is
  `utf-8-sig`, so Portuguese and Arabic names survive Excel.
- **Pipeline scoping**: stages process only the current run's prospects instead
  of re-processing the whole database forever, plus an `UNREACHABLE` terminal
  status after 3 failed attempts (still visible and exportable).
- Fixed Alembic's `fileConfig` silently disabling every logger created before
  migrations ran at startup.


## 2.1.0 (2026-07-25)

- **Dedupe exclusion**: `/find` skips already-imported businesses *before* paying
  for website discovery (new indexed `dedupe_key` column, backfilled by
  migration 0002; lazy registry iteration; `skipped known` in run summaries;
  clear exhaustion message when a city runs dry).
- **Discovery robustness**: website cache flushes atomically every 10
  resolutions and on interrupt; 30-day negative cache for businesses with no
  findable site; live progress lines during long discovery runs; cheaper miss
  path (3s domain probes, search backoff capped at one alternate engine).
- **Pipeline-stage UI**: Leads replaced by Prospects / Drafts / In Sequence /
  Replies / Closed. Every prospect on exactly one page, stages derived live,
  global cross-stage search, count badges.
- **Web parity**: Actions page runs find (with live progress), drafting,
  queueing, pause/resume from the browser. Going LIVE still requires the
  Telegram confirmation: dual-channel by design.
- **New providers**: `osm` (OpenStreetMap/Overpass: free, keyless, worldwide,
  any niche) and `places` (Google Places API: field-masked, hard daily cap,
  SKU-logged). Per-run provider picker with availability status.

## 2.0.0 (2026-07-24)

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

## 1.0.0 (2026-07-21)

- Initial engine: Prospector (license registries / Google Maps / CSV) → Enricher
  → Verifier → Researcher → Writer → Sender → Reply Watcher → Reporter →
  Dashboard, commanded from Telegram, local Ollama, hard deliverability rails
  (warm-up ramp, send window, jitter, suppression-forever, bounce breaker,
  DRY_RUN / SANDBOX / LIVE with `/golive` confirmation).
