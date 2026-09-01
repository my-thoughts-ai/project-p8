"""Rendering: SMS segment budget, HTML integrity, disclaimer presence."""
import pytest

from src.config import DISCLAIMER
from src.format import (GSM7_SAFE, build_context, compass, render_email_html,
                        render_email_text, render_sms, render_telegram,
                        sms_segments, warning_lines)
from src.metrics import red_events, summit_windows


@pytest.fixture
def context(frame, cfg, payload):
    windows = summit_windows(frame, cfg)
    events = red_events(frame, cfg, 24)
    return build_context(frame, cfg, payload, windows, warning_lines(events, cfg),
                         "Test digest")


def test_segment_counting_matches_twilio_rules():
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2
    assert sms_segments("a" * 306) == 2
    assert sms_segments("") == 0


def test_emoji_halves_the_segment_length():
    """One emoji forces UCS-2 and doubles the bill -- SMS output must stay ASCII."""
    assert sms_segments("a" * 100) == 1
    assert sms_segments("a" * 100 + "\N{SNOWMAN}") == 2


def test_sms_stays_within_the_segment_budget(frame, cfg, context):
    text = render_sms(frame, cfg, context)
    assert sms_segments(text) <= cfg["channels"]["sms"]["max_segments"]


def test_sms_contains_no_non_gsm_characters(frame, cfg, context):
    text = render_sms(frame, cfg, context)
    assert all(ch in GSM7_SAFE for ch in text), \
        "non-GSM characters would halve the segment length"


def test_sms_carries_the_essentials(frame, cfg, context):
    text = render_sms(frame, cfg, context)
    assert "MANASLU" in text
    assert context["overall"].upper() in text
    assert "go/no-go" in text  # the disclaimer survives compression


def test_sms_truncates_rather_than_overflowing(frame, cfg, context):
    narrow = {**cfg, "channels": {**cfg["channels"], "sms": {"enabled": False, "max_segments": 1}}}
    text = render_sms(frame, narrow, context)
    assert len(text) <= 160


def test_danger_sms_is_labelled_as_an_alert(frame, cfg, context):
    assert render_sms(frame, cfg, context, kind="danger").startswith("MANASLU ALERT")


def test_email_html_renders_every_band(context, cfg):
    html = render_email_html(context)
    for band in cfg["altitude_bands"]:
        assert "{} m".format(band["altitude_m"]) in html


def test_email_html_is_balanced_and_complete(context):
    html = render_email_html(context)
    assert html.count("<table") == html.count("</table>")
    assert html.count("<tr") == html.count("</tr>")
    assert html.rstrip().endswith("</html>")


def test_disclaimer_present_in_every_channel(frame, cfg, context):
    assert DISCLAIMER in render_email_html(context)
    assert DISCLAIMER in render_email_text(context)
    assert DISCLAIMER in render_telegram(context)
    assert "go/no-go" in render_sms(frame, cfg, context)


def test_plain_text_has_no_html_tags(context):
    text = render_email_text(context)
    assert "<td" not in text and "<div" not in text


def test_text_and_html_agree_on_the_headline(context):
    assert context["headline"] in render_email_text(context)


@pytest.mark.parametrize("degrees,expected", [
    (0, "N"), (90, "E"), (180, "S"), (270, "W"), (45, "NE"), (359, "N"), (360, "N"),
])
def test_compass_conversion(degrees, expected):
    assert compass(degrees) == expected


def test_compass_handles_missing():
    assert compass(None) == "n/a"


def test_column_wide_hazards_are_not_repeated_per_band(frame, cfg):
    """Precipitation is a single column value; five identical lines is noise."""
    events = red_events(frame, cfg, 24)
    lines = warning_lines(events, cfg)
    assert len(lines) == len(set(lines))
    precip_lines = [l for l in lines if "precip" in l]
    assert len(precip_lines) <= 1


def test_telegram_message_uses_html_parse_mode(context):
    message = render_telegram(context)
    assert "<b>" in message and "</b>" in message


def test_context_marks_the_worst_status_in_the_header(context):
    assert context["overall"] in ("green", "amber", "red")
    assert context["header_color"].startswith("#")
