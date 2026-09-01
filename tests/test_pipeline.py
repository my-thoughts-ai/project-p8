"""Fetch parameter construction, config integrity and end-to-end mode runs."""
import pytest

from src.alerts import build_channels, dispatch, summarise
from src.fetch import LEVEL_VARIABLES, SURFACE_VARIABLES, build_hourly_params
from src.main import main


def test_every_level_gets_every_variable(cfg):
    levels = cfg["forecast"]["pressure_levels_hpa"]
    params = build_hourly_params(levels)
    for level in levels:
        for variable in LEVEL_VARIABLES:
            assert "{}_{}hPa".format(variable, level) in params
    for variable in SURFACE_VARIABLES:
        assert variable in params


def test_geopotential_height_is_always_requested(cfg):
    """Without it the interpolation has nothing to locate altitudes against."""
    params = build_hourly_params(cfg["forecast"]["pressure_levels_hpa"])
    assert sum(1 for p in params if p.startswith("geopotential_height")) == \
        len(cfg["forecast"]["pressure_levels_hpa"])


def test_configured_levels_bracket_every_target_altitude(cfg, payload):
    """Each band must sit inside the profile, or it is silently extrapolated."""
    hourly = payload["hourly"]
    heights = sorted(
        hourly["geopotential_height_{}hPa".format(level)][0]
        for level in cfg["forecast"]["pressure_levels_hpa"]
    )
    for band in cfg["altitude_bands"]:
        assert heights[0] <= band["altitude_m"] <= heights[-1], \
            "{} m is outside the fetched pressure levels".format(band["altitude_m"])


def test_no_band_is_extrapolated_on_real_data(frame):
    assert not frame["extrapolated"].any()


def test_thresholds_are_ordered_sanely(cfg):
    th = cfg["thresholds"]
    for name in ("wind_speed_kmh", "wind_gusts_10m_kmh", "cape_j_per_kg"):
        assert th[name]["amber"] < th[name]["red"]
    assert th["wind_chill_c"]["amber"] > th["wind_chill_c"]["red"]  # colder is worse


def test_recipients_are_env_references_not_literals():
    """config.yaml is committed, so it must name variables, never real contacts."""
    import re
    from pathlib import Path
    raw = Path(__file__).resolve().parent.parent.joinpath("config.yaml").read_text()
    recipients = raw.split("recipients:")[1].split("\n\n")[0]
    assert "${MANASLU_EMAIL_TO}" in recipients
    assert "${MANASLU_SMS_TO}" in recipients
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", recipients)
    assert not re.search(r"\+\d{7,}", recipients)


def test_recipients_resolve_from_the_environment(monkeypatch):
    from src.config import load_config
    monkeypatch.setenv("MANASLU_EMAIL_TO", "climber@example.com")
    monkeypatch.setenv("MANASLU_SMS_TO", "+491700000000")
    resolved = load_config()["recipients"]
    assert resolved["email"] == "climber@example.com"
    assert resolved["sms"] == "+491700000000"


def test_unset_recipients_expand_to_empty_not_a_literal_placeholder(monkeypatch):
    """A literal "${MANASLU_SMS_TO}" handed to Twilio would be a confusing failure."""
    from src.config import load_config
    monkeypatch.delenv("MANASLU_EMAIL_TO", raising=False)
    monkeypatch.delenv("MANASLU_SMS_TO", raising=False)
    resolved = load_config()["recipients"]
    assert resolved["email"] == ""
    assert resolved["sms"] == ""


def test_email_falls_back_to_the_sending_account(cfg, monkeypatch):
    """Unset MANASLU_EMAIL_TO means "mail it to me", not a broken channel."""
    from src.alerts import build_channels
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)
    patched = {**cfg, "recipients": {"email": "", "sms": ""}}
    email = [c for c in build_channels(patched) if c.name == "email"][0]
    assert email.recipient == "me@example.com"


