"""Central configuration. Everything comes from .env; hard safety maxima live here in code."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard rails: these are code constants on purpose. Env values can only lower
# limits, never raise them above these.
HARD_WARMUP_SCHEDULE = ((7, 10), (14, 20))  # (through_day, max_per_day)
# Volume-based ramp too, so an idle/paused stretch cannot calendar-skip the
# warm-up: (below_total_real_sends, max_per_day)
HARD_WARMUP_VOLUME = ((70, 10), (210, 20))
HARD_MAX_DAILY = 30
HARD_JITTER_MIN_SECONDS = 180
# The send window is a hard rail as well: env values can only NARROW it.
HARD_SEND_WINDOW_START = "09:00"
HARD_SEND_WINDOW_END = "16:30"
HARD_SEND_TIMEZONE = "America/New_York"

ENGINE_MODES = ("DRY_RUN", "SANDBOX", "LIVE")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    engine_mode: str = "DRY_RUN"
    database_url: str = "sqlite:///data/growth.db"
    log_level: str = "INFO"

    ollama_base_url: str = "http://localhost:11434"
    # Cost guard for API-key LLM providers: max calls per provider per local
    # day; on breach the engine falls back to local Ollama for the rest of the
    # day (never silently burns money, never stops working).
    api_daily_call_cap: int = 300
    writer_model: str = "qwen2.5:7b-instruct"
    # Same model as writer on purpose: benchmarked on the RTX 3060 Ti (8GB),
    # qwen2.5:7b-instruct runs 66 tok/s fully on GPU while llama3.1:8b spills
    # to CPU (~14 tok/s), and one resident model avoids VRAM swap thrash.
    classifier_model: str = "qwen2.5:7b-instruct"
    researcher_model: str = ""

    telegram_bot_token: str = ""
    telegram_user_id: int = 0

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_name: str = "Your Name"
    from_email: str = ""
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    sandbox_recipient: str = ""

    daily_send_cap: int = 30
    send_window_start: str = "09:00"
    send_window_end: str = "16:30"
    send_timezone: str = "America/New_York"
    jitter_min_minutes: int = 3
    jitter_max_minutes: int = 12
    bounce_breaker_rate: float = 0.03
    bounce_breaker_window: int = 50

    unsubscribe_line: str = "Reply STOP or unsubscribe to never hear from me again."
    postal_address: str = "Example Co | your postal address here"

    prospect_provider: str = "registry"
    # True = a prospect with no findable website is dropped (what a cold-email
    # campaign wants). False = keep it as a 'no website' lead; those are never
    # auto-emailed unless a verified address is found. See README, Phase 4.
    require_website: bool = True
    # Google Places API (optional, off by default): the supported, ToS-compliant
    # way to get Google's business data. Requires a key; capped per day because
    # every call costs money at the ENTERPRISE SKU (see providers/places.py).
    places_api_key: str = ""
    places_daily_call_cap: int = 30
    registry_default_state: str = "FL"
    registry_discover_websites: bool = True
    gosom_binary: str = "bin/google-maps-scraper.exe"
    gosom_depth: int = 5
    gosom_timeout_seconds: int = 900
    gosom_extra_args: str = ""
    min_review_count: int = 10
    max_review_count: int = 200
    franchise_keywords: str = (
        "one hour heating,benjamin franklin plumbing,mr rooter,roto-rooter,"
        "mister sparky,aire serv,mr. electric,mr. handyman,ars rescue,"
        "service experts,horizon services,abc home,john moore"
    )

    # Campaign fallbacks (the real campaign lives in Admin > Campaign / the DB;
    # these env values bootstrap a fresh install with a clearly-fake example)
    company_name: str = "Example Co"
    product_pitch: str = (
        "an AI front desk that instantly texts back every missed call, answers "
        "the customer's questions, and books the job onto your calendar"
    )
    target_niche: str = "US home-service businesses"
    sender_role: str = "founder"
    company_website: str = "https://example.com"
    demo_url: str = "https://example.com/demo"
    calendly_url: str = "https://example.com/call"
    default_job_value: int = 400
    # Per-trade average job values: a flat number undersells HVAC badly and puts
    # an identical "$400" fingerprint in every email.
    job_value_by_trade: str = (
        "plumbing:500,plumber:500,hvac:700,electrician:450,electrical:450"
    )
    missed_calls_per_week: int = 5

    dashboard_password: str = "changeme"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    secret_key: str = "dev-secret-change-me"
    # Free Cloudflare Quick Tunnel: reach the dashboard from your phone without
    # port forwarding. Refuses to start while DASHBOARD_PASSWORD is the default.
    tunnel_enabled: bool = False
    cloudflared_binary: str = ""

    report_time: str = "20:00"
    report_timezone: str = "UTC"

    # Researcher: local (summarize scraped site text) or web (real deep search).
    research_mode: str = "local"
    research_max_pages: int = 3
    searxng_base_url: str = ""

    reply_poll_minutes: int = 3
    smtp_probe_enabled: bool = True
    # Set these to YOUR sending domain: some MX hosts judge probe legitimacy by them
    verifier_helo_domain: str = "example.com"
    verifier_probe_from: str = "verify@example.com"
    strict_no_hyphens: bool = False
    # True = draft only for prospects with a discovered owner name; the rest
    # stay VERIFIED instead of receiving a "Hi there" email.
    require_owner_name: bool = False

    # v2.4 targeting gate: a Brazilian restaurant must never receive a US
    # home-service pitch. Country checked against prospect.country and any
    # ccTLD on the website/email; trades checked against TARGET_TRADES.
    # Empty string disables the respective check (worldwide / any-niche runs).
    target_country: str = "US"
    target_trades: str = ""
    # Local parts that can never be a business owner's inbox. info/office/
    # service/contact/admin stay ALLOWED: for a four-truck business those are
    # often the owner's own mailbox.
    verifier_deny_localparts: str = (
        "noreply,no-reply,donotreply,careers,jobs,recruiting,catering,"
        "underwriting,billing,invoices,press,media,legal,abuse,postmaster,"
        "webmaster"
    )

    @property
    def target_trade_list(self) -> list[str]:
        return [t.strip().lower() for t in self.target_trades.split(",") if t.strip()]

    @property
    def effective_researcher_model(self) -> str:
        return self.researcher_model or self.classifier_model

    @property
    def effective_imap_user(self) -> str:
        return self.imap_user or self.smtp_user

    @property
    def effective_imap_password(self) -> str:
        return self.imap_password or self.smtp_password

    @property
    def franchise_keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in self.franchise_keywords.split(",") if k.strip()]

    def job_value_for(self, trade: str | None) -> int:
        """Average job value for a trade; falls back to DEFAULT_JOB_VALUE."""
        if trade:
            key = trade.strip().lower()
            for pair in self.job_value_by_trade.split(","):
                name, _, value = pair.partition(":")
                if name.strip().lower() == key and value.strip().isdigit():
                    return int(value.strip())
        return self.default_job_value


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
