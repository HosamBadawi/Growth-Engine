# Changelog

## 2.5.1 (2026-08-17)

**Numbers must be real.** A draft claimed "50 calls a week mean missing out
on $2500.00" when the campaign says 5 calls a week at $500 a job. Every
number in a body must now be one the campaign or the prospect actually owns:
the ROI math (missed calls, job value, weekly and yearly loss), the product's
own constants (24/7, the 60 second demo), or a fact from the prospect's
grounded intel card (rating, reviews, years). Anything else is a violation
naming the bad number, so the retry loop fixes it. Money written with
decimals ("$2500.00") is rejected outright with the correct form ("$2,500")
in the message. URLs and the signature line are exempt from the scan.


## 2.5.0 (2026-08-16)

**Output polish: six deterministic rules that make machine tells impossible
rather than unlikely.** A hand review of the 16 v2.4 drafts found each of
these still shipping; every quoted failure is now a pinned regression test.

- **The greeting is a first name and nothing else**: the first line must match
  `Hi <Firstname>,` exactly. "Hi Ronald Mccrory at Anchor Construction Of
  Tampa in Tampa, FL," can no longer pass. The greeting is rendered
  deterministically and the model is told to keep it verbatim; a name the
  pattern cannot bless falls back to "Hi there,".
- **A wrong-city card kills the prospect, not just the detail**: discarding
  "20 years serving San Diego" and drafting anyway let the model fill the hole
  with "based in Tampa", a false statement. A card placing the business in
  another state now flags GEO_MISMATCH and the prospect is never drafted.
  Same-state service areas (St Petersburg on a Tampa card) stay fine.
- **Independent trade check**: `prospect.trade` says what the source claimed,
  and the source can be wrong. A company name or intel card carrying another
  trade's vocabulary (construction, builders, restaurant, insurance...) flags
  TRADE_MISMATCH and blocks the draft. "Contractor" and "building" only count
  in the NAME, so a licensed HVAC contractor's own prose is safe. When in
  doubt, do not draft.
- **Empty flattery is banned**: "local reputation", "built something worth
  protecting", "you clearly run a solid operation" and friends are rejected by
  rule, removed from all three templates, and the fallback compliment is gone:
  when nothing checkable exists the sentence is omitted entirely.
- **Prose casing enforced**: lowercase "hvac" or "a/c" anywhere in a body is a
  violation, and every string that reaches a template (including details
  lifted from intel cards) passes through the casing fix first.
- **No more subject collisions**: the "{city} {trade} question" pattern that
  produced one subject for three prospects is gone; every pattern carries the
  company name, a distinctive-company-token rule guards the subject, and
  overlong legal names fall back to the shortest pattern instead of breaking
  the 8 word rule.


## 2.4.0 (2026-08-07)

**v2.3 fixed the prose; v2.4 fixes the facts.** A real 23-draft run shipped
"prom dresses and bridal wear available" to an HVAC owner, queued
`%20pat.cox@...` and `support@constrafor.com`, drafted a US missed-call pitch
at a restaurant in Rio de Janeiro, and greeted fgolden@ as "Hi Lindsay". The
emails were well formed and wrong. This release grounds the inputs.

- **Grounded research or none**: a `human_detail` must be provable against the
  text actually fetched for THAT prospect (70% content-word overlap), must not
  name another city, must not belong to another industry (bridal, insurance,
  catering...), and must not repeat across prospects in a run. Cached cards
  are gated on read too, so a stale card cannot smuggle a hallucination past
  the new rules. On discard the writer falls back to the deterministic detail
  ("your 4.9 stars across 72 reviews"), which was the best draft of the run
  anyway. An email with no personalisation beats an email with a wrong fact.
- **Verifier rejects what cannot be an owner's inbox**: malformed addresses
  (whitespace, percent-encoding), a configurable role denylist (noreply,
  careers, catering, underwriting... while info/office/service/contact/admin
  stay allowed), placeholder local parts (demo/test/example/sample/your), and
  any domain that is neither the prospect's own website nor a free provider.
  Every rejection records a reason, and /report shows the breakdown.
- **Geo and niche gate**: TARGET_COUNTRY (default US) and TARGET_TRADES reject
  out-of-market prospects at prospecting time and again before drafting
  (foreign ccTLDs on the website or email count). The Rio restaurant case is
  now a permanent regression test.
- **Owner-name integrity**: a model-proposed owner name must appear in the
  prospect's own fetched text; the same owner name is never used on two
  unrelated prospects in one run; and a personal mailbox that shares nothing
  with the owner name is greeted "Hi there" rather than with a stranger's name.
