"""Module 5, Writer: personalized cold emails via Ollama.

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
    if name not in EDITABLE_TEMPLATES:
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

# v2.4: three structurally different first emails, selected deterministically,
# because eleven byte-identical template fallbacks in one batch is a bulk-mail
# fingerprint that filters cluster on.
INITIAL_TEMPLATES = ["initial.j2", "initial_b.j2", "initial_c.j2"]

EDITABLE_TEMPLATES = (list(SEQUENCE_TEMPLATES.values())
                      + ["initial_b.j2", "initial_c.j2", "footer.j2"])


def initial_template_for(prospect: Prospect) -> str:
    return INITIAL_TEMPLATES[(prospect.id or 0) % len(INITIAL_TEMPLATES)]


# Trades are stored lowercase for matching; prose needs the display form.
# "researching Tampa hvac companies" reads like a bot wrote it.
TRADE_DISPLAY = {
    "hvac": "HVAC", "plumber": "plumbing", "plumbing": "plumbing",
    "electrician": "electrical", "electrical": "electrical",
    "roofer": "roofing", "roofing": "roofing", "landscaper": "landscaping",
}


def _clean_company(name: str) -> str:
    """Normalise missing spaces after commas: 'Southland Services,llc'."""
    return re.sub(r",(?=\S)", ", ", (name or "").strip())


_COMPANY_SUFFIXES = {"llc": "LLC", "inc": "Inc", "co": "Co", "corp": "Corp",
                     "ltd": "Ltd", "pa": "PA", "pc": "PC"}


def _subject_company(name: str) -> str:
    """Title-cased company for the subject line only; the body keeps the
    legal name. Existing capitals (HVAC, McDonald) are left alone, legal
    suffixes get their conventional casing."""
    words = []
    for word in _clean_company(name).split():
        key = word.strip(".,").lower()
        if key in _COMPANY_SUFFIXES:
            words.append(word.replace(word.strip(".,"), _COMPANY_SUFFIXES[key]))
        elif word.islower():
            words.append(word.capitalize())
        else:
            words.append(word)
    return " ".join(words)

GENERIC_NAME_WORDS = {
    "hvac", "plumbing", "plumber", "plumbers", "heating", "cooling", "air",
    "electric", "electrical", "electrician", "service", "services", "company",
    "llc", "corp", "inc", "home", "repair", "repairs", "pros", "group",
    "solutions", "comfort", "mechanical", "conditioning", "contractors",
}

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️]"
)

URL_RE = re.compile(r"https?://\S+")

# Rendered against template_context(), selected by prospect.id so the same
# prospect always gets the same subject and runs stay reproducible. Every
# pattern must contain {company}: v2.5 dropped "{city} {trade} question" after
# it produced the same subject for three prospects in one 16-draft batch.
SUBJECT_PATTERNS = [
    "missed call at {company}?",
    "quick question for {company}",
    "after hours calls at {company}",
    "{company}, one question",
]

GREETING_RE = re.compile(r"^Hi ([A-Z][a-z'’-]{1,20}|there),$")
_FIRST_NAME_RE = re.compile(r"[A-Z][a-z'’-]{1,20}$")

# The tell of a mail merge: compliments that carry no information. A body
# containing any of these is rejected; the templates state a fact or nothing.
BANNED_PHRASES = (
    "local reputation", "something worth protecting", "something valuable",
    "you clearly run", "solid operation", "respect for what you've built",
    "built something",
)

# Acronyms that must keep their casing anywhere they appear in prose.
_CASING_FIXES = ((re.compile(r"\bhvac\b", re.I), "HVAC"),
                 (re.compile(r"\ba/c\b", re.I), "A/C"))

_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
_MONEY_DECIMALS_RE = re.compile(r"\$\d[\d,]*\.\d+")
# "24/7" and the "60 second demo" are the product's own constants; every other
# number in a body must be a campaign number or a prospect fact.
_PRODUCT_NUMBERS = {24.0, 7.0, 60.0}


def _to_float(token: str) -> float | None:
    try:
        return float(token.lstrip("$").replace(",", ""))
    except ValueError:
        return None


def _allowed_numbers(prospect: Prospect, campaign) -> set[float]:
    """Every number a draft may legitimately contain: the campaign's ROI math,
    the product constants, and the prospect's own facts (rating, reviews, and
    the numbers in an intel card that v2.4's grounding already verified)."""
    job_value = campaign.job_value_for(prospect.trade)
    weekly = campaign.missed_calls_per_week * job_value
    allowed = {float(campaign.missed_calls_per_week), float(job_value),
               float(weekly), float(weekly * 52)} | set(_PRODUCT_NUMBERS)
    intel = prospect.intel_json or {}
    card = intel.get("card") or {}
    sources = " ".join([
        str(card.get("human_detail") or ""),
        str(card.get("pain_signal") or ""),
        " ".join(str(b) for b in (card.get("bullets") or [])),
        str(intel.get("years_in_business") or ""),
        str(prospect.rating or ""), str(prospect.review_count or ""),
        prospect.name or "", campaign.product_pitch or "",
    ])
    for token in _NUMBER_RE.findall(sources):
        value = _to_float(token)
        if value is not None:
            allowed.add(value)
    return allowed


