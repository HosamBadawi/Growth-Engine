"""Module 5 — Writer: personalized cold emails via Ollama.

Strict JSON output {subject, body}, style rules enforced in code with retry
feedback; deterministic Jinja2 template fallback if the model cannot comply.
"""
import logging
import re
from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader
from sqlalchemy.orm import Session

from db.models import Prospect, Touch, TouchStatus, TouchType, ProspectStatus
from engine.config import get_settings
from engine.events import log_event
from engine.llm import LLMError, llm_chat_json
from engine.researcher import build_intel_card

log = logging.getLogger("writer")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates_email"
# UI edits save to data/templates/ (gitignored) which shadows the repo defaults,
# so templates_email/ stays pristine and upgrades never clobber user edits.
OVERRIDE_DIR = Path("data/templates")
_env = Environment(
    loader=ChoiceLoader([FileSystemLoader(str(OVERRIDE_DIR)),
                         FileSystemLoader(str(TEMPLATE_DIR))]),
    autoescape=False,
)


def reload_templates() -> None:
    """Call after the admin saves a template override."""
    _env.cache.clear()


def read_template_source(name: str) -> tuple[str, bool]:
    """Return (source, is_override) for the admin editor."""
    override = OVERRIDE_DIR / name
    if override.exists():
        return override.read_text(encoding="utf-8"), True
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8"), False


def save_template_override(name: str, source: str) -> None:
    if name not in set(SEQUENCE_TEMPLATES.values()) | {"footer.j2"}:
        raise ValueError(f"Unknown template '{name}'")
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    (OVERRIDE_DIR / name).write_text(source, encoding="utf-8")
    reload_templates()


def reset_template_override(name: str) -> bool:
    override = OVERRIDE_DIR / name
    if override.exists():
        override.unlink()
        reload_templates()
        return True
    return False

SEQUENCE_TEMPLATES = {
    TouchType.EMAIL_1: "initial.j2",
    TouchType.FOLLOWUP_2: "followup_day3.j2",
    TouchType.FOLLOWUP_3: "followup_day6.j2",
    TouchType.BREAKUP: "breakup_day10.j2",
}

GENERIC_NAME_WORDS = {
    "hvac", "plumbing", "plumber", "plumbers", "heating", "cooling", "air",
    "electric", "electrical", "electrician", "service", "services", "company",
    "llc", "corp", "inc", "home", "repair", "repairs", "pros", "group",
    "solutions", "comfort", "mechanical", "conditioning", "contractors",
}

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️]"
)


def sanitize_detail(text: str) -> str:
    """Make an LLM-produced snippet safe for the no-dash templates."""
    text = (text or "").strip().rstrip(".")
    text = text.replace("—", ",").replace("–", ",").replace(" - ", ", ").replace("--", ",")
    return text


def template_context(prospect: Prospect, card: dict | None = None) -> dict:
    from engine.campaign import get_campaign

    campaign = get_campaign()
    first_name = prospect.owner_name.split()[0] if prospect.owner_name else "there"
    job_value = campaign.job_value_for(prospect.trade)
    weekly_loss = campaign.missed_calls_per_week * job_value
    detail = ""
    if card:
        detail = sanitize_detail(card.get("human_detail") or "")
        if len(detail) > 80:
            detail = ""  # too sentence-like for the template slot, use deterministic fallback
        elif detail:
            detail = detail[0].lower() + detail[1:]
    if not detail:
        intel = prospect.intel_json or {}
        if intel.get("years_in_business"):
            detail = f"being in business {intel['years_in_business']}"
        elif prospect.rating and prospect.review_count:
            detail = f"your {prospect.rating} star rating from {prospect.review_count} customers"
        else:
            detail = "your local reputation"
    return {
        "first_name": first_name,
        "company": prospect.name,
        "city": (prospect.city or "your city").title(),
        "trade": prospect.trade or "home service",
        "personal_detail": detail,
        "job_value": job_value,
        "missed_calls_week": campaign.missed_calls_per_week,
        "weekly_loss": weekly_loss,
        "yearly_loss": weekly_loss * 52,
        "demo_url": campaign.demo_url,
        "calendly_url": campaign.calendar_url,
        # campaign identity (templates render pitch/signature from these)
        "product_pitch": campaign.product_pitch,
        "signature": campaign.signature,
        "sender_name": campaign.sender_name,
        "sender_company": campaign.company,
    }


def render_email_template(
    template_name: str, prospect: Prospect, card: dict | None = None
) -> tuple[str, str]:
    """Render a sequence template; first line 'SUBJECT: ...' becomes the subject."""
    text = _env.get_template(template_name).render(**template_context(prospect, card)).strip()
    first, _, rest = text.partition("\n")
    if first.upper().startswith("SUBJECT:"):
        return first.split(":", 1)[1].strip(), rest.strip()
    return "", text


def render_footer() -> str:
    """CAN-SPAM footer. Email-connection identity wins (per sending account),
    campaign fills the gaps, env is the last fallback."""
    from engine.campaign import get_campaign
    from engine.connections import resolve_email

    campaign = get_campaign()
    email_cfg = resolve_email()
    return _env.get_template("footer.j2").render(
        unsubscribe_line=campaign.unsubscribe_line,
        from_name=email_cfg.from_name or campaign.sender_name,
        company=campaign.company,
        website=campaign.website,
        postal_address=email_cfg.postal_address or campaign.postal_address,
    ).strip()


def _personal_tokens(prospect: Prospect) -> set[str]:
    tokens = set()
    if prospect.owner_name:
        tokens.add(prospect.owner_name.split()[0].lower())
    if prospect.city:
        tokens.add(prospect.city.lower())
    if prospect.name:
        tokens.add(prospect.name.lower())
        for word in re.findall(r"[a-z]+", prospect.name.lower()):
            if len(word) > 3 and word not in GENERIC_NAME_WORDS:
                tokens.add(word)
    return tokens


