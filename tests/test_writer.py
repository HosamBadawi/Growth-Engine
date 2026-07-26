"""Writer: trade-aware job values, first-touch link discipline, style gate."""
from db.models import TouchType
from engine.config import get_settings
from engine.writer import check_style, render_email_template, template_context


def test_job_value_by_trade(monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.job_value_for("hvac") == 700
    assert settings.job_value_for("HVAC") == 700
    assert settings.job_value_for("plumber") == 500
    assert settings.job_value_for("roofing") == settings.default_job_value  # unknown
    assert settings.job_value_for(None) == settings.default_job_value
    assert settings.job_value_for("hvac") > settings.default_job_value


def test_template_context_uses_trade_value(prospect):
    prospect.trade = "hvac"
    ctx = template_context(prospect)
    assert ctx["job_value"] == 700
    assert ctx["weekly_loss"] == 700 * get_settings().missed_calls_per_week
    assert ctx["yearly_loss"] == ctx["weekly_loss"] * 52


def _valid_email1_body(prospect) -> str:
    settings = get_settings()
    return (
        f"Hi Mike,\n\nI came across {prospect.name} while researching Tampa "
        f"plumber companies, and your 4.8 star rating stood out.\n\n"
        f"When a customer calls after hours, what happens to that call? For most "
        f"companies a $500 job walks away.\n\n"
        f"60 second demo: {settings.demo_url}\n\nWorth a quick look?\n\n"
        f"Best regards from the team"
    )


def test_email1_forbids_calendar_link(prospect):
    settings = get_settings()
    body = _valid_email1_body(prospect)
    assert check_style("quick question", body, prospect, TouchType.EMAIL_1) == []

    body_with_cal = body + f"\nMy calendar: {settings.calendly_url}"
    violations = check_style("quick question", body_with_cal, prospect, TouchType.EMAIL_1)
    assert any("must NOT contain the calendar link" in v for v in violations)


def test_followups_require_calendar_link(prospect):
    settings = get_settings()
    body = _valid_email1_body(prospect)  # no calendar link
    violations = check_style("re: quick question", body, prospect, TouchType.FOLLOWUP_2)
    assert any("missing the calendar link" in v for v in violations)

    body_with_cal = body + f"\nMy calendar: {settings.calendly_url}"
    assert check_style("re: quick question", body_with_cal, prospect,
                       TouchType.FOLLOWUP_2) == []


def test_initial_template_has_demo_but_no_calendar(prospect):
    settings = get_settings()
    subject, body = render_email_template("initial.j2", prospect)
    assert settings.demo_url in body
    assert settings.calendly_url not in body
    assert check_style(subject, body, prospect, TouchType.EMAIL_1) == []


def test_followup_templates_carry_calendar(prospect):
    settings = get_settings()
    for name in ("followup_day3.j2", "followup_day6.j2"):
        _, body = render_email_template(name, prospect)
        assert settings.calendly_url in body, name
