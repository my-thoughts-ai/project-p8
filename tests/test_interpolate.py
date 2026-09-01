"""Interpolation and wind vector maths -- the parts most likely to break silently."""
import math

import pytest

from src.interpolate import (_bracket, _linear, interpolate_bands, uv_to_wind,
                             wind_to_uv)


@pytest.mark.parametrize("speed,direction", [
    (10, 0), (10, 45), (25, 90), (40, 180), (60, 270), (5, 359), (33.3, 123.4),
])
def test_wind_uv_round_trip(speed, direction):
    u, v = wind_to_uv(speed, direction)
    speed2, direction2 = uv_to_wind(u, v)
    assert speed2 == pytest.approx(speed, abs=1e-9)
    assert direction2 % 360 == pytest.approx(direction % 360, abs=1e-9)


def test_north_wind_has_negative_v():
    """A northerly blows *from* the north, so its v component points south."""
    u, v = wind_to_uv(10, 0)
    assert v == pytest.approx(-10)
    assert u == pytest.approx(0, abs=1e-9)


def test_east_wind_has_negative_u():
    u, v = wind_to_uv(10, 90)
    assert u == pytest.approx(-10)


def test_direction_wrap_does_not_average_to_south():
    """The whole reason for u/v: 359 and 1 degrees must average to north, not south."""
    u1, v1 = wind_to_uv(20, 359)
    u2, v2 = wind_to_uv(20, 1)
    speed, direction = uv_to_wind((u1 + u2) / 2, (v1 + v2) / 2)
    assert direction == pytest.approx(0, abs=0.01) or direction == pytest.approx(360, abs=0.01)
    assert speed == pytest.approx(20, rel=0.01)
    naive = (359 + 1) / 2  # what interpolating degrees directly would have given
    assert abs(naive - 180) < 1


def test_opposing_winds_cancel():
    u1, v1 = wind_to_uv(10, 0)
    u2, v2 = wind_to_uv(10, 180)
    speed, _ = uv_to_wind((u1 + u2) / 2, (v1 + v2) / 2)
    assert speed == pytest.approx(0, abs=1e-9)


def test_linear_interpolation():
    assert _linear(5, 0, 10, 0, 100) == pytest.approx(50)
    assert _linear(0, 0, 10, 3, 100) == pytest.approx(3)
    assert _linear(5, 5, 5, 7, 9) == 7  # degenerate bracket returns lower value


def test_bracket_finds_enclosing_levels():
    profile = [(600, 4400.0), (550, 5000.0), (500, 5900.0), (400, 7600.0)]
    lower, upper, extrapolated = _bracket(profile, 5700)
    assert (lower[0], upper[0]) == (550, 500)
    assert not extrapolated


def test_bracket_flags_extrapolation_above_and_below():
    profile = [(600, 4400.0), (500, 5900.0), (400, 7600.0)]
    _, _, high = _bracket(profile, 9000)
    _, _, low = _bracket(profile, 1000)
    assert high and low


def test_interpolation_uses_geopotential_not_the_hpa_table(payload, cfg):
    """8100 m must be bracketed by the levels whose *returned heights* enclose it."""
    frame = interpolate_bands(payload, cfg)
    summit = frame[frame.altitude_m == 8100].iloc[0]
    hourly = payload["hourly"]
    lower_h = hourly["geopotential_height_{}hPa".format(summit.lower_level_hpa)][0]
    upper_h = hourly["geopotential_height_{}hPa".format(summit.upper_level_hpa)][0]
    assert lower_h <= 8100 <= upper_h
    # The spec's static table maps 350 hPa to ~8100 m; the real atmosphere disagrees,
    # which is exactly why the table must not be used.
    assert not math.isclose(upper_h, 8100, abs_tol=50)


def test_all_bands_present_for_every_timestamp(payload, cfg):
    frame = interpolate_bands(payload, cfg)
    expected = {band["altitude_m"] for band in cfg["altitude_bands"]}
    for _, group in frame.groupby("time"):
        assert set(group.altitude_m) == expected


def test_temperature_decreases_with_altitude(payload, cfg):
    """Below the tropopause the profile must be monotonically cooler with height."""
    frame = interpolate_bands(payload, cfg)
    first = frame[frame.time == frame.time.min()].sort_values("altitude_m")
    temps = list(first.temperature_c)
    assert temps == sorted(temps, reverse=True)


def test_no_nulls_in_core_columns(payload, cfg):
    frame = interpolate_bands(payload, cfg)
    for column in ("temperature_c", "wind_speed_kmh", "wind_direction_deg"):
        assert frame[column].notna().all()


def test_interpolated_values_lie_between_bracketing_levels(payload, cfg):
    frame = interpolate_bands(payload, cfg)
    hourly = payload["hourly"]
    row = frame[(frame.altitude_m == 6900)].iloc[0]
    lo = hourly["temperature_{}hPa".format(row.lower_level_hpa)][0]
    hi = hourly["temperature_{}hPa".format(row.upper_level_hpa)][0]
    assert min(lo, hi) <= row.temperature_c <= max(lo, hi)
