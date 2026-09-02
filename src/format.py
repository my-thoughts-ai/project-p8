"""Render forecasts into email HTML/text, SMS text and Telegram messages.

The SMS path is the constrained one: Twilio bills per 160-character GSM-7 segment and
a German destination is roughly EUR 0.075 a segment, so the text is compressed to a
headline plus the worst band and capped at a configured segment count. Any non-GSM
character (including every emoji) forces UCS-2 encoding, which halves the segment to
70 characters -- so the SMS text is ASCII-only by construction.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import DISCLAIMER, REPO_ROOT
from .metrics import GREEN, AMBER, RED, band_reasons, severity_rank

LOG = logging.getLogger("manaslu.format")

NEPAL_OFFSET = timedelta(hours=5, minutes=45)

ROW_BG = {GREEN: "#f2f9f3", AMBER: "#fff8e8", RED: "#fdecea"}
BADGE = {GREEN: "#2e8b57", AMBER: "#d68910", RED: "#c0392b"}
HEADER = {GREEN: "#2e8b57", AMBER: "#d68910", RED: "#c0392b"}

GSM7_SAFE = set(
    "@$\n\r_ !\"#%&'()*+,-./0123456789:;<=>?"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def to_nepal(timestamp: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(timestamp) + NEPAL_OFFSET


def _fmt(value: Optional[float], spec: str = "{:.0f}", suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return spec.format(float(value)) + suffix


def compass(degrees: Optional[float]) -> str:
    if degrees is None or pd.isna(degrees):
        return "n/a"
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return points[int((float(degrees) % 360) / 22.5 + 0.5) % 16]


def sms_segments(text: str) -> int:
    """Segment count Twilio will bill for this text."""
    if not text:
        return 0
    unicode_needed = any(ch not in GSM7_SAFE for ch in text)
    if unicode_needed:
        return 1 if len(text) <= 70 else -(-len(text) // 67)
    return 1 if len(text) <= 160 else -(-len(text) // 153)


def build_context(frame: pd.DataFrame, cfg: Dict[str, Any], payload: Dict[str, Any],
                  windows: List[Dict[str, Any]], warnings: List[str],
                  title: str, cross_check: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble everything the email template and text renderers need."""
    loc = cfg["location"]
    issued = frame["time"].min()
    overall = max(frame["status"], key=severity_rank) if not frame.empty else GREEN

    blocks = []
    for hours in cfg["digest"]["table_hours"]:
        stamp = issued + timedelta(hours=int(hours))
        slice_ = frame[frame["time"] == stamp]
        if slice_.empty:
            continue
        rows = []
        for _, row in slice_.iterrows():
            rows.append({
                "altitude": int(row["altitude_m"]),
                "label": row["label"],
                "temp": _fmt(row["temperature_c"], "{:.1f}", " C"),
                "wind": _fmt(row["wind_speed_kmh"], "{:.0f}", " km/h"),
                "direction": compass(row["wind_direction_deg"]),
                "chill": _fmt(row["wind_chill_c"], "{:.0f}", " C"),
                "humidity": _fmt(row["relative_humidity_pct"], "{:.0f}", "%"),
                "status": row["status"].upper(),
                "bg": ROW_BG.get(row["status"], "#fff"),
                "badge": BADGE.get(row["status"], "#6b7076"),
            })
        first = slice_.iloc[0]
        blocks.append({
            "heading": "+{}h  {} UTC".format(hours, stamp.strftime("%a %d %b %H:%M")),
            "local": to_nepal(stamp).strftime("%H:%M"),
            "rows": rows,
            "precip": _fmt(first.get("precipitation"), "{:.1f}"),
            "snow": _fmt(first.get("snowfall"), "{:.1f}"),
            "gusts": _fmt(first.get("wind_gusts_10m")),
            "freezing": _fmt(first.get("freezing_level_height")),
            "mslp": _fmt(first.get("pressure_msl"), "{:.1f}"),
            "mslp_trend": _fmt(first.get("mslp_change_6h"), "{:+.1f}"),
            "cape": _fmt(first.get("cape")),
        })

    window_rows = [{
        "start": w["start"].strftime("%a %d %b %H:%M"),
        "end": w["end"].strftime("%a %d %b %H:%M"),
        "hours": w["hours"],
        "max_wind": "{:.0f}".format(w["max_wind_kmh"]),
        "min_chill": _fmt(w["min_wind_chill_c"], "{:.0f}", " C"),
    } for w in windows]

    return {
        "title": title,
        "location_name": loc["name"],
        "latitude": payload.get("latitude", loc["latitude"]),
        "longitude": payload.get("longitude", loc["longitude"]),
        "model": payload.get("_model", "gfs_seamless"),
        "issued_utc": issued.strftime("%Y-%m-%d %H:%M"),
        "issued_local": to_nepal(issued).strftime("%Y-%m-%d %H:%M"),
        "headline": headline(frame, windows, overall),
        "overall": overall,
        "header_color": HEADER.get(overall, "#2e8b57"),
        "warnings": warnings,
        "windows": window_rows,
        "blocks": blocks,
        "lookahead_days": round(cfg["summit_window"]["lookahead_hours"] / 24),
        "min_window_hours": cfg["summit_window"]["min_consecutive_hours"],
        "levels": ", ".join(str(l) for l in cfg["forecast"]["pressure_levels_hpa"]),
        "cross_check": cross_check,
        "disclaimer": DISCLAIMER,
    }


