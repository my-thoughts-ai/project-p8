"""Wind chill, threshold grading and summit-window detection."""
import pandas as pd
import pytest

from src.metrics import (AMBER, GREEN, RED, _grade, evaluate, overall_status,
                         red_events, summit_windows, wind_chill_c, worst)


@pytest.mark.parametrize("temp,wind,expected", [
    (-20, 50, -35),   # NWS table reference points, +/- 1 C
    (-10, 30, -19.5),
    (-30, 40, -49),
    (0, 20, -5),
])
def test_wind_chill_matches_nws_table(temp, wind, expected):
    assert wind_chill_c(temp, wind) == pytest.approx(expected, abs=1.5)


def test_wind_chill_outside_valid_range_returns_air_temperature():
    """The NWS formula is undefined above 10 C or below 4.8 km/h."""
    assert wind_chill_c(15, 50) == 15
    assert wind_chill_c(-20, 2) == -20


def test_wind_chill_handles_missing_values():
    assert wind_chill_c(None, 40) is None
    assert wind_chill_c(-20, None) is None


def test_wind_chill_is_never_warmer_than_air_in_valid_range():
    for temp in (-40, -20, -5, 0, 5):
        assert wind_chill_c(temp, 30) <= temp


def test_grade_boundaries():
    assert _grade(29.9, 30, 50) == GREEN
    assert _grade(30, 30, 50) == AMBER
    assert _grade(49.9, 30, 50) == AMBER
    assert _grade(50, 30, 50) == RED


def test_grade_lower_is_worse_for_wind_chill():
    assert _grade(-39, -40, -50, lower_is_worse=True) == GREEN
    assert _grade(-40, -40, -50, lower_is_worse=True) == AMBER
    assert _grade(-50, -40, -50, lower_is_worse=True) == RED
    assert _grade(-60, -40, -50, lower_is_worse=True) == RED


def test_grade_missing_value_is_green_not_a_crash():
    assert _grade(None, 30, 50) == GREEN


def test_worst_picks_highest_severity():
    assert worst(GREEN, AMBER, RED) == RED
    assert worst(GREEN, AMBER) == AMBER
    assert worst(GREEN, GREEN) == GREEN


def _synthetic(hours, wind, precip=0.0, temp=-15.0):
    """Build a two-band frame with fixed conditions for `hours` hours."""
    base = pd.Timestamp("2026-09-01T00:00Z")
    rows = []
    for h in range(hours):
        for altitude in (5700, 8100):
            rows.append({
                "time": base + pd.Timedelta(hours=h),
                "altitude_m": altitude, "label": str(altitude), "climbing": True,
                "temperature_c": temp,
                "wind_speed_kmh": wind[h] if isinstance(wind, list) else wind,
                "wind_direction_deg": 270.0, "relative_humidity_pct": 50.0,
                "precipitation": precip, "snowfall": 0.0, "wind_gusts_10m": 10.0,
                "cape": 0.0, "pressure_msl": 1010.0, "freezing_level_height": 5000.0,
                "extrapolated": False, "lower_level_hpa": 500, "upper_level_hpa": 400,
            })
    return pd.DataFrame(rows)


def test_summit_window_found_when_conditions_are_calm(cfg):
    frame = evaluate(_synthetic(12, wind=10.0), cfg)
    windows = summit_windows(frame, cfg)
    assert len(windows) == 1
    assert windows[0]["hours"] == 12


def test_no_summit_window_when_windy(cfg):
    frame = evaluate(_synthetic(12, wind=70.0), cfg)
    assert summit_windows(frame, cfg) == []


def test_no_summit_window_when_precipitating(cfg):
    frame = evaluate(_synthetic(12, wind=10.0, precip=1.0), cfg)
    assert summit_windows(frame, cfg) == []


def test_short_calm_spell_is_rejected(cfg):
    """A 3-hour gap is below min_consecutive_hours and must not be offered."""
    wind = [10.0] * 3 + [70.0] * 9
    frame = evaluate(_synthetic(12, wind=wind), cfg)
    assert summit_windows(frame, cfg) == []


def test_window_split_by_a_storm(cfg):
    wind = [10.0] * 8 + [70.0] * 4 + [10.0] * 8
    frame = evaluate(_synthetic(20, wind=wind), cfg)
    windows = summit_windows(frame, cfg)
    assert len(windows) == 2
    assert [w["hours"] for w in windows] == [8, 8]


def test_window_reports_worst_conditions_inside_it(cfg):
    wind = [10.0, 25.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    frame = evaluate(_synthetic(8, wind=wind), cfg)
    window = summit_windows(frame, cfg)[0]
    assert window["max_wind_kmh"] == pytest.approx(25.0)


def test_red_wind_produces_an_event(cfg):
    frame = evaluate(_synthetic(6, wind=80.0), cfg)
    events = red_events(frame, cfg, 24)
    assert events
    assert any("wind" in reason for event in events for reason in event["reasons"])


def test_calm_forecast_produces_no_events(cfg):
    frame = evaluate(_synthetic(6, wind=10.0), cfg)
    assert red_events(frame, cfg, 24) == []


def test_red_events_respect_the_lookahead_horizon(cfg):
    """A storm 20 hours out must not appear in a 6-hour danger check."""
    wind = [10.0] * 20 + [80.0] * 4
    frame = evaluate(_synthetic(24, wind=wind), cfg)
    assert red_events(frame, cfg, 6) == []
    assert red_events(frame, cfg, 24)


def test_overall_status_on_real_data(frame):
    assert overall_status(frame, 24) in (GREEN, AMBER, RED)


def test_evaluate_adds_expected_columns(frame):
    for column in ("wind_chill_c", "status", "status_wind", "mslp_change_6h",
                   "snowfall_24h"):
        assert column in frame.columns
