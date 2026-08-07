"""Module 3, Verifier: no email is queued without verification.

Pipeline: normalise -> reject what cannot be an owner's inbox (v2.4: malformed,
denylisted role, off-domain, placeholder) -> syntax (email-validator) -> MX
(dnspython) -> optional SMTP RCPT probe on port 25 with graceful degradation.
Levels: SYNTAX | MX | SMTP_OK | FAILED. FAILED is never sent to.

v2.4 exists because a real run queued `%20pat.cox@...` (URL-encoded space),
`support@constrafor.com` on a plumbing company, and `catering@southlandtrade.com`
as if they were owners. Every rejection now carries a machine-readable reason
so the verify rate in /report is explainable rather than just a number.
"""
import logging
import re
import smtplib
import socket
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

import dns.resolver
from email_validator import EmailNotValidError, validate_email
from sqlalchemy.orm import Session

from db.models import Prospect, ProspectStatus, VerificationLevel
from engine.config import get_settings
from engine.events import log_event

log = logging.getLogger("verifier")

ROLE_PREFIXES = {
    "info", "office", "contact", "admin", "sales", "support", "hello", "service",
    "billing", "team", "mail", "help", "postmaster", "abuse", "noreply", "no-reply",
    "webmaster", "hr", "dispatch",
}
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "yopmail.com",
    "temp-mail.org", "tempmail.com", "trashmail.com", "sharklasers.com",
    "getnada.com", "dispostable.com",
}

# Free providers a four-truck contractor legitimately uses; matched by the
# first label of the registrable domain so yahoo.co.uk counts as yahoo.
FREE_PROVIDER_NAMES = {"gmail", "yahoo", "outlook", "hotmail", "aol"}

# Country-code second-level registrations (foo.com.br, foo.co.uk).
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac"}


def registrable_domain(host: str) -> str:
    """somemail.constrafor.com -> constrafor.com; shop.example.com.br ->
    example.com.br; www. stripped. Good enough without a PSL dependency."""
    host = (host or "").strip().lower().removeprefix("www.").split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _deny_localparts() -> set[str]:
    raw = get_settings().verifier_deny_localparts
    return {re.sub(r"[._-]", "", part.strip().lower())
            for part in raw.split(",") if part.strip()}


def _is_denied_local(local: str) -> bool:
    return re.sub(r"[._-]", "", local) in _deny_localparts()


def _looks_placeholder(local: str) -> bool:
    """demo/test/example/sample/your local parts are scraped page furniture,
    never a person. `westchasefamilyplumbingdemo@gmail.com` was queued once."""
    if local.startswith("demo") or re.search(r"demo\d*$", local):
        return True
    for token in ("test", "example", "sample"):
        if re.fullmatch(rf"{token}\d*", local) \
                or re.search(rf"(?:^|[._-]){token}\d*(?:[._-]|$)", local):
            return True
    return local.startswith("your")


def _domain_mismatch(email_domain: str, website: str) -> str | None:
    """Reject an address whose domain is neither the prospect's own site nor a
    free provider. A plumbing company reachable at support@constrafor.com is a
    scraping error, not a lead."""
    site = (website or "").strip()
    if not site:
        return None  # nothing to compare against; other gates still apply
    if "//" not in site:
        site = "//" + site
    site_host = urlparse(site).netloc.split(":")[0]
    if not site_host:
        return None
    email_reg = registrable_domain(email_domain)
    site_reg = registrable_domain(site_host)
    if email_reg == site_reg:
        return None
    if email_reg.split(".")[0] in FREE_PROVIDER_NAMES:
        return None
    return f"email domain {email_reg} does not match website {site_reg}"


@dataclass
class VerificationResult:
    level: str
    detail: str = ""
    is_role: bool = False
    is_disposable: bool = False
    normalized: str = ""
    # v2.4: machine-readable category when FAILED ("malformed", "role_mailbox",
    # "domain_mismatch", "placeholder", "disposable", "syntax",
    # "domain_unresolvable", "smtp_rejected"). Empty when passing.
    reason: str = ""


def _resolve_mx(domain: str) -> list[str]:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 6.0
    resolver.timeout = 3.0
    answers = resolver.resolve(domain, "MX")
    hosts = sorted(answers, key=lambda r: r.preference)
    return [str(r.exchange).rstrip(".") for r in hosts]


def _has_a_record(domain: str) -> bool:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 6.0
    try:
        resolver.resolve(domain, "A")
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def port25_reachable() -> bool:
    """Probe once per process whether outbound port 25 works at all."""
    try:
        with socket.create_connection(("gmail-smtp-in.l.google.com", 25), timeout=6):
            return True
    except OSError:
        return False


