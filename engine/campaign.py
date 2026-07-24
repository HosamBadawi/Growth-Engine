"""Campaign profile: WHO is sending, WHAT they sell, and the offer numbers.

This is the genericization backbone — the public repo ships neutral example
values; the operator's real campaign lives in the local DB (Admin > Campaign)
with .env as the bootstrap fallback. Everything that used to be hard-coded
brand text (writer prompt, templates, signatures) renders from here.
"""
import logging
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from db.models import Connection, ConnectionKind
from engine.config import get_settings
from engine.connections import get_active

log = logging.getLogger("campaign")

EXAMPLE_MARKER = "Example Co"  # presence of this anywhere = placeholder campaign


@dataclass
class Campaign:
    company: str
    product_pitch: str      # 1-2 sentences: what the product does for the prospect
    target_niche: str       # e.g. "US home-service businesses"
    sender_name: str
    sender_role: str        # e.g. "founder"
    website: str
    demo_url: str
    calendar_url: str
    signature: str          # e.g. "Jane Doe, Example Co | example.com"
    unsubscribe_line: str
    postal_address: str
    job_value_by_trade: str
    default_job_value: int
    missed_calls_per_week: int
    source: str = "env"

    @property
    def is_placeholder(self) -> bool:
        return EXAMPLE_MARKER.lower() in f"{self.company} {self.signature}".lower()

    def job_value_for(self, trade: str | None) -> int:
        if trade:
            key = trade.strip().lower()
            for pair in (self.job_value_by_trade or "").split(","):
                name, _, value = pair.partition(":")
                if name.strip().lower() == key and value.strip().isdigit():
                    return int(value.strip())
        return self.default_job_value


def _env_campaign() -> Campaign:
    settings = get_settings()
    company = settings.company_name
    sender = settings.from_name
    return Campaign(
        company=company,
        product_pitch=settings.product_pitch,
        target_niche=settings.target_niche,
        sender_name=sender,
        sender_role=settings.sender_role,
        website=settings.company_website,
        demo_url=settings.demo_url,
        calendar_url=settings.calendly_url,
        signature=f"{sender}, {company} | {settings.company_website}".strip(" |,"),
        unsubscribe_line=settings.unsubscribe_line,
        postal_address=settings.postal_address,
        job_value_by_trade=settings.job_value_by_trade,
        default_job_value=settings.default_job_value,
        missed_calls_per_week=settings.missed_calls_per_week,
        source="env",
    )


def get_campaign(session: Session | None = None) -> Campaign:
    """Active CAMPAIGN connection wins; .env values are the fallback."""
    own = session is None
    if own:
        from db.session import new_session

        session = new_session()
    try:
        conn = get_active(session, ConnectionKind.CAMPAIGN)
    finally:
        if own:
            session.close()
    base = _env_campaign()
    if not conn:
        return base
    cfg = conn.config_json or {}
    data = asdict(base)
    for key in data:
        if key == "source":
            continue
        value = cfg.get(key)
        if value not in (None, ""):
            data[key] = type(data[key])(value)
    data["source"] = f"connection:{conn.name}"
    return Campaign(**data)


CAMPAIGN_FIELDS = [
    ("company", "Company name"),
    ("product_pitch", "Product pitch (1-2 sentences, what it does for them)"),
    ("target_niche", "Target niche"),
    ("sender_name", "Sender name"),
    ("sender_role", "Sender role (founder, owner...)"),
    ("website", "Website"),
    ("demo_url", "Demo URL"),
    ("calendar_url", "Calendar URL"),
    ("signature", "Signature line"),
    ("unsubscribe_line", "Unsubscribe line"),
    ("postal_address", "Postal address (CAN-SPAM)"),
    ("job_value_by_trade", "Job value by trade (trade:dollars, comma separated)"),
    ("default_job_value", "Default job value ($)"),
    ("missed_calls_per_week", "Missed calls per week (ROI math)"),
]