def _fix_prose_casing(text: str) -> str:
    for pattern, canonical in _CASING_FIXES:
        text = pattern.sub(canonical, text)
    return text


def sanitize_detail(text: str) -> str:
    """Make an LLM-produced snippet safe for the no-dash templates, with
    prose casing applied (scraped text arrives as 'quality hvac systems')."""
    text = (text or "").strip().rstrip(".")
    text = text.replace("—", ",").replace("–", ",").replace(" - ", ", ").replace("--", ",")
    return _fix_prose_casing(text)


def campaign_for(prospect: Prospect):
    """The offer that fits this prospect.

    A business with no website is not a missed-call prospect, it is a
    website-build prospect. If the operator has configured a campaign for the
    'no_website' segment it is used here; otherwise the default campaign is,
    so single-offer setups behave exactly as before.
    """
    from engine.campaign import campaign_for_segment
    from engine.stages import SEGMENT_NO_WEBSITE, in_segment

    segment = SEGMENT_NO_WEBSITE if in_segment(prospect, SEGMENT_NO_WEBSITE) else ""
    return campaign_for_segment(segment)


def greeting_first_name(prospect: Prospect) -> str:
    """The first name the greeting may safely use.

    'Hi there' is fine; 'Hi Kenneth' to someone called Lindsay is not. When the
    email local part looks personal and shares nothing with the owner name
    (fgolden@... vs 'Lindsay W Byers'), the mailbox belongs to someone else,
    so the greeting falls back rather than asserting a wrong name."""
    owner = (prospect.owner_name or "").strip()
    if not owner:
        return "there"
    local = (prospect.email or "").split("@")[0].lower()
    local_alpha = re.sub(r"[^a-z]", "", local)
    if local_alpha and local not in _ROLE_LOCALS:
        parts = [p for p in re.findall(r"[a-z]+", owner.lower()) if len(p) > 1]
        variants = set(parts)
        if parts:
            variants.add(parts[0][0] + parts[-1])   # lbyers
            variants.add(parts[0] + parts[-1][0])   # lindsayb
        overlap = any(v in local_alpha for v in variants) or (
            # ken@ belongs to Kenneth: a short local that prefixes a name part.
            len(local_alpha) >= 3
            and any(p.startswith(local_alpha) for p in parts))
        if not overlap:
            return "there"
    first = owner.split()[0]
    # The greeting gate demands a clean single capitalized word; a name the
    # regex cannot bless (ALLCAPS scrape, initials, stray digits) falls back.
    return first if _FIRST_NAME_RE.fullmatch(first) else "there"


_ROLE_LOCALS = {
    "info", "office", "contact", "admin", "sales", "support", "hello",
    "service", "team", "mail", "help", "dispatch", "scheduling",
}