- **The signature is now a rule**: a body that does not end with the campaign
  signature is rejected (one shipped draft ended on "Should I send you the
  demo?" with nothing after it).
- **The model no longer writes subjects**: the deterministic per-prospect
  pattern is rendered in code, the LLM contract is `{"body": ...}` only. The
  dominant observed failure (subject returned, body omitted, every retry
  burned) is structurally gone, and subject variety is better without the
  model anyway.
- **Writer reliability**: temperature 0.35, explicit Ollama `num_ctx: 8192`
  and `num_predict: 700` (the writer prompt overran the silent 2048 default),
  JSON retries now shrink the prompt instead of growing it, and the first 400
  characters of bad model output are logged so failures are diagnosable.
- **Per-role fallback chain**: a role's assigned model that fails now hands
  over to the env default (previously "fallback to local" was a no-op for
  local operators: it retried the exact model that had just failed). Every
  hand-over is an event and /report counts them.
- **Template variety**: three structurally different first emails
  (`initial.j2`, `initial_b.j2`, `initial_c.j2`) selected by prospect id, all
  holding zero `check_style` violations. Eleven byte-identical fallback drafts
  in one batch was a bulk-mail fingerprint.
- **Display polish**: trades render in prose casing (HVAC, plumbing,
  electrical), subject lines get title-cased company names, and missing spaces
  after commas in legal names are normalised.


## 2.3.1 (2026-08-07)

**The From header joins the placeholder gate.** v2.3.0 validated the campaign
profile but the From header is built from the email identity
(saved connection, then .env FROM_NAME / FROM_EMAIL), which the gate never
looked at: a fully filled campaign could still ship every email as
"Your Name". Now:

- `assert_campaign_ready()` also checks the raw configured identity against
  the placeholder markers, reporting `from_name` / `from_email` with an error
  that points at .env or the saved email connection rather than
  /admin/campaign. An empty FROM_EMAIL is checked as the "dryrun@localhost"
  the message builder would stamp on it.
- When FROM_NAME is unset or still the "Your Name" default, `resolve_email()`
  falls back to the campaign's sender_name instead of the placeholder string.
  The gate still checks the raw value, so the fallback fixes DRY_RUN artifacts
  without letting an unset identity slip into SANDBOX or LIVE.


## 2.3.0 (2026-08-02)

**A quality gate release: no new modules, seven focused fixes.** The trigger
was a DRY_RUN QA pass that found a 58 word run-on with one full stop queued in
the outbox. It had passed every existing check, because none of them asked
whether the text could actually be read.

- **Readability rules in `check_style()`**: longest unpunctuated run capped at
  40 words, sentences capped at 32, at least 4 sentences in at least 3
  paragraphs, every prose line ends like a sentence (URLs may close a line),
  no 5 word phrase repeated, exactly one link in the first email. Violations
  feed the existing LLM retry loop, so the model is told exactly what it broke.
- **Placeholder campaign is now a hard block**: `placeholder_fields` names
  every campaign field still carrying an example value (an empty CAN-SPAM
  postal address counts). DRY_RUN warns once and continues; SANDBOX and LIVE
  raise `CampaignNotReady` at draft time, at send time before any SMTP
  connection, and in `/golive`. Emailing `example.com/demo` to a real prospect
  is no longer possible.
- **Per-prospect subject lines**: five subject patterns rendered from the
  prospect, selected deterministically by id, so runs are reproducible and no
  two prospects share a byte-identical subject. New subject rules: must carry
  the company name or city, 8 words or fewer, no trailing full stop.
- **Owner-name coverage is visible**: `/report` and `cli status` show how many
  prospects awaiting a draft have a discovered owner name, and each drafting
  run logs the coverage. Optional `REQUIRE_OWNER_NAME=true` skips the
  "Hi there" fallback entirely, leaving those prospects VERIFIED.
- **The sender's name appears once**: `footer.j2` no longer repeats
  name | company | website below the signature. Unsubscribe line and postal
  address stay, that part is CAN-SPAM, not style.
- **`purge-drafts`** (CLI, `--with-outbox --yes`) and **`/purgedrafts`**
  (Telegram, two-step confirm): throw away every DRAFT touch and reset its
  prospect to VERIFIED so the queue regenerates cleanly after a writer change.
  QUEUED and SENT are never touched.
- Fixed `resolve_email()` mixing accounts: a saved connection's blank IMAP
  fields now fall back to that connection's own SMTP credentials before any
  env value.


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
