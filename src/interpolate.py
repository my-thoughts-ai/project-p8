"""Interpolate pressure-level forecasts onto exact target altitudes.

Two rules drive this module:

1. Use the *returned* geopotential height of each pressure level to locate a target
   altitude. The standard-atmosphere hPa-to-metre table drifts by hundreds of metres
   with the synoptic situation and is only used to decide which levels to request.
2. Never interpolate a wind direction in degrees. Directions are converted to u/v
   components, the components are interpolated, and speed/direction are recomputed.
   Interpolating degrees breaks across the 359 -> 0 wrap.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

LOG = logging.getLogger("manaslu.interpolate")

# Meteorological direction (degrees the wind blows *from*) <-> u/v components.
# u is the eastward component, v the northward component.


def wind_to_uv(speed: float, direction_deg: float) -> Tuple[float, float]:
    """Convert speed + meteorological direction to (u, v) components."""
    rad = math.radians(direction_deg)
    u = -speed * math.sin(rad)
    v = -speed * math.cos(rad)
    return u, v


def uv_to_wind(u: float, v: float) -> Tuple[float, float]:
    """Convert (u, v) components back to (speed, meteorological direction)."""
    speed = math.hypot(u, v)
    if speed == 0.0:
        return 0.0, 0.0
    direction = (math.degrees(math.atan2(-u, -v))) % 360.0
    if direction >= 359.9999:  # float noise at due north must read 0, not 360
        direction = 0.0
    return speed, direction


def _linear(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation of y at x between (x0,y0) and (x1,y1)."""
    if x1 == x0:
        return y0
    weight = (x - x0) / (x1 - x0)
    return y0 + weight * (y1 - y0)


def _bracket(heights: List[Tuple[int, float]], target: float
             ) -> Tuple[Optional[Tuple[int, float]], Optional[Tuple[int, float]], bool]:
    """Find the two levels bracketing `target`.

    `heights` is [(hPa, geopotential_height_m), ...] sorted ascending by height.
    Returns (lower, upper, extrapolated). If the target lies outside the profile the
    nearest pair is returned with extrapolated=True.
    """
    if len(heights) < 2:
        return None, None, True

    for lower, upper in zip(heights, heights[1:]):
        if lower[1] <= target <= upper[1]:
            return lower, upper, False

    if target < heights[0][1]:
        return heights[0], heights[1], True
    return heights[-2], heights[-1], True


def _level_profile(row: pd.Series, levels: List[int]) -> List[Tuple[int, float]]:
    """Build the (hPa, height) profile for one timestamp, sorted by height ascending."""
    profile: List[Tuple[int, float]] = []
    for level in levels:
        height = row.get("geopotential_height_{}hPa".format(level))
        if height is not None and pd.notna(height):
            profile.append((level, float(height)))
    profile.sort(key=lambda item: item[1])
    return profile


def payload_to_frame(payload: Dict[str, Any]) -> pd.DataFrame:
    """Turn the Open-Meteo hourly block into a DataFrame indexed by UTC timestamp."""
    hourly = payload["hourly"]
    frame = pd.DataFrame(hourly)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame.set_index("time").sort_index()


def interpolate_bands(payload: Dict[str, Any], cfg: Dict[str, Any]) -> pd.DataFrame:
    """Interpolate every configured altitude band for every forecast timestamp.

    Returns a tidy frame: one row per (time, altitude_m) with interpolated
    temperature, humidity, wind speed/direction, plus the surface context columns
    repeated per row for convenience downstream.
    """
    frame = payload_to_frame(payload)
    levels = list(cfg["forecast"]["pressure_levels_hpa"])
    bands = cfg["altitude_bands"]

    surface_cols = [c for c in (
        "precipitation", "snowfall", "freezing_level_height", "wind_gusts_10m",
        "cloud_cover", "cape", "pressure_msl", "weather_code",
    ) if c in frame.columns]

    records: List[Dict[str, Any]] = []
    extrapolation_warned = set()

    for timestamp, row in frame.iterrows():
        profile = _level_profile(row, levels)
        if len(profile) < 2:
            continue

        for band in bands:
            target = float(band["altitude_m"])
            lower, upper, extrapolated = _bracket(profile, target)
            if lower is None or upper is None:
                continue
            if extrapolated and target not in extrapolation_warned:
                LOG.warning(
                    "Altitude %.0f m lies outside the pressure-level profile "
                    "(%.0f-%.0f m); extrapolating.",
                    target, profile[0][1], profile[-1][1],
                )
                extrapolation_warned.add(target)

            (p_lo, h_lo), (p_hi, h_hi) = lower, upper

            def at(var: str) -> Tuple[Optional[float], Optional[float]]:
                lo = row.get("{}_{}hPa".format(var, p_lo))
                hi = row.get("{}_{}hPa".format(var, p_hi))
                lo = float(lo) if lo is not None and pd.notna(lo) else None
                hi = float(hi) if hi is not None and pd.notna(hi) else None
                return lo, hi

            t_lo, t_hi = at("temperature")
            rh_lo, rh_hi = at("relative_humidity")
            ws_lo, ws_hi = at("wind_speed")
            wd_lo, wd_hi = at("wind_direction")

            temperature = (_linear(target, h_lo, h_hi, t_lo, t_hi)
                           if None not in (t_lo, t_hi) else None)
            humidity = (_linear(target, h_lo, h_hi, rh_lo, rh_hi)
                        if None not in (rh_lo, rh_hi) else None)

            wind_speed = wind_dir = None
            if None not in (ws_lo, ws_hi, wd_lo, wd_hi):
                u_lo, v_lo = wind_to_uv(ws_lo, wd_lo)
                u_hi, v_hi = wind_to_uv(ws_hi, wd_hi)
                u = _linear(target, h_lo, h_hi, u_lo, u_hi)
                v = _linear(target, h_lo, h_hi, v_lo, v_hi)
                wind_speed, wind_dir = uv_to_wind(u, v)

            record: Dict[str, Any] = {
                "time": timestamp,
                "altitude_m": int(target),
                "label": band.get("label", str(target)),
                "climbing": bool(band.get("climbing", True)),
                "temperature_c": temperature,
                "relative_humidity_pct": humidity,
                "wind_speed_kmh": wind_speed,
                "wind_direction_deg": wind_dir,
                "lower_level_hpa": p_lo,
                "upper_level_hpa": p_hi,
                "extrapolated": extrapolated,
            }
            for col in surface_cols:
                value = row.get(col)
                record[col] = float(value) if pd.notna(value) else None
            records.append(record)

    result = pd.DataFrame.from_records(records)
    if result.empty:
        LOG.error("Interpolation produced no rows — check the pressure-level response.")
        return result
    return result.sort_values(["time", "altitude_m"]).reset_index(drop=True)