def headline(frame: pd.DataFrame, windows: List[Dict[str, Any]], overall: str) -> str:
    """One sentence: worst condition in 24 h plus the next usable window."""
    if frame.empty:
        return "No forecast data available."
    horizon = frame["time"].min() + timedelta(hours=24)
    day = frame[frame["time"] <= horizon]
    climbing = day[day["climbing"]]
    peak_wind = climbing["wind_speed_kmh"].max() if not climbing.empty else 0.0
    coldest = climbing["wind_chill_c"].min() if not climbing.empty else None

    status_24h = max(day["status"], key=severity_rank) if not day.empty else GREEN

    parts = ["Next 24 h {} on climbing bands: peak wind {:.0f} km/h".format(
        status_24h.upper(), peak_wind or 0)]
    if coldest is not None and pd.notna(coldest):
        parts.append("wind chill down to {:.0f} C".format(coldest))
    if windows:
        first = windows[0]
        parts.append("next climbable window {} UTC for {} h".format(
            first["start"].strftime("%a %d %b %H:%M"), first["hours"]))
    else:
        parts.append("no qualifying summit window in range")
    return "; ".join(parts) + "."


def render_email_html(context: Dict[str, Any]) -> str:
    return _env().get_template("email.html.j2").render(**context)


def render_email_text(context: Dict[str, Any]) -> str:
    """Plain-text fallback. Kept genuinely readable, not a tag-stripped HTML dump."""
    lines = [
        context["title"],
        "{} ({}, {}) - model {}".format(
            context["location_name"], context["latitude"], context["longitude"],
            context["model"]),
        "Issued {} UTC / {} Nepal".format(context["issued_utc"], context["issued_local"]),
        "",
        context["headline"],
        "",
    ]
    if context["warnings"]:
        lines.append("ACTIVE WARNINGS")
        lines.extend("  - " + w for w in context["warnings"])
        lines.append("")

    lines.append("SUMMIT WINDOWS")
    if context["windows"]:
        for w in context["windows"]:
            lines.append("  {} -> {} UTC  ({} h, max wind {} km/h, min chill {})".format(
                w["start"], w["end"], w["hours"], w["max_wind"], w["min_chill"]))
    else:
        lines.append("  none of {}+ consecutive hours".format(context["min_window_hours"]))
    lines.append("")

    for block in context["blocks"]:
        lines.append(block["heading"] + "  (" + block["local"] + " Nepal)")
        lines.append("  {:<10} {:>9} {:>11} {:>6} {:>11} {:>6}  {}".format(
            "ALTITUDE", "TEMP", "WIND", "DIR", "CHILL", "RH", "STATUS"))
        for row in block["rows"]:
            lines.append("  {:<10} {:>9} {:>11} {:>6} {:>11} {:>6}  {}".format(
                "{} m".format(row["altitude"]), row["temp"], row["wind"],
                row["direction"], row["chill"], row["humidity"], row["status"]))
        lines.append("  surface: precip {} mm/h, snow {} mm/h, gusts {} km/h, "
                     "freezing level {} m, MSLP {} hPa ({}/6h), CAPE {}".format(
                         block["precip"], block["snow"], block["gusts"], block["freezing"],
                         block["mslp"], block["mslp_trend"], block["cape"]))
        lines.append("")

    if context.get("cross_check"):
        lines.append("Model cross-check ({}): {}".format(
            context["cross_check"]["model"], context["cross_check"]["summary"]))
        lines.append("")

    lines.append("--")
    lines.append(context["disclaimer"])
    lines.append("Altitudes interpolated from {} hPa levels via geopotential height. "
                 "Source: Open-Meteo.".format(context["levels"]))
    return "\n".join(lines)