def test_sms_without_a_recipient_is_skipped_not_sent(cfg, monkeypatch):
    """There is no safe default phone number, so SMS must refuse rather than guess."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "x" * 32)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15551234567")
    monkeypatch.setattr("src.alerts.datetime", _FrozenDate(
        __import__("datetime").datetime.fromisoformat("2026-09-01T12:00:00+00:00")))
    patched = {**cfg,
               "recipients": {"email": "", "sms": ""},
               "channels": {**cfg["channels"],
                            "sms": {**cfg["channels"]["sms"], "enabled": True}}}
    results = dispatch(patched, {"subject": "s", "text": "t", "sms": "x"})
    assert not any(name == "sms" for name, _ in results["sent"])
    assert any(name == "sms" and "recipient" in detail
               for name, detail in results["skipped"])
    assert results["failed"] == []


def test_all_channels_default_to_off_except_console(cfg):
    """Shipping with a live channel enabled would send on the first accidental run."""
    for name, settings in cfg["channels"].items():
        if name != "console":
            assert settings["enabled"] is False


def test_disabled_channels_are_skipped_not_failed(cfg):
    results = dispatch(cfg, {"subject": "s", "text": "t", "sms": "x"}, dry_run=True)
    assert results["failed"] == []
    assert any(name == "console" for name, _ in results["sent"])


def test_channels_without_secrets_report_them(cfg, monkeypatch):
    for name in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    email = [c for c in build_channels(cfg) if c.name == "email"][0]
    assert email.missing_secrets()
    assert not email.ready


def test_summarise_never_returns_empty():
    assert summarise({"sent": [], "skipped": [], "failed": []})


@pytest.mark.parametrize("mode", ["test", "digest", "danger"])
def test_modes_run_end_to_end_offline(mode, tmp_path, monkeypatch, capsys):
    """Full run against the fixture: no network, no sends, temp database."""
    import src.config as config_module
    cfg = config_module.load_config()
    cfg["storage"]["db_path"] = str(tmp_path / "run.sqlite")
    cfg["runtime"]["log_path"] = str(tmp_path / "run.log")
    monkeypatch.setattr(config_module, "load_config", lambda path=None: cfg)
    monkeypatch.setattr("src.main.load_config", lambda path=None: cfg)

    code = main(["--mode", mode, "--fixture", "tests/fixtures/openmeteo_gfs.json",
                 "--dry-run", "--no-cross-check"])
    assert code == 0


def test_danger_mode_does_not_repeat_itself(tmp_path, monkeypatch):
    import src.config as config_module
    cfg = config_module.load_config()
    cfg["storage"]["db_path"] = str(tmp_path / "dedup.sqlite")
    cfg["runtime"]["log_path"] = str(tmp_path / "dedup.log")
    monkeypatch.setattr("src.main.load_config", lambda path=None: cfg)

    args = ["--mode", "danger", "--fixture", "tests/fixtures/openmeteo_gfs.json"]
    assert main(args) == 0
    from src.store import Store
    with Store(cfg["storage"]["db_path"]) as store:
        first = store.get_alert_state()
        assert first is not None, "fixture should contain a red condition"

    assert main(args) == 0
    with Store(cfg["storage"]["db_path"]) as store:
        rows = [r for r in store.recent_runs(5) if r["mode"] == "danger"]
        assert any("no send" in (r["detail"] or "") for r in rows)


def test_bad_fixture_path_exits_nonzero_without_crashing(tmp_path, monkeypatch):
    import src.config as config_module
    cfg = config_module.load_config()
    cfg["storage"]["db_path"] = str(tmp_path / "x.sqlite")
    cfg["runtime"]["log_path"] = str(tmp_path / "x.log")
    monkeypatch.setattr("src.main.load_config", lambda path=None: cfg)
    assert main(["--mode", "digest", "--fixture", "/nonexistent.json"]) == 2


class _FrozenDate:
    """Minimal stand-in so the expiry boundary can be tested without a time library."""

    def __init__(self, day):
        self._day = day

    def now(self, tz=None):
        return self._day


def _sms_channel(cfg, enabled=True, active_until="2026-09-10"):
    from src.alerts import build_channels
    settings = dict(cfg["channels"]["sms"])
    settings["enabled"] = enabled
    if active_until is None:
        settings.pop("active_until", None)
    else:
        settings["active_until"] = active_until
    patched = {**cfg, "channels": {**cfg["channels"], "sms": settings}}
    return [c for c in build_channels(patched) if c.name == "sms"][0], patched


def _freeze(monkeypatch, iso_day):
    import datetime as real_datetime
    day = real_datetime.datetime.fromisoformat(iso_day + "T12:00:00+00:00")
    monkeypatch.setattr("src.alerts.datetime", _FrozenDate(day))


@pytest.mark.parametrize("today,expired", [
    ("2026-09-01", False),   # well inside the window
    ("2026-09-09", False),   # day before
    ("2026-09-10", False),   # the final day is inclusive
    ("2026-09-11", True),    # first day after
    ("2026-12-01", True),    # long after
])
def test_sms_expiry_boundary(cfg, monkeypatch, today, expired):
    channel, _ = _sms_channel(cfg)
    _freeze(monkeypatch, today)
    assert channel.expired is expired


def test_expired_sms_is_skipped_not_failed(cfg, monkeypatch):
    """An expired channel is a deliberate skip; it must not look like a delivery failure."""
    channel, patched = _sms_channel(cfg)
    _freeze(monkeypatch, "2026-09-11")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "x" * 32)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15551234567")

    results = dispatch(patched, {"subject": "s", "text": "t", "sms": "x"})
    assert results["failed"] == []
    assert any(name == "sms" and "expired" in detail for name, detail in results["skipped"])
    assert not any(name == "sms" for name, _ in results["sent"])


def test_email_has_no_expiry(cfg):
    """Only SMS is date-limited; the email channel must keep running indefinitely."""
    from src.alerts import build_channels
    email = [c for c in build_channels(cfg) if c.name == "email"][0]
    assert email.active_until is None
    assert not email.expired


def test_channel_without_active_until_never_expires(cfg, monkeypatch):
    channel, _ = _sms_channel(cfg, active_until=None)
    _freeze(monkeypatch, "2030-01-01")
    assert channel.active_until is None
    assert not channel.expired


def test_unparseable_expiry_fails_closed(cfg, monkeypatch):
    """A typo'd date must stop sending, not silently disable the limit."""
    channel, _ = _sms_channel(cfg, active_until="10.09.2026")
    _freeze(monkeypatch, "2026-09-01")
    assert channel.expired


def test_configured_sms_expiry_is_the_agreed_date(cfg):
    assert cfg["channels"]["sms"]["active_until"] == "2026-09-10"
