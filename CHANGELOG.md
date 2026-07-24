# Changelog

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