def render_telegram(context: Dict[str, Any]) -> str:
    """Telegram HTML-parse-mode message. Free and unlimited, so it carries the full table."""
    icon = {GREEN: "OK", AMBER: "CAUTION", RED: "DANGER"}[context["overall"]]
    lines = ["<b>{}</b>".format(context["title"]),
             "<i>{} - {} UTC - {}</i>".format(
                 context["location_name"], context["issued_utc"], icon), ""]
    if context["warnings"]:
        lines.append("<b>Warnings</b>")
        lines.extend("- " + w for w in context["warnings"])
        lines.append("")
    lines.append(context["headline"])
    lines.append("")
    lines.append("<b>Summit windows</b>")
    if context["windows"]:
        for w in context["windows"]:
            lines.append("- {} UTC, {} h, max wind {} km/h".format(
                w["start"], w["hours"], w["max_wind"]))
    else:
        lines.append("- none of {}+ h".format(context["min_window_hours"]))
    lines.append("")
    for block in context["blocks"][:4]:
        lines.append("<b>{}</b>".format(block["heading"]))
        lines.append("<pre>" + "\n".join(
            "{:<7} {:>7} {:>9} {:>4} {}".format(
                str(r["altitude"]) + "m", r["temp"], r["wind"], r["direction"], r["status"])
            for r in block["rows"]) + "</pre>")
    lines.append("")
    lines.append("<i>{}</i>".format(context["disclaimer"]))
    return "\n".join(lines)


def render_sms(frame: pd.DataFrame, cfg: Dict[str, Any], context: Dict[str, Any],
               kind: str = "digest") -> str:
    """Compress to headline status + worst band + top warning, ASCII only.

    Truncated to the configured segment budget rather than sent long: an unnoticed
    3-segment message is a recurring charge, not a one-off.
    """
    max_segments = int(cfg["channels"]["sms"].get("max_segments", 2))
    prefix = "MANASLU" if kind == "digest" else "MANASLU ALERT"
    overall = context["overall"].upper()

    horizon = frame["time"].min() + timedelta(hours=24)
    day = frame[(frame["time"] <= horizon) & frame["climbing"]]

    body = "{} {} {}".format(prefix, context["issued_utc"][5:], overall)
    if not day.empty:
        worst_row = day.loc[day["wind_speed_kmh"].idxmax()]
        body += ". Worst {}m: wind {:.0f}km/h".format(
            int(worst_row["altitude_m"]), worst_row["wind_speed_kmh"])
        if pd.notna(worst_row["wind_chill_c"]):
            body += " chill {:.0f}C".format(worst_row["wind_chill_c"])
        body += " {}".format(pd.Timestamp(worst_row["time"]).strftime("%d %H%MZ"))

    if context["warnings"]:
        body += ". " + context["warnings"][0]

    if context["windows"]:
        first = context["windows"][0]
        body += ". Window {} {}h".format(first["start"], first["hours"])
    else:
        body += ". No window"

    body += ". Not a go/no-go tool."
    body = "".join(ch if ch in GSM7_SAFE else " " for ch in body)
    body = " ".join(body.split())

    limit = 153 * max_segments if max_segments > 1 else 160
    if len(body) > limit:
        body = body[:limit - 3].rstrip() + "..."
    LOG.info("SMS built: %d chars, %d segment(s)", len(body), sms_segments(body))
    return body


def warning_lines(events: List[Dict[str, Any]], cfg: Dict[str, Any],
                  limit: int = 8) -> List[str]:
    """Collapse red events into readable warning lines.

    Wind and wind chill are genuinely per-altitude, so they are reported per band.
    Precipitation, snowfall, surface gusts, CAPE and pressure are column-wide -- the
    same value is attached to every band -- so reporting them five times is noise.
    Those are de-duplicated into a single line each.
    """
    band_level = ("wind", "chill")
    per_band: Dict[int, Dict[str, List[Any]]] = {}
    column: Dict[str, List[Any]] = {}

    for event in events:
        for reason in event["reasons"]:
            text = reason.replace("RED: ", "")
            key = "chill" if text.startswith("wind chill") else text.split()[0]
            if key in band_level:
                slot = per_band.setdefault(event["altitude_m"], {})
                slot.setdefault(key, []).append(event)
            else:
                column.setdefault(key, []).append(event["time"])

    lines: List[str] = []
    for altitude in sorted(per_band, reverse=True):
        for key, group in sorted(per_band[altitude].items()):
            group = sorted(group, key=lambda e: e["time"])
            peak = max(group, key=lambda e: (e["wind_speed_kmh"] or 0)
                       if key == "wind" else -(e["wind_chill_c"] or 0))
            detail = ("peak {:.0f} km/h".format(peak["wind_speed_kmh"] or 0)
                      if key == "wind"
                      else "down to {:.0f} C".format(peak["wind_chill_c"] or 0))
            lines.append("{} m ({}): {} {} from {} UTC, {} h".format(
                altitude, group[0]["label"],
                "wind" if key == "wind" else "wind chill", detail,
                pd.Timestamp(group[0]["time"]).strftime("%a %d %b %H:%M"),
                len({pd.Timestamp(e["time"]) for e in group})))

    for key, times in sorted(column.items()):
        stamps = sorted(set(pd.Timestamp(t) for t in times))
        lines.append("All bands: {} from {} UTC, {} h".format(
            key, stamps[0].strftime("%a %d %b %H:%M"), len(stamps)))

    return lines[:limit]
