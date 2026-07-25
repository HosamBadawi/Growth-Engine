# Growth Engine

![CI](https://github.com/HosamBadawi/growth-engine/actions/workflows/ci.yml/badge.svg)

An open-source AI outbound machine: find prospect clients, research them, write
personalized emails with **local or API LLMs**, send safely behind hard
deliverability rails, watch replies, and track everything  commanded from
Telegram, observed on a local web dashboard.

Runs 100% free on a local [Ollama](https://ollama.com) model out of the box; plug
in OpenAI, Anthropic, OpenRouter, Groq, or any OpenAI-compatible API from the
dashboard when you want to. Defaults to `DRY_RUN`  it writes `.eml` files to
`/outbox` and sends nothing until you deliberately go live.

```
TELEGRAM BOT (aiogram 3) ──► ORCHESTRATOR (FastAPI + APScheduler)
 [1 PROSPECTOR] → [2 ENRICHER] → [3 VERIFIER] → [4 RESEARCHER] → [5 WRITER]
                     DATABASE (SQLite via SQLAlchemy 2.0, Postgres-ready)
 [6 SENDER]  [7 REPLY WATCHER]  [8 REPORTER]  [9 DASHBOARD localhost:8080]
```

## Features

- **Multi-provider LLM layer**  local Ollama by default (zero config, zero cost);
  add OpenAI / Anthropic / OpenRouter / Groq / any OpenAI-compatible endpoint from
  Admin → Models, assignable per role (writer / classifier / researcher). API
  providers are cost-guarded with a daily call cap and automatic fallback to local.
- **Prospector** with swappable sources: US contractor **license registries**
  (public records, zero-risk, the default), a Google Maps scraper, or CSV import.
- **Enricher** crawls each prospect's site (robots-aware) for emails, owner names,
  and negative signals (no chat widget / no online booking = a better prospect).
- **Verifier**  syntax → MX → optional SMTP probe, with graceful port-25
  degradation. Unverifiable addresses are never sent to.
- **Researcher**  summarize the scraped site (local) or run real web deep-research
  per prospect (web mode), distilled into a 3-bullet intel card.
- **Writer**  personalized cold emails from your campaign profile + templates,
  with machine-enforced style rules and a deterministic template fallback.
- **Sender** with sacred deliverability rails (below), warm-up ramp, send window,
  jitter, suppression, bounce circuit breaker, and Day 0/3/6/10 sequences that
  cancel the instant a reply arrives.
- **Reply watcher**  IMAP poll → LLM classification → Telegram alert with a
  suggested reply you approve, edit, or ignore.
- **Full admin dashboard**  models, campaign profile, sending rails (tighten-only),
  email templates with live preview, prospector, suppression, data tools, health.
- **Telegram command center**  run the whole machine from your phone.

## Quickstart

Requirements: Python 3.11+, [Ollama](https://ollama.com) running locally.

### Windows (easiest)

```
ollama pull qwen2.5:7b-instruct
```

Then **double-click `start.bat`**. On first run it creates the virtual
environment, installs everything, and copies `.env.example` to `.env`; after
that it starts in seconds. Set a strong `DASHBOARD_PASSWORD` in `.env`.

To reach the dashboard from your phone, double-click **`start-with-phone-access.bat`**
instead — it prints a free `https://<random>.trycloudflare.com` link (no account,
no port forwarding; requires `bin/cloudflared.exe` and a non-default password).

### Windows (manual) / Linux / macOS

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# one local model does the writer + classifier + researcher jobs (a 7B Q4 fits an 8GB GPU)
ollama pull qwen2.5:7b-instruct

copy .env.example .env              # Linux/macOS: cp; then set DASHBOARD_PASSWORD and SECRET_KEY
.venv\Scripts\python.exe run.py     # Linux/macOS: .venv/bin/python run.py
```

> Use the venv's Python, not a bare `python run.py` — a system Python without the
> project's packages fails with `No module named 'aiogram'`. `start.bat` avoids this.

### Linux / macOS

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ollama pull qwen2.5:7b-instruct
cp .env.example .env        # then set DASHBOARD_PASSWORD and SECRET_KEY
python run.py
```

Dashboard: <http://localhost:8080> (password = `DASHBOARD_PASSWORD`). On first run
the engine verifies your models exist in Ollama and prints tokens/sec so you can
tune. Telegram and email are optional and configured from **Admin → Connections**
(saved in the local DB, n8n style)  the dashboard and CLI work without them.

Then open **Admin → Campaign** and fill in your company, pitch, links, and
signature. Until you do, the engine runs a clearly-marked example campaign and
shows a banner.

New to it? A plain-English, non-technical walkthrough of every feature — including
how to collect and review prospects with **no email sent** — is in
[`docs/Growth-Engine-User-Guide.pdf`](docs/Growth-Engine-User-Guide.pdf).

## Configuration

Most things are configured live from the dashboard (Admin panel). `.env` holds
the bootstrap fallbacks and the few things that must not be one browser click away.

| Key | What |
|---|---|
| `ENGINE_MODE` | `DRY_RUN` (default) / `SANDBOX` / `LIVE`  **`.env` only, by design** |
| `DATABASE_URL` | SQLite by default; Postgres-ready |
| `OLLAMA_BASE_URL` | local Ollama endpoint |
| `WRITER_MODEL` / `CLASSIFIER_MODEL` / `RESEARCHER_MODEL` | default local models per role |
| `API_DAILY_CALL_CAP` | max calls/day per API provider, then local fallback |
| `RESEARCH_MODE` | `local` (summarize site) or `web` (real deep research) |
| `DASHBOARD_PASSWORD` / `SECRET_KEY` | dashboard login + cookie signing |
| `DASHBOARD_HOST` | `127.0.0.1` by default  see security notes |

Telegram bots, email accounts (SMTP + IMAP + sender identity), LLM providers, and
the campaign profile are all managed in the Admin panel and stored in the local
database, with `.env` as the fallback.

## Prospect sources

| Provider | Coverage | Cost / keys | Data quality | Notes |
|---|---|---|---|---|
| `registry` (default) | US licensed trades (FL shipped) | free, none | owner + license no, no website (discovered) | public records, zero scraping risk |
| `osm` | worldwide, any niche | free, none | name/address, phone+website when mapped | OpenStreetMap via Overpass |
| `places` | worldwide, any niche | **paid**, `PLACES_API_KEY` | Google's business data incl. website + ratings | field-masked, hard daily cap |
| `csv` | anything you import | free | whatever your file has | Admin → Data or `find "csv:..."` |
| `gosom` | worldwide | free binary | Maps data | fingerprinted by Google; needs US egress; often fails elsewhere |

- **Registry**: bulk CSV public records. Adding a state is one entry in
  `engine/providers/registry.py`. Re-runs skip already-known businesses before
  any website discovery, so `/find ... 10` always chases 10 *new* prospects.
- **OSM (OpenStreetMap)**: keyless and worldwide — restaurants in Giza, cafes in
  Alexandria, any tagged niche anywhere. Respects the public Overpass instance
  usage policy (descriptive User-Agent, ≥2s between queries, backoff on 429/504,
  7-day response cache). **Heavy users should self-host an Overpass instance**
  rather than lean on the free public ones. Unknown niches fall back to a
  name-substring match and say so in the log.
- **Google Places**: the legitimate, ToS-compliant way to get Google's business
  data. Costs real money per call: a free allowance exists, but pricing has been
  restructured over time and sources conflict — **confirm the current figure in
  your own Cloud Console before enabling**, which is exactly why the daily cap
  (`PLACES_DAILY_CALL_CAP`, default 200) defaults low and the engine stops hard
  at the cap instead of silently spending. Every call is logged with its SKU in
  the Activity page.
- **Google Maps (gosom)**: wraps the MIT `gosom/google-maps-scraper` binary as a
  subprocess. Google fingerprints headless automation, so this is most reliable
  from a US residential/VPS egress; some networks see it fail with
  `unexpected page type`. Prefer `places` (paid, reliable) or `osm` (free).

## Safety rails (hard-coded, env can only tighten)

- **Warm-up ramp**: day 1-7 max 10/day, day 8-14 max 20/day, then 30/day. A
  volume ramp (first 70 sends 10/day, first 210 20/day) also applies so a pause
  can't calendar-skip the warm-up.
- **Send window**: 9:00-16:30 US Eastern, Mon-Fri, hard-clamped in code; 3-12 min
  randomized jitter between sends. A failed SMTP attempt consumes the jitter slot
  too, so a rejecting provider can't cause a rapid-fire cascade.
- **CAN-SPAM footer** on every email: unsubscribe line + postal address.
- **Suppression** checked before every send; STOP/unsubscribe = suppressed forever
  (there is deliberately no un-suppress button).
- **Bounce breaker**: >3% bounces over the trailing 50 sends pauses everything and
  alerts you. 5xx SMTP rejections count as bounces.
- **Sequences** (Day 0 → 3 → 6 → 10) cancel instantly when any reply arrives.
- **DRY_RUN artifacts can never leak**: dry follow-ups are marked and auto-cancelled
  in real modes; inbox messages are marked seen only after successful handling.
- **LIVE requires two keys**: `ENGINE_MODE=LIVE` in `.env` **and** a one-time
  `/golive` confirmation in Telegram. Going live is never one browser click.

The tighten-only rail knobs in Admin → Rails are clamped server-side against these
constants; the UI can never loosen a rail.

## Telegram commands

Only your configured user id may command the bot.

```
/find <niche> <city> <n>   find + enrich + verify
/draft                     drafts for every verified prospect → approve/edit/skip buttons
/send                      queue approved drafts (caps + window apply)
/status  /report           funnel summary / full report
/models                    LLM role assignments + today's API usage
/pause all  /pause <email>  /resume all
/golive                    one-time confirmation gate for LIVE mode
```

Everything also works from the CLI without Telegram:

```bash
python -m engine.cli find "hvac" "tampa fl" 15
python -m engine.cli draft
python -m engine.cli research <prospect_id>
python -m engine.cli status
```

## Going LIVE safely (runbook)

1. **DRY_RUN** (default): run the full loop, inspect the `.eml` files in `/outbox`,
   watch the dashboard funnel. Nothing leaves the machine.
2. **Deliverability prep**: set up SPF, DKIM, and DMARC for your sending domain,
   use a dedicated sending address, fill your postal address in Admin → Campaign.
3. **SANDBOX**: `ENGINE_MODE=SANDBOX` + `SANDBOX_RECIPIENT=you@…`. Everything sends
   for real but only to your own inbox. Run it a few days.
4. **LIVE**: `ENGINE_MODE=LIVE`, restart, `/golive`, confirm. Warm-up caps start at
   10/day automatically. Watch the bounce rate on the Overview page.
5. If anything smells wrong: `/pause all`. The bounce breaker also pauses on its own.

## Tests, migrations, health

```bash
python -m pytest              # fully offline; mocks LLM/DNS/SMTP/network
```

Schema migrations run automatically at startup (Alembic). A nightly job backs up
the SQLite database to `data/backups/`. `GET /healthz` returns engine status JSON
(localhost, no auth).

## Responsible use

This is a tool for **legitimate B2B outreach**. You are responsible for complying
with the laws of your jurisdiction and your recipients'  including the US CAN-SPAM
Act and similar regulations  for honoring opt-outs promptly (the engine suppresses
them forever automatically), and for respecting the terms of service of every data
source you configure. Cold outreach carries legal and reputational risk; the safety
rails reduce it but do not remove your responsibility. Do not use this for spam.

## Security notes

- API keys and SMTP/IMAP passwords are stored in the local SQLite database
  (`data/growth.db`), like any self-hosted tool. Keep that machine and file private.
- The dashboard binds `127.0.0.1` by default. For remote access use a private
  network overlay (Tailscale, WireGuard)  never bind it to a public interface.
- Never commit your `.env` or `data/` directory. Both are gitignored.

## Architecture notes

- Every LLM call goes through `engine/llm/`  swap providers without touching call
  sites. Every prospect source implements `ProspectProvider`. The sender/bot can
  later move to a small VPS while Ollama stays on your PC (set `OLLAMA_BASE_URL` to
  your PC's private-network IP).
- Timestamps are naive UTC throughout. SQLite now, Postgres-ready via `DATABASE_URL`.

## License

MIT  see [LICENSE](LICENSE).
