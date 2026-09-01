"""Orchestrator for the Manaslu forecast alerter.

Modes
  digest  -- twice daily: full per-altitude report + summit windows, to all channels.
  danger  -- hourly: evaluate the next N hours, alert only on new/worsening red
             conditions (de-duplicated in SQLite), plus one all-clear when red lifts.
  test    -- real fetch, console output only, nothing sent and nothing stored.
  status  -- print recent run/alert history from the database.

Exit codes: 0 success, 1 handled failure (fetch or dispatch), 2 unexpected error.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .alerts import dispatch, summarise
from .config import DISCLAIMER, load_config, load_env, setup_logging
from .fetch import FetchError, fetch_cross_check, fetch_forecast
from .format import (build_context, render_email_html, render_email_text,
                     render_sms, render_telegram, sms_segments, warning_lines)
from .interpolate import interpolate_bands
from .metrics import GREEN, RED, evaluate, overall_status, red_events, summit_windows
from .store import Store, fingerprint_events, should_send


def load_pipeline(cfg: Dict[str, Any], log, fixture: Optional[str] = None):
    """Fetch (or load a fixture) and run it through interpolation and metrics."""
    if fixture:
        log.info("Loading fixture %s (no network)", fixture)
        payload = json.loads(Path(fixture).read_text(encoding="utf-8"))
    else:
        payload = fetch_forecast(cfg)

    frame = interpolate_bands(payload, cfg)
    if frame.empty:
        raise FetchError("Interpolation produced no rows")
    return payload, evaluate(frame, cfg)


def cross_check_summary(cfg: Dict[str, Any], frame: pd.DataFrame, log
                        ) -> Optional[Dict[str, Any]]:
    """Compare the primary model's summit-band wind against a second model.

    Purely informational: a large spread between GFS and ECMWF is itself the signal
    that the forecast is low-confidence, which matters more than either number.
    """
    payload = fetch_cross_check(cfg)
    if not payload:
        return None
    try:
        other = evaluate(interpolate_bands(payload, cfg), cfg)
    except Exception as exc:
        log.warning("Cross-check interpolation failed: %s", exc)
        return None
    if other.empty:
        return None

    top = max(band["altitude_m"] for band in cfg["altitude_bands"])
    horizon = frame["time"].min() + pd.Timedelta(hours=24)

    def peak(df: pd.DataFrame) -> float:
        window = df[(df["altitude_m"] == top) & (df["time"] <= horizon)]
        return float(window["wind_speed_kmh"].max()) if not window.empty else float("nan")

    primary, secondary = peak(frame), peak(other)
    if pd.isna(primary) or pd.isna(secondary):
        return None
    spread = abs(primary - secondary)
    confidence = "good agreement" if spread < 10 else (
        "moderate spread" if spread < 25 else "LOW CONFIDENCE - models disagree")
    return {
        "model": payload.get("_model", cfg["forecast"]["cross_check_model"]),
        "summary": "peak {} m wind next 24 h: {:.0f} km/h vs {:.0f} km/h primary "
                   "(spread {:.0f} km/h, {}).".format(
                       top, secondary, primary, spread, confidence),
    }


def run_digest(cfg: Dict[str, Any], log, args) -> int:
    payload, frame = load_pipeline(cfg, log, args.fixture)
    windows = summit_windows(frame, cfg)
    events = red_events(frame, cfg, cfg["danger_mode"]["lookahead_hours"])
    warnings = warning_lines(events, cfg)

    cross = None if (args.fixture or args.no_cross_check) else cross_check_summary(cfg, frame, log)
    overall = overall_status(frame, 24)
    title = "Manaslu forecast digest - {}".format(overall.upper())
    context = build_context(frame, cfg, payload, windows, warnings, title, cross)

    message = {
        "subject": "{} - {} UTC".format(title, context["issued_utc"]),
        "text": render_email_text(context),
        "html": render_email_html(context),
        "telegram": render_telegram(context),
        "sms": render_sms(frame, cfg, context, kind="digest"),
    }
    log.info("SMS is %d segment(s)", sms_segments(message["sms"]))

    results = dispatch(cfg, message, dry_run=args.dry_run)
    log.info("Dispatch: %s", summarise(results))

    if not args.no_store:
        with Store(cfg["storage"]["db_path"]) as store:
            store.save_forecast(frame, payload.get("_model", "gfs_seamless"),
                                payload.get("_fetched_at"))
            store.prune(int(cfg["storage"]["retain_days"]))
            store.log_alert("digest", None,
                            [name for name, _ in results["sent"]],
                            not results["failed"], summarise(results))
            store.log_run("digest", not results["failed"], summarise(results))

    return 1 if results["failed"] else 0


def run_danger(cfg: Dict[str, Any], log, args) -> int:
    payload, frame = load_pipeline(cfg, log, args.fixture)
    lookahead = int(cfg["danger_mode"]["lookahead_hours"])
    events = red_events(frame, cfg, lookahead)
    log.info("Found %d red event row(s) in the next %d h", len(events), lookahead)

    with Store(cfg["storage"]["db_path"]) as store:
        state = store.get_alert_state()
        decision = should_send(events, state, int(cfg["danger_mode"]["reminder_after_hours"]))
        log.info("Decision: send=%s kind=%s (%s)",
                 decision["send"], decision["kind"], decision["reason"])

        store.save_forecast(frame, payload.get("_model", "gfs_seamless"),
                            payload.get("_fetched_at"))

        if not decision["send"]:
            store.log_run("danger", True, "no send: " + decision["reason"])
            return 0

        if decision["kind"] == "all_clear":
            if not cfg["danger_mode"].get("send_all_clear", True):
                store.set_alert_state(None, None)
                store.log_run("danger", True, "all clear suppressed by config")
                return 0
            title = "Manaslu ALL CLEAR - red conditions have lifted"
            warnings = ["All previously flagged red conditions have cleared from the "
                        "next {} h of forecast.".format(lookahead)]
        else:
            title = "Manaslu DANGER WARNING ({})".format(decision["kind"])
            warnings = warning_lines(events, cfg)

        windows = summit_windows(frame, cfg)
        context = build_context(frame, cfg, payload, windows, warnings, title)
        context["header_color"] = "#2e8b57" if decision["kind"] == "all_clear" else "#c0392b"

        message = {
            "subject": "{} - {} UTC".format(title, context["issued_utc"]),
            "text": render_email_text(context),
            "html": render_email_html(context),
            "telegram": render_telegram(context),
            "sms": render_sms(frame, cfg, context, kind="danger"),
        }

        results = dispatch(cfg, message, dry_run=args.dry_run)
        log.info("Dispatch: %s", summarise(results))

        if not results["failed"] and not args.dry_run:
            if decision["kind"] == "all_clear":
                store.set_alert_state(None, None)
            else:
                store.set_alert_state(decision["fingerprint"], RED,
                                      {"event_count": len(events),
                                       "warnings": warnings})
        elif results["failed"]:
            log.error("Alert state NOT advanced because a channel failed; "
                      "the next run will retry this warning.")

        store.log_alert(decision["kind"], decision["fingerprint"],
                        [name for name, _ in results["sent"]],
                        not results["failed"], summarise(results))
        store.log_run("danger", not results["failed"],
                      "{}: {}".format(decision["kind"], summarise(results)))
        return 1 if results["failed"] else 0


def run_test(cfg: Dict[str, Any], log, args) -> int:
    """Real fetch, console only. Nothing sent, nothing written to the database."""
    payload, frame = load_pipeline(cfg, log, args.fixture)
    windows = summit_windows(frame, cfg)
    events = red_events(frame, cfg, cfg["danger_mode"]["lookahead_hours"])
    context = build_context(frame, cfg, payload, windows, warning_lines(events, cfg),
                            "Manaslu forecast (test run)")
    sms = render_sms(frame, cfg, context, kind="digest")

    print(render_email_text(context))
    print("SMS preview ({} chars, {} segment(s)):".format(len(sms), sms_segments(sms)))
    print("  " + sms)
    print("\nChannel readiness:")
    from .alerts import build_channels
    for channel in build_channels(cfg):
        missing = channel.missing_secrets()
        if channel.ready:
            state = "READY"
        elif not channel.enabled:
            state = "DISABLED"
        elif channel.expired:
            state = "EXPIRED (active_until {})".format(channel.active_until)
        else:
            state = "MISSING " + ",".join(missing)
        if channel.active_until and not channel.expired:
            state += "  [sends until {} UTC]".format(channel.active_until)
        print("  {:<9} {}".format(channel.name, state))
    return 0


def run_status(cfg: Dict[str, Any], log, args) -> int:
    with Store(cfg["storage"]["db_path"]) as store:
        state = store.get_alert_state()
        print("Active warning:", dict(state) if state else "none")
        print("\nRecent runs:")
        for row in store.recent_runs(int(args.limit)):
            print("  {}  {:<7} {}  {}".format(
                row["started_at"], row["mode"], "ok " if row["ok"] else "FAIL",
                (row["detail"] or "")[:100]))
        count = store.conn.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"]
        print("\nStored forecast rows:", count)
    return 0


MODES = {"digest": run_digest, "danger": run_danger, "test": run_test, "status": run_status}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manaslu high-altitude forecast alerter. " + DISCLAIMER)
    parser.add_argument("--mode", choices=sorted(MODES), default="test")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="render and print, but send nothing but console")
    parser.add_argument("--fixture", default=None,
                        help="use a saved Open-Meteo JSON instead of the network")
    parser.add_argument("--no-store", action="store_true", help="skip database writes")
    parser.add_argument("--no-cross-check", action="store_true",
                        help="skip the second-model comparison")
    parser.add_argument("--limit", default=20, help="rows for --mode status")
    args = parser.parse_args(argv)

    load_env()
    cfg = load_config(args.config)
    log = setup_logging(cfg)
    log.info("Starting mode=%s dry_run=%s", args.mode, args.dry_run)

    try:
        return MODES[args.mode](cfg, log, args)
    except FetchError as exc:
        log.error("Forecast unavailable: %s", exc)
        try:
            with Store(cfg["storage"]["db_path"]) as store:
                store.log_run(args.mode, False, str(exc))
        except Exception:
            pass
        return 1
    except Exception as exc:
        log.error("Unexpected failure: %s\n%s", exc, traceback.format_exc())
        return 2


if __name__ == "__main__":
    sys.exit(main())
