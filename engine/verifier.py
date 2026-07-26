"""Module 3 — Verifier: no email is queued without verification.

Pipeline: syntax (email-validator) -> MX (dnspython) -> optional SMTP RCPT probe
on port 25 with graceful degradation (many networks block 25).
Levels: SYNTAX | MX | SMTP_OK | FAILED. FAILED is never sent to.
"""
import logging
import smtplib
import socket
from dataclasses import dataclass
from functools import lru_cache

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


@dataclass
class VerificationResult:
    level: str
    detail: str = ""
    is_role: bool = False
    is_disposable: bool = False
    normalized: str = ""


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


def verify_email_address(email: str, allow_probe: bool = True) -> VerificationResult:
    try:
        validated = validate_email(email, check_deliverability=False)
        normalized = validated.normalized.lower()
    except EmailNotValidError as exc:
        return VerificationResult(VerificationLevel.FAILED, f"syntax: {exc}")

    local, domain = normalized.rsplit("@", 1)
    is_role = local in ROLE_PREFIXES
    is_disposable = domain in DISPOSABLE_DOMAINS
    if is_disposable:
        return VerificationResult(
            VerificationLevel.FAILED, "disposable domain", is_role, True, normalized
        )

    try:
        mx_hosts = _resolve_mx(domain)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return VerificationResult(VerificationLevel.FAILED, "domain does not resolve",
                                  is_role, False, normalized)
    except dns.resolver.NoAnswer:
        # RFC 5321 implicit MX: fall back to A record
        if _has_a_record(domain):
            return VerificationResult(VerificationLevel.SYNTAX, "no MX, A record exists",
                                      is_role, False, normalized)
        return VerificationResult(VerificationLevel.FAILED, "no MX and no A record",
                                  is_role, False, normalized)
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
    return VerificationResult(level, detail, is_role, False, normalized)


def verify_prospect(session: Session, prospect: Prospect) -> VerificationResult | None:
    """Verify a prospect's email and update status. FAILED -> contact-form fallback."""
    if not prospect.email:
        if prospect.contact_form_url:
            prospect.status = ProspectStatus.FORM_ONLY
            session.commit()
            log_event(session, "verifier",
                      f"{prospect.name}: no email, falling back to contact form")
        return None

    result = verify_email_address(prospect.email)
    prospect.email = result.normalized or prospect.email
    prospect.email_verification_level = result.level

    intel = dict(prospect.intel_json or {})
    intel["email_quality"] = "role" if result.is_role else "personal"
    intel["verification_detail"] = result.detail
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
