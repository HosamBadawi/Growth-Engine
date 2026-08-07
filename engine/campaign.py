"""Campaign profile: WHO is sending, WHAT they sell, and the offer numbers.

This is the genericization backbone: the public repo ships neutral example
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

# Any of these inside a campaign field means the operator has not replaced the
# example value yet. Checked case-insensitively across the fields below.
PLACEHOLDER_MARKERS = ("example.com", "example co", "your name",
                       "your postal address here", "dryrun@localhost",
                       "yourdomain", "changeme", "your city")

# The fields a real campaign must have filled in before anything real happens.
CHECKED_FIELDS = ("company", "sender_name", "website", "demo_url",
                  "calendar_url", "signature", "postal_address", "product_pitch")


class CampaignNotReady(RuntimeError):
    """Raised in SANDBOX/LIVE when the campaign profile is still placeholder."""


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
    # Which prospect segment this offer is for ("" = the default campaign,
    # "no_website" = e.g. a web-design offer). Lets two offers coexist.
    segment: str = ""
    # Optional per-campaign template directory under data/templates/, so each
    # offer can have its own copy without touching the other's.
    template_dir: str = ""

    @property
    def placeholder_fields(self) -> list[str]:
        """Names of every field still carrying an example value.

        An empty postal address counts too: a missing CAN-SPAM address is as
        broken as a fake one, legally rather than stylistically."""
        fields = []
        for name in CHECKED_FIELDS:
            value = str(getattr(self, name) or "")
            if name == "postal_address" and not value.strip():
                fields.append(name)
                continue
            lowered = value.lower()
            if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
                fields.append(name)
        return fields

    @property
    def is_placeholder(self) -> bool:
        return bool(self.placeholder_fields)

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


def campaign_for_segment(segment: str, session: Session | None = None) -> Campaign:
    """Pick the campaign whose `segment` matches, else the active one.

    Two offers coexist: the missed-call offer for businesses that already have a
    site, and (for example) a web-design offer for the `no_website` segment.
    Neither offer is hardcoded anywhere: both are just campaign rows.
    """
    own = session is None
    if own:
        from db.session import new_session

        session = new_session()
    try:
        from sqlalchemy import select

        rows = session.execute(
            select(Connection).where(Connection.kind == ConnectionKind.CAMPAIGN)
        ).scalars().all()
        match = next((c for c in rows
                      if (c.config_json or {}).get("segment") == segment and segment), None)
        if match:
            return _merge(_env_campaign(), match)
        return get_campaign(session)
    finally:
        if own:
            session.close()


def _merge(base: Campaign, conn: Connection) -> Campaign:
    cfg = conn.config_json or {}
    data = asdict(base)
    for key in data:
        if key in ("source", "segment"):
            continue
        value = cfg.get(key)
        if value not in (None, ""):
            data[key] = type(data[key])(value)
    data["source"] = f"connection:{conn.name}"
    data["segment"] = cfg.get("segment", "")
    return Campaign(**data)


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
    return _merge(base, conn)


# Dedupe for the DRY_RUN warning: one line per distinct offending-field set,
# not one per drafted prospect.
_warned_fields: tuple = ()


def _identity_placeholder_fields() -> list[str]:
    """Placeholder checks on the RAW From identity (connection, then .env).

    Deliberately raw: resolve_email() heals an unset FROM_NAME from the
    campaign's sender_name so DRY_RUN artifacts look right, but the gate still
    demands the operator set the identity explicitly. An empty FROM_EMAIL is
    checked as the "dryrun@localhost" the message builder would stamp on it."""
    from engine.connections import raw_email_identity

    from_name, from_email = raw_email_identity()
    fields = []
    if not (from_name or "").strip() or any(
            marker in from_name.lower() for marker in PLACEHOLDER_MARKERS):
        fields.append("from_name")
    effective_from_email = ((from_email or "").strip() or "dryrun@localhost").lower()
    if any(marker in effective_from_email for marker in PLACEHOLDER_MARKERS):
        fields.append("from_email")
    return fields


def assert_campaign_ready(mode: str) -> None:
    """Raise in SANDBOX/LIVE while the campaign profile or the From identity
    is still placeholder.

    DRY_RUN warns and continues: the operator must be able to exercise the
    whole pipeline before the profile is filled in. The other two modes touch
    the real world, so there a placeholder anywhere is a hard stop."""
    global _warned_fields
    campaign_fields = get_campaign().placeholder_fields
    identity_fields = _identity_placeholder_fields()
    if not campaign_fields and not identity_fields:
        _warned_fields = ()
        return
    parts = []
    if campaign_fields:
        parts.append("campaign profile still has placeholder values in: "
                     + ", ".join(campaign_fields)
                     + " (fill them in at /admin/campaign)")
    if identity_fields:
        parts.append("sender identity still has placeholder values in: "
                     + ", ".join(identity_fields)
                     + " (set FROM_NAME / FROM_EMAIL in .env or in the saved "
                       "email connection)")
    message = "; ".join(parts)
    if (mode or "").upper() == "DRY_RUN":
        key = tuple(campaign_fields + identity_fields)
        if key != _warned_fields:
            _warned_fields = key
            log.warning("%s (fine for DRY_RUN, blocks SANDBOX and LIVE)", message)
        return
    raise CampaignNotReady(message + ". Nothing real goes out until this is fixed.")


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
    ("segment", "Segment this offer targets (blank = default, e.g. no_website)"),
    ("template_dir", "Template subfolder under data/templates (blank = shared)"),
]
