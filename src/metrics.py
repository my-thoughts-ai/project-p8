"""Derived climbing metrics: wind chill, per-band status, summit windows.

Status vocabulary is green / amber / red throughout, ordered by `severity_rank`.
Every threshold comes from config.yaml — nothing is hardcoded here.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

LOG = logging.getLogger("manaslu.metrics")

GREEN, AMBER, RED = "green", "amber", "red"
SEVERITY = {GREEN: 0, AMBER: 1, RED: 2}


def severity_rank(status: str) -> int:
    return SEVERITY.get(status, 0)


def worst(*statuses: str) -> str:
    """Return the most severe of the given statuses."""
    return max(statuses, key=severity_rank) if statuses else GREEN


def wind_chill_c(temperature_c: Optional[float], wind_kmh: Optional[float]) -> Optional[float]:
    """NWS wind chill in degrees C.

    The formula is only valid for T <= 10 C and V >= 4.8 km/h. Outside that range the
    air temperature itself is the honest answer, so it is returned unchanged.
    """
    if temperature_c is None or wind_kmh is None or pd.isna(temperature_c) or pd.isna(wind_kmh):
        return None
    if temperature_c > 10.0 or wind_kmh < 4.8:
        return float(temperature_c)
    v16 = float(wind_kmh) ** 0.16
    return (13.12 + 0.6215 * temperature_c - 11.37 * v16 + 0.3965 * temperature_c * v16)


def _grade(value: Optional[float], amber: float, red: float, lower_is_worse: bool = False) -> str:
    """Grade a value against amber/red cut-offs."""
    if value is None or pd.isna(value):
        return GREEN
    value = float(value)
    if lower_is_worse:
        if value <= red:
            return RED
        if value <= amber:
            return AMBER
        return GREEN
    if value >= red:
        return RED
    if value >= amber:
        return AMBER
    return GREEN


def mslp_trend(frame: pd.DataFrame, hours: int = 6) -> pd.Series:
    """Pressure change over the trailing `hours`, per timestamp (negative = falling)."""
    if "pressure_msl" not in frame.columns:
        return pd.Series(dtype=float)
    series = frame.groupby("time")["pressure_msl"].first().sort_index()
    return series - series.shift(hours)


def snowfall_24h(frame: pd.DataFrame) -> pd.Series:
    """Rolling 24 h snowfall total per timestamp (mm)."""
    if "snowfall" not in frame.columns:
        return pd.Series(dtype=float)
    series = frame.groupby("time")["snowfall"].first().sort_index()
    return series.rolling(24, min_periods=1).sum()


def evaluate(frame: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    """Add wind chill and per-row status columns to the interpolated frame."""
    if frame.empty:
        return frame

    th = cfg["thresholds"]
    out = frame.copy()

    out["wind_chill_c"] = [
        wind_chill_c(t, w) for t, w in zip(out["temperature_c"], out["wind_speed_kmh"])
    ]

    trend = mslp_trend(out)
    snow24 = snowfall_24h(out)
    out["mslp_change_6h"] = out["time"].map(trend)
    out["snowfall_24h"] = out["time"].map(snow24)

    def row_status(row: pd.Series) -> pd.Series:
        wind = _grade(row["wind_speed_kmh"],
                      th["wind_speed_kmh"]["amber"], th["wind_speed_kmh"]["red"])
        chill = _grade(row["wind_chill_c"],
                       th["wind_chill_c"]["amber"], th["wind_chill_c"]["red"],
                       lower_is_worse=True)
        gust = _grade(row.get("wind_gusts_10m"),
                      th["wind_gusts_10m_kmh"]["amber"], th["wind_gusts_10m_kmh"]["red"])
        precip = _grade(row.get("precipitation"),
                        th["precipitation_mm_per_h"]["amber"], th["precipitation_mm_per_h"]["red"])
        snow_h = _grade(row.get("snowfall"),
                        th["snowfall_mm_per_h"]["amber"], th["snowfall_mm_per_h"]["red"])
        snow_d = _grade(row.get("snowfall_24h"),
                        th["snowfall_mm_per_24h"]["amber"], th["snowfall_mm_per_24h"]["red"])
        cape = _grade(row.get("cape"),
                      th["cape_j_per_kg"]["amber"], th["cape_j_per_kg"]["red"])
        drop = row.get("mslp_change_6h")
        drop_mag = -float(drop) if drop is not None and pd.notna(drop) else None
        pressure = _grade(drop_mag,
                          th["mslp_drop_hpa_per_6h"]["amber"], th["mslp_drop_hpa_per_6h"]["red"])

        return pd.Series({
            "status_wind": wind,
            "status_wind_chill": chill,
            "status_gusts": gust,
            "status_precip": precip,
            "status_snow": worst(snow_h, snow_d),
            "status_cape": cape,
            "status_pressure": pressure,
            "status": worst(wind, chill, gust, precip, snow_h, snow_d, cape, pressure),
        })

    return pd.concat([out, out.apply(row_status, axis=1)], axis=1)


def band_reasons(row: pd.Series, cfg: Dict[str, Any]) -> List[str]:
    """Human-readable reasons a row is not green, most severe first."""
    th = cfg["thresholds"]
    reasons: List[str] = []

    def note(status: str, text: str) -> None:
        if status != GREEN:
            reasons.append("{}: {}".format(status.upper(), text))

    note(row["status_wind"], "wind {:.0f} km/h".format(row["wind_speed_kmh"] or 0))
    if row.get("wind_chill_c") is not None and pd.notna(row["wind_chill_c"]):
        note(row["status_wind_chill"], "wind chill {:.0f} C".format(row["wind_chill_c"]))
    note(row["status_gusts"], "surface gusts {:.0f} km/h".format(row.get("wind_gusts_10m") or 0))
    note(row["status_snow"], "snowfall {:.1f} mm/h, {:.1f} mm/24h".format(
        row.get("snowfall") or 0, row.get("snowfall_24h") or 0))
    note(row["status_precip"], "precip {:.1f} mm/h".format(row.get("precipitation") or 0))
    note(row["status_cape"], "CAPE {:.0f} J/kg".format(row.get("cape") or 0))
    if row.get("mslp_change_6h") is not None and pd.notna(row.get("mslp_change_6h")):
        note(row["status_pressure"], "pressure {:+.1f} hPa/6h".format(row["mslp_change_6h"]))
    reasons.sort(key=lambda r: 0 if r.startswith("RED") else 1)
    _ = th  # thresholds are already baked into the statuses; kept for signature stability
    return reasons


def summit_windows(frame: pd.DataFrame, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Find runs of consecutive hours where every climbing band is climbable.

    This is the actual value-add: not "is it bad right now" but "where is the gap".
    """
    if frame.empty:
        return []

    sw = cfg["summit_window"]
    lookahead = int(sw.get("lookahead_hours", 168))
    climbing = frame[frame["climbing"]].copy()
    if climbing.empty:
        return []

    start_time = climbing["time"].min()
    horizon = start_time + timedelta(hours=lookahead)
    climbing = climbing[climbing["time"] <= horizon]

    # Row-wise "this band is climbable this hour", then AND across bands per hour.
    # Vectorised deliberately: a groupby.apply here silently included the grouping
    # column and tripped a pandas deprecation.
    row_ok = (
        (climbing["wind_speed_kmh"].fillna(0.0) <= sw["max_wind_kmh"])
        & (climbing["wind_chill_c"].fillna(0.0) >= sw["min_wind_chill_c"])
        & (climbing["precipitation"].fillna(0.0) <= sw["max_precip_mm_per_h"])
        & (climbing["snowfall"].fillna(0.0) <= sw["max_precip_mm_per_h"])
    )
    ok_by_hour = row_ok.groupby(climbing["time"]).all().sort_index()

    windows: List[Dict[str, Any]] = []
    run_start = None
    previous = None

    for timestamp, ok in ok_by_hour.items():
        if ok and run_start is None:
            run_start = timestamp
        elif not ok and run_start is not None:
            windows.append({"start": run_start, "end": previous})
            run_start = None
        previous = timestamp
    if run_start is not None:
        windows.append({"start": run_start, "end": previous})

    minimum = int(sw.get("min_consecutive_hours", 6))
    result = []
    for window in windows:
        hours = int((window["end"] - window["start"]).total_seconds() // 3600) + 1
        if hours < minimum:
            continue
        span = climbing[(climbing["time"] >= window["start"]) & (climbing["time"] <= window["end"])]
        result.append({
            "start": window["start"],
            "end": window["end"],
            "hours": hours,
            "max_wind_kmh": float(span["wind_speed_kmh"].max()),
            "min_wind_chill_c": (float(span["wind_chill_c"].min())
                                 if span["wind_chill_c"].notna().any() else None),
            "hours_ahead": int((window["start"] - start_time).total_seconds() // 3600),
        })
    LOG.info("Found %d summit window(s) of >= %d h", len(result), minimum)
    return result


def red_events(frame: pd.DataFrame, cfg: Dict[str, Any], lookahead_hours: int
               ) -> List[Dict[str, Any]]:
    """Red-status rows within the next `lookahead_hours`, one entry per (time, band)."""
    if frame.empty:
        return []
    start = frame["time"].min()
    horizon = start + timedelta(hours=lookahead_hours)
    window = frame[(frame["time"] <= horizon) & (frame["status"] == RED)]

    events = []
    for _, row in window.iterrows():
        events.append({
            "time": row["time"],
            "altitude_m": int(row["altitude_m"]),
            "label": row["label"],
            "climbing": bool(row["climbing"]),
            "wind_speed_kmh": row["wind_speed_kmh"],
            "wind_chill_c": row["wind_chill_c"],
            "reasons": [r for r in band_reasons(row, cfg) if r.startswith("RED")],
        })
    return events


def overall_status(frame: pd.DataFrame, hours: int = 24) -> str:
    """Worst status across all bands within the next `hours`."""
    if frame.empty:
        return GREEN
    horizon = frame["time"].min() + timedelta(hours=hours)
    window = frame[frame["time"] <= horizon]
    if window.empty:
        return GREEN
    return max(window["status"], key=severity_rank)
