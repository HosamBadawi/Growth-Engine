"""Verifier: the module that exists because the first-ever cold email bounced."""
import dns.resolver
import pytest

from db.models import ProspectStatus, VerificationLevel
from engine import verifier
from engine.verifier import verify_email_address, verify_prospect


def test_bad_syntax_fails():
    assert verify_email_address("not-an-email", allow_probe=False).level == VerificationLevel.FAILED
    assert verify_email_address("a@b", allow_probe=False).level == VerificationLevel.FAILED
    assert verify_email_address("", allow_probe=False).level == VerificationLevel.FAILED


def test_disposable_domain_fails():
    result = verify_email_address("x@mailinator.com", allow_probe=False)
    assert result.level == VerificationLevel.FAILED
    assert result.is_disposable


def test_nxdomain_fails(monkeypatch):
    def boom(domain):
        raise dns.resolver.NXDOMAIN()
    monkeypatch.setattr(verifier, "_resolve_mx", boom)
    result = verify_email_address("owner@no-such-domain-zzz.example", allow_probe=False)
    assert result.level == VerificationLevel.FAILED


def test_mx_level_when_probe_off(monkeypatch):
    monkeypatch.setattr(verifier, "_resolve_mx", lambda d: ["mx1.example.com"])
    result = verify_email_address("owner@realdomain.example", allow_probe=False)
    assert result.level == VerificationLevel.MX


def test_syntax_level_when_no_mx_but_a_record(monkeypatch):
    def no_answer(domain):
        raise dns.resolver.NoAnswer()
    monkeypatch.setattr(verifier, "_resolve_mx", no_answer)
    monkeypatch.setattr(verifier, "_has_a_record", lambda d: True)
    result = verify_email_address("owner@apex.example", allow_probe=False)
    assert result.level == VerificationLevel.SYNTAX


def test_smtp_ok_via_probe(monkeypatch):
    monkeypatch.setenv("SMTP_PROBE_ENABLED", "true")
    from engine.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr(verifier, "_resolve_mx", lambda d: ["mx1.example.com"])
    monkeypatch.setattr(verifier, "port25_reachable", lambda: True)
    monkeypatch.setattr(verifier, "smtp_probe",
                        lambda email, hosts: (VerificationLevel.SMTP_OK, "250 ok"))
    result = verify_email_address("owner@realdomain.example")
    assert result.level == VerificationLevel.SMTP_OK


def test_probe_inconclusive_keeps_mx(monkeypatch):
    monkeypatch.setenv("SMTP_PROBE_ENABLED", "true")
    from engine.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr(verifier, "_resolve_mx", lambda d: ["mx1.example.com"])
    monkeypatch.setattr(verifier, "port25_reachable", lambda: True)
    monkeypatch.setattr(verifier, "smtp_probe", lambda email, hosts: (None, "greylisted"))
    result = verify_email_address("owner@realdomain.example")
    assert result.level == VerificationLevel.MX


def test_role_address_flagged(monkeypatch):
    monkeypatch.setattr(verifier, "_resolve_mx", lambda d: ["mx1.example.com"])
    assert verify_email_address("info@shop.example", allow_probe=False).is_role
    assert not verify_email_address("mike@shop.example", allow_probe=False).is_role


def test_failed_prospect_falls_back_to_form(session, prospect, monkeypatch):
    prospect.contact_form_url = "https://tampabayplumbingpros.example/contact"
    session.commit()

    def boom(domain):
        raise dns.resolver.NXDOMAIN()
    monkeypatch.setattr(verifier, "_resolve_mx", boom)
    verify_prospect(session, prospect)
    assert prospect.email_verification_level == VerificationLevel.FAILED
    assert prospect.status == ProspectStatus.FORM_ONLY


def test_verified_prospect_status(session, prospect, monkeypatch):
    monkeypatch.setattr(verifier, "_resolve_mx", lambda d: ["mx1.example.com"])
    verify_prospect(session, prospect)
    assert prospect.status == ProspectStatus.VERIFIED
    assert prospect.email_verification_level == VerificationLevel.MX