def check_style(subject: str, body: str, prospect: Prospect,
                touch_type: str = TouchType.EMAIL_1) -> list[str]:
    """Return list of style violations (empty = compliant).

    Link discipline is touch-type aware: the first cold email carries the demo
    link ONLY (a calendar link from a stranger raises spam score, and email 1's
    only job is the reply); follow-ups may carry the calendar link.
    """
    from engine.campaign import get_campaign

    settings = get_settings()
    campaign = get_campaign()
    violations = []
    words = len(body.split())
    if words > 150:
        violations.append(f"body is {words} words, must be under 150")
    text = f"{subject}\n{body}"
    if re.search(r"[—–]", text) or " - " in text or "--" in text:
        violations.append(
            "contains a dash (em dash, en dash, double hyphen or spaced hyphen); "
            "write 'to' or use commas instead"
        )
    if settings.strict_no_hyphens and "-" in text:
        violations.append("contains a hyphen (STRICT_NO_HYPHENS is on)")
    if EMOJI_RE.search(text):
        violations.append("contains an emoji")
    if campaign.demo_url not in body:
        violations.append(f"missing the demo link {campaign.demo_url}")
    if touch_type == TouchType.EMAIL_1:
        if campaign.calendar_url and campaign.calendar_url in body:
            violations.append(
                "the first email must NOT contain the calendar link, only the demo link"
            )
    elif campaign.calendar_url and campaign.calendar_url not in body:
        violations.append(f"missing the calendar link {campaign.calendar_url}")
    if "?" not in body:
        violations.append("missing a question close")
    first_lines = " ".join(body.strip().splitlines()[:3]).lower()
    if not any(tok in first_lines for tok in _personal_tokens(prospect)):
        violations.append(
            "the first line must reference THEIR specific detail "
            "(company name, owner first name or city)"
        )
    if not body.strip().lower().startswith("hi "):
        greeting = prospect.owner_name.split()[0] if prospect.owner_name else "there"
        violations.append(f'must open with the greeting "Hi {greeting}," on its own line')
    return violations


def writer_system_prompt() -> str:
    """Brand-free system prompt rendered from the active campaign profile."""
    from engine.campaign import get_campaign

    campaign = get_campaign()
    return f"""You write short cold emails for {campaign.sender_name}, \
{campaign.sender_role} of {campaign.company}.
{campaign.company} sells {campaign.product_pitch} to {campaign.target_niche}.

Hard style rules, every one is checked by a machine:
- Under 150 words total in the body.
- The FIRST line must reference the prospect's specific detail from the intel.
- Include exactly one concrete money pain (a job value walking away).
- Include the demo link exactly as given. Do NOT include any calendar or
  booking link, this first email's only job is to get a reply.
- End with one short question.
- NEVER use any dash character (no -, --, em dash or en dash). Write "to" or use commas.
- No emojis. Plain, human, conversational American English. No hype words.
- Sign as: {campaign.signature}
Reply with ONLY a JSON object: {{"subject": "...", "body": "..."}}. The body is
plain text with real line breaks. Do not copy the template verbatim, adapt it
to this specific prospect."""


async def generate_draft(session: Session, prospect: Prospect) -> Touch:
    """Generate (or fall back to template) an EMAIL_1 draft for a prospect."""
    settings = get_settings()
    card = await build_intel_card(session, prospect)
    tpl_subject, tpl_body = render_email_template("initial.j2", prospect, card)

    from engine.campaign import get_campaign

    campaign = get_campaign()
    base_user = (
        f"Prospect:\n"
        f"- company: {prospect.name}\n"
        f"- trade: {prospect.trade}\n"
        f"- city: {(prospect.city or '').title()}, {prospect.state or ''}\n"
        f"- owner first name: {prospect.owner_name or 'unknown, open with Hi there'}\n"
        f"- intel card: {card}\n"
        f"- average job value: ${campaign.job_value_for(prospect.trade)}\n"
        f"- demo link: {campaign.demo_url}\n\n"
        f"Proven template to adapt (do not copy verbatim):\n---\n{tpl_body}\n---\n"
        f'Subject line options to adapt: "quick question about your after-hours calls" '
        f'or "missed call at {prospect.name}?"'
    )

    subject, body, source = tpl_subject, tpl_body, "template"
    system_prompt = writer_system_prompt()
    user = base_user
    try:
        for attempt in range(1, 4):
            data = await llm_chat_json(
                "writer", system_prompt, user,
                required_keys=["subject", "body"], max_attempts=2, temperature=0.6,
            )
            cand_subject = str(data["subject"]).strip()
            cand_body = str(data["body"]).strip()
            violations = check_style(cand_subject, cand_body, prospect)
            if not violations:
                subject, body, source = cand_subject, cand_body, "llm"
                break
            log.info("Draft for %s attempt %d violations: %s", prospect.name, attempt, violations)
            user = (
                base_user
                + "\n\nYour previous draft broke these rules, fix ALL of them and regenerate:\n- "
                + "\n- ".join(violations)
            )
    except LLMError as exc:
        log_event(session, "writer",
                  f"{prospect.name}: LLM failed ({exc}), using template fallback",
                  level="WARNING")

    touch = Touch(
        prospect_id=prospect.id,
        type=TouchType.EMAIL_1,
        subject=subject,
        body=body,
        status=TouchStatus.DRAFT,
        meta_json={"source": source},
    )
    session.add(touch)
    prospect.status = ProspectStatus.DRAFTED
    session.commit()
    log_event(session, "writer",
              f"Draft ready for {prospect.name} ({source}, {len(body.split())} words)")
    return touch