def template_context(prospect: Prospect, card: dict | None = None) -> dict:
    campaign = campaign_for(prospect)
    first_name = greeting_first_name(prospect)
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
        # v2.5: no more "your local reputation". When nothing checkable exists
        # the detail stays empty and the templates omit the sentence entirely;
        # one less sentence beats an empty compliment.
    detail = _fix_prose_casing(detail)
    raw_trade = (prospect.trade or "home service").strip().lower()
    context = {
        "first_name": first_name,
        "company": _clean_company(prospect.name),
        "city": (prospect.city or "your city").title(),
        "trade": TRADE_DISPLAY.get(raw_trade, raw_trade),
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
    # Deterministic per-prospect subject: same prospect, same subject, always.
    # The subject gets the title-cased company; the body keeps the legal name.
    subject_context = {**context, "company": _subject_company(prospect.name)}
    subject = SUBJECT_PATTERNS[
        (prospect.id or 0) % len(SUBJECT_PATTERNS)].format(**subject_context)
    if len(subject.split()) > 8:
        # A five word legal name overflows the longer patterns; the shortest
        # pattern keeps the subject inside its own 8 word rule.
        subject = "{company}, one question".format(**subject_context)
    context["subject_line"] = subject
    return context


def render_email_template(
    template_name: str, prospect: Prospect, card: dict | None = None
) -> tuple[str, str]:
    """Render a sequence template; first line 'SUBJECT: ...' becomes the subject.

    A campaign may set template_dir to keep its own copies under
    data/templates/<dir>/, so two offers never overwrite each other's wording.
    """
    campaign = campaign_for(prospect)
    name = template_name
    if campaign.template_dir:
        scoped = f"{campaign.template_dir.strip('/')}/{template_name}"
        if (OVERRIDE_DIR / scoped).exists():
            name = scoped
    text = _env.get_template(name).render(**template_context(prospect, card)).strip()
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


def _company_tokens(prospect: Prospect) -> set[str]:
    """Tokens distinctive to THIS company's name (plus the full name itself)."""
    tokens = set()
    if prospect.name:
        tokens.add(_clean_company(prospect.name).lower())
        for word in re.findall(r"[a-z]+", prospect.name.lower()):
            if len(word) > 3 and word not in GENERIC_NAME_WORDS:
                tokens.add(word)
    return tokens


def _personal_tokens(prospect: Prospect) -> set[str]:
    tokens = _company_tokens(prospect)
    if prospect.owner_name:
        tokens.add(prospect.owner_name.split()[0].lower())
    if prospect.city:
        tokens.add(prospect.city.lower())
    return tokens


def _strip_for_readability(body: str, signature: str = "") -> str:
    """The body minus greeting line and signature block, so readability rules
    judge only the prose. A signature without a full stop is not a style crime."""
    lines = body.strip().splitlines()
    if lines and lines[0].strip().lower().startswith("hi"):
        lines = lines[1:]
    found_signature = False
    signature_first = (signature or "").strip().splitlines()
    signature_first = signature_first[0].strip().lower() if signature_first else ""
    if signature_first:
        for index, line in enumerate(lines):
            if line.strip().lower() == signature_first:
                lines, found_signature = lines[:index], True
                break
    text = "\n".join(lines).strip()
    if not found_signature:
        # No exact signature match (edited drafts, test bodies): treat the last
        # blank-line-separated block as the sign-off, unless it is all there is.
        blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
        if len(blocks) >= 2:
            text = "\n\n".join(blocks[:-1]).strip()
    return text


def _readability_violations(body: str, signature: str = "") -> list[str]:
    """Rules that catch an unreadable draft, not just a rule-breaking one.

    The v2.3 trigger: a 58 word run-on with no punctuation sailed through every
    existing check because none of them asked whether the text could be read.
    """
    violations = []
    stripped = _strip_for_readability(body, signature)
    if not stripped:
        return ["body has no prose beyond greeting and signature"]
    # URLs carry dots that are not sentence breaks; analyse with them masked.
    prose = URL_RE.sub("thelink", stripped)

    # 1. Longest span of words with no sentence break at all.
    longest = max((len(seg.split()) for seg in re.split(r"[.?!:\n]", prose)),
                  default=0)
    if longest > 40:
        violations.append(f"contains a {longest} word run with no sentence break")

    # 2 + 3. Sentence length and sentence count.
    sentences = [s.strip() for s in re.split(r"[.?!]+", prose)
                 if len(s.split()) >= 2]
    for sentence in sentences:
        count = len(sentence.split())
        if count > 32:
            head = " ".join(sentence.split()[:6])
            violations.append(f'sentence starting "{head}" is {count} words, '
                              f"keep every sentence under 32 words")
    if len(sentences) < 4:
        violations.append(f"body has only {len(sentences)} sentences, "
                          f"write at least 4 short ones")

    # 4. Every prose line ends like a sentence (a URL may close a line).
    for line in stripped.splitlines():
        text = line.strip()
        if not text or URL_RE.match(text.split()[-1]):
            continue
        if text[-1] not in ".?!:":
            head = " ".join(text.split()[:6])
            violations.append(f'line starting "{head}" must end with . ? ! or :')

    # 5. Paragraph structure.
    paragraphs = [b for b in re.split(r"\n\s*\n", stripped) if b.strip()]
    if len(paragraphs) < 3:
        violations.append(f"body has {len(paragraphs)} paragraphs, use at "
                          f"least 3 separated by blank lines")

    # 6. Repetition: the same 5 word phrase twice reads like a stuck record.
    words = re.findall(r"[a-z0-9$']+", prose.lower())
    seen: dict[str, int] = {}
    for index in range(len(words) - 4):
        gram = " ".join(words[index:index + 5])
        seen[gram] = seen.get(gram, 0) + 1
    repeated = next((g for g, c in seen.items() if c > 1), None)
    if repeated:
        violations.append(f'the phrase "{repeated}" appears more than once, '
                          f"vary the wording")
    return violations


def check_style(subject: str, body: str, prospect: Prospect,
                touch_type: str = TouchType.EMAIL_1) -> list[str]:
    """Return list of style violations (empty = compliant).

    Link discipline is touch-type aware: the first cold email carries the demo
    link ONLY (a calendar link from a stranger raises spam score, and email 1's
    only job is the reply); follow-ups may carry the calendar link.
    """
    settings = get_settings()
    campaign = campaign_for(prospect)
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
        violations.append(f'must open with the greeting '
                          f'"Hi {greeting_first_name(prospect)}," on its own line')
    else:
        # The greeting is one first name and a comma, nothing else. A real run
        # opened with "Hi Ronald Mccrory at Anchor Construction Of Tampa in
        # Tampa, FL," because the model pasted the whole prospect record.
        first_line = body.strip().splitlines()[0].rstrip()
        if not GREETING_RE.fullmatch(first_line):
            violations.append(
                f'the greeting line "{first_line}" must be exactly '
                f'"Hi {greeting_first_name(prospect)}," with nothing added')

    lowered_body = body.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered_body:
            violations.append(f'contains the empty flattery phrase "{phrase}"; '
                              f"state a fact about their business or say nothing")

    # Prose casing: hvac and a/c must be capitalized wherever they appear.
    for pattern, canonical in _CASING_FIXES:
        wrong = next((m.group(0) for m in pattern.finditer(body)
                      if m.group(0) != canonical), None)
        if wrong:
            violations.append(f'write "{canonical}", not "{wrong}"')

    # v2.5.1: the writer invented "50 calls a week mean missing out on
    # $2500.00" when the campaign says 5 calls at $500. Every number in the
    # body must be one the campaign or the prospect actually owns. URLs and
    # the signature (which may carry a phone number) are exempt.
    number_prose = URL_RE.sub(" ", _strip_for_readability(body, campaign.signature))
    for match in _MONEY_DECIMALS_RE.finditer(number_prose):
        value = _to_float(match.group(0))
        pretty = f"${int(round(value)):,}" if value is not None else match.group(0)
        violations.append(f'money must not carry decimals: write "{pretty}", '
                          f'not "{match.group(0)}"')
    allowed_numbers = _allowed_numbers(prospect, campaign)
    ungrounded = []
    for token in _NUMBER_RE.findall(number_prose):
        value = _to_float(token)
        if value is not None and value not in allowed_numbers \
                and token not in ungrounded:
            ungrounded.append(token)
    if ungrounded:
        violations.append(
            "numbers that are not campaign or prospect facts: "
            + ", ".join(f'"{t}"' for t in ungrounded)
            + "; use only the numbers you were given, or none")

    # Draft #34 shipped ending on "Should I send you the demo?" with no
    # signature at all. The last non-empty line must be the signature.
    signature_line = (campaign.signature or "").strip().splitlines()
    signature_line = signature_line[0].strip() if signature_line else ""
    if signature_line:
        non_empty = [line.strip() for line in body.splitlines() if line.strip()]
        last = non_empty[-1] if non_empty else ""
        sender = (campaign.sender_name or "").strip()
        if not (signature_line.lower() in last.lower()
                or (sender and sender.lower() in last.lower())):
            violations.append(
                f'must end with the signature "{signature_line}" as the last line')

    violations.extend(_readability_violations(body, campaign.signature))

    # Link count (the signature's own URL does not count against the body).
    url_count = len(URL_RE.findall(_strip_for_readability(body, campaign.signature)))
    if touch_type == TouchType.EMAIL_1:
        if url_count != 1:
            violations.append(f"the body must contain exactly one http link, "
                              f"the demo link, found {url_count}")
        # Subject rules apply to the first cold email; follow-ups thread as "re:".
        subject_words = len(subject.split())
        subject_lower = subject.lower()
        name_lower = _clean_company(prospect.name or "").lower()
        city_lower = (prospect.city or "").lower()
        if not ((name_lower and name_lower in subject_lower)
                or (city_lower and city_lower in subject_lower)):
            violations.append("the subject must contain the company name or the city")
        if subject_words > 8:
            violations.append(f"the subject is {subject_words} words, use 8 or fewer")
        if subject.rstrip().endswith("."):
            violations.append("the subject must not end with a full stop")
        # "Tampa HVAC question" went to three prospects in one batch: nothing
        # in it belonged to the business. Demand a distinctive company token.
        if not any(token in subject_lower for token in _company_tokens(prospect)):
            violations.append("the subject must contain a distinctive word "
                              "from the company name")
    elif url_count > 2:
        violations.append(f"the body must not carry more than two http links, "
                          f"found {url_count}")
    return violations


def writer_system_prompt(prospect: Prospect | None = None) -> str:
    """Brand-free system prompt rendered from the campaign that fits the prospect."""
    from engine.campaign import get_campaign

    campaign = campaign_for(prospect) if prospect is not None else get_campaign()
    return f"""You write short cold emails for {campaign.sender_name}, \
{campaign.sender_role} of {campaign.company}.
{campaign.company} sells {campaign.product_pitch} to {campaign.target_niche}.

Hard style rules, every one is checked by a machine:
- Under 150 words total in the body. Short sentences, real punctuation, at
  least 4 sentences in at least 3 paragraphs separated by blank lines.
- The first line is the greeting you are given, EXACTLY as given, one first
  name and a comma. Never add surnames, company names, cities or titles to it.
- State a fact about their business or say nothing. Never compliment a
  business you know nothing about; no empty flattery, no "local reputation".
- Write HVAC and A/C in capitals.
- The FIRST line must reference the prospect's specific detail from the intel.
- Include exactly one concrete money pain (a job value walking away).
- Include the demo link exactly as given. Do NOT include any calendar or
  booking link, this first email's only job is to get a reply.
- End with one short question.
- NEVER use any dash character (no -, --, em dash or en dash). Write "to" or use commas.
- No emojis. Plain, human, conversational American English. No hype words.
- Sign as: {campaign.signature} (this must be the last line of the body).
Reply with ONLY a JSON object: {{"body": "..."}}. The body is plain text with
real line breaks. Do not copy the template verbatim, adapt it to this specific
prospect. The subject line is handled separately, do not write one."""


def _mismatch_blocks(session: Session, prospect: Prospect) -> bool:
    """v2.5: a card that places the business in another state, or a business
    whose name/intel belongs to another trade, is never drafted. A discarded
    wrong detail leaves a hole the model fills with the campaign's own city,
    producing a false statement; the only safe draft is no draft."""
    from engine.researcher import geo_mismatch_reason
    from engine.targeting import trade_mismatch_reason

    intel = dict(prospect.intel_json or {})
    blocked = False
    changed = False
    for key, compute in (("geo_mismatch", geo_mismatch_reason),
                         ("trade_mismatch", trade_mismatch_reason)):
        reason = compute(prospect)
        if reason:
            if intel.get(key) != reason:
                intel[key] = reason
                changed = True
            log_event(session, "writer",
                      f"{prospect.name}: not drafting ({key}: {reason})",
                      level="WARNING")
            blocked = True
            break
        if intel.pop(key, None) is not None:
            # The rule (or the data) moved on: a stale flag must not keep a
            # legitimate prospect out of the campaign forever.
            changed = True
    if changed:
        prospect.intel_json = intel
        session.commit()
    return blocked


async def generate_draft(session: Session, prospect: Prospect) -> Touch | None:
    """Generate (or fall back to template) an EMAIL_1 draft for a prospect.

    Returns None (leaving the prospect VERIFIED) when REQUIRE_OWNER_NAME is on
    and no owner name was discovered."""
    from engine.campaign import assert_campaign_ready, get_campaign

    from engine.targeting import targeting_block_reason

    settings = get_settings()
    assert_campaign_ready(settings.engine_mode)
    if settings.require_owner_name and not prospect.owner_name:
        log.info("Skipping %s: no owner name and REQUIRE_OWNER_NAME is on",
                 prospect.name)
        return None
    # Backstop for the prospecting-time gate: enrichment can attach a foreign
    # site or email long after prospecting let the row through.
    block = targeting_block_reason(prospect.country, prospect.website,
                                   prospect.email, prospect.trade)
    if block:
        log_event(session, "writer",
                  f"{prospect.name}: not drafting, outside campaign target ({block})",
                  level="WARNING")
        return None
    if _mismatch_blocks(session, prospect):
        return None
    card = await build_intel_card(session, prospect)
    # The card build itself can discover the mismatch (fresh research naming
    # another state); check again before any prose is written.
    if _mismatch_blocks(session, prospect):
        return None
    template_name = initial_template_for(prospect)
    tpl_subject, tpl_body = render_email_template(template_name, prospect, card)

    campaign = get_campaign()
    greet = greeting_first_name(prospect)
    base_user = (
        f"Prospect:\n"
        f"- company: {prospect.name}\n"
        f"- trade: {prospect.trade}\n"
        f"- city: {(prospect.city or '').title()}, {prospect.state or ''}\n"
        f'- greeting line, use it EXACTLY as the first line: "Hi {greet},"\n'
        f"- intel card: {card}\n"
        f"- average job value: ${campaign.job_value_for(prospect.trade)}\n"
        f"- demo link: {campaign.demo_url}\n\n"
        f"Proven template to adapt (do not copy verbatim):\n---\n{tpl_body}\n---"
    )

    # The subject never comes from the model: the deterministic pattern is
    # already rendered into the template, and run data showed the model both
    # omitting the body (burning every retry) and collapsing subject variety.
    subject, body, source = tpl_subject, tpl_body, "template"
    system_prompt = writer_system_prompt(prospect)
    user = base_user
    try:
        for attempt in range(1, 4):
            data = await llm_chat_json(
                "writer", system_prompt, user,
                required_keys=["body"], max_attempts=2, temperature=0.35,
            )
            cand_body = str(data["body"]).strip()
            violations = check_style(subject, cand_body, prospect)
            if not violations:
                body, source = cand_body, "llm"
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
        meta_json={"source": source, "template": template_name},
    )
    session.add(touch)
    prospect.status = ProspectStatus.DRAFTED
    session.commit()
    log_event(session, "writer",
              f"Draft ready for {prospect.name} ({source}, {len(body.split())} words)")
    return touch