def smtp_probe(email: str, mx_hosts: list[str]) -> tuple[str | None, str]:
    """RCPT probe. Returns (level or None if inconclusive, detail). Never sends DATA."""
    settings = get_settings()
    for host in mx_hosts[:2]:
        try:
            with smtplib.SMTP(host, 25, timeout=8) as smtp:
                smtp.ehlo(settings.verifier_helo_domain)
                smtp.mail(settings.verifier_probe_from)
                code, msg = smtp.rcpt(email)
                detail = f"{host} said {code} {msg[:120]!r}"
                if code in (250, 251):
                    return VerificationLevel.SMTP_OK, detail
                if code in (550, 551, 553, 554):
                    return VerificationLevel.FAILED, detail
                return None, detail  # 4xx greylist etc. -> inconclusive
        except (OSError, smtplib.SMTPException) as exc:
            log.debug("probe %s via %s inconclusive: %s", email, host, exc)
            continue
    return None, "probe inconclusive (timeout/blocked)"


def verify_email_address(email: str, allow_probe: bool = True,
                         website: str = "") -> VerificationResult:
    email = (email or "").strip()
    # Malformed before anything else: whitespace or percent-encoding inside an
    # address is a scraping bug, and `%20pat.cox@...` WILL hard bounce.
    if re.search(r"[\s%]", email):
        return VerificationResult(VerificationLevel.FAILED,
                                  "malformed (whitespace or percent-encoding)",
                                  reason="malformed")
    try:
        validated = validate_email(email, check_deliverability=False)
        normalized = validated.normalized.lower()
    except EmailNotValidError as exc:
        return VerificationResult(VerificationLevel.FAILED, f"syntax: {exc}",
                                  reason="syntax")

    local, domain = normalized.rsplit("@", 1)
    is_role = local in ROLE_PREFIXES
    is_disposable = domain in DISPOSABLE_DOMAINS
    if is_disposable:
        return VerificationResult(
            VerificationLevel.FAILED, "disposable domain", is_role, True,
            normalized, reason="disposable"
        )
    if _is_denied_local(local):
        return VerificationResult(
            VerificationLevel.FAILED,
            f"'{local}@' cannot be a business owner's inbox", is_role, False,
            normalized, reason="role_mailbox")
    if _looks_placeholder(local):
        return VerificationResult(
            VerificationLevel.FAILED, f"placeholder local part '{local}'",
            is_role, False, normalized, reason="placeholder")
    mismatch = _domain_mismatch(domain, website)
    if mismatch:
        return VerificationResult(VerificationLevel.FAILED, mismatch, is_role,
                                  False, normalized, reason="domain_mismatch")

    try:
        mx_hosts = _resolve_mx(domain)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return VerificationResult(VerificationLevel.FAILED, "domain does not resolve",
                                  is_role, False, normalized,
                                  reason="domain_unresolvable")
    except dns.resolver.NoAnswer:
        # RFC 5321 implicit MX: fall back to A record
        if _has_a_record(domain):
            return VerificationResult(VerificationLevel.SYNTAX, "no MX, A record exists",
                                      is_role, False, normalized)
        return VerificationResult(VerificationLevel.FAILED, "no MX and no A record",
                                  is_role, False, normalized,
                                  reason="domain_unresolvable")
    except Exception as exc:
        return VerificationResult(VerificationLevel.SYNTAX, f"DNS inconclusive: {exc}",
                                  is_role, False, normalized)

    level, detail = VerificationLevel.MX, f"MX: {mx_hosts[0]}"
    settings = get_settings()
    if allow_probe and settings.smtp_probe_enabled and port25_reachable():
        probe_level, probe_detail = smtp_probe(normalized, mx_hosts)
        if probe_level:
            level, detail = probe_level, probe_detail
        else:
            detail = f"MX ok; {probe_detail}"
    reason = "smtp_rejected" if level == VerificationLevel.FAILED else ""
    return VerificationResult(level, detail, is_role, False, normalized,
                              reason=reason)


def verify_prospect(session: Session, prospect: Prospect) -> VerificationResult | None:
    """Verify a prospect's email and update status. FAILED -> contact-form fallback."""
    if not prospect.email:
        if prospect.contact_form_url:
            prospect.status = ProspectStatus.FORM_ONLY
            session.commit()
            log_event(session, "verifier",
                      f"{prospect.name}: no email, falling back to contact form")
        return None

    result = verify_email_address(prospect.email, website=prospect.website or "")
    prospect.email = result.normalized or prospect.email
    prospect.email_verification_level = result.level

    intel = dict(prospect.intel_json or {})
    intel["email_quality"] = "role" if result.is_role else "personal"
    intel["verification_detail"] = result.detail
    if result.reason:
        intel["reject_reason"] = result.reason
    else:
        intel.pop("reject_reason", None)
    prospect.intel_json = intel

    if result.level == VerificationLevel.FAILED:
        prospect.status = (
            ProspectStatus.FORM_ONLY if prospect.contact_form_url else ProspectStatus.ENRICHED
        )
        log_event(session, "verifier",
                  f"{prospect.name}: {prospect.email} FAILED ({result.detail})",
                  level="WARNING")
    else:
        prospect.status = ProspectStatus.VERIFIED
        log_event(session, "verifier",
                  f"{prospect.name}: {prospect.email} -> {result.level}"
                  + (" [role address]" if result.is_role else ""))
    session.commit()
    return result
