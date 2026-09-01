"""SQLite persistence: forecast history, run log, and alert de-duplication state.

De-duplication is the whole point of the alert table. Danger mode runs hourly; the
same storm would otherwise generate 24 identical SMS. An alert is re-sent only when
its fingerprint changes (a new or worsening hazard) or when the reminder interval
elapses; clearing a red state emits one all-clear.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

LOG = logging.getLogger("manaslu.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    fetched_at   TEXT NOT NULL,
    valid_time   TEXT NOT NULL,
    model        TEXT NOT NULL,
    altitude_m   INTEGER NOT NULL,
    temperature_c        REAL,
    wind_speed_kmh       REAL,
    wind_direction_deg   REAL,
    relative_humidity_pct REAL,
    wind_chill_c         REAL,
    precipitation        REAL,
    snowfall             REAL,
    wind_gusts_10m       REAL,
    cape                 REAL,
    pressure_msl         REAL,
    freezing_level_height REAL,
    status       TEXT,
    PRIMARY KEY (fetched_at, valid_time, model, altitude_m)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_valid ON forecasts (valid_time);

CREATE TABLE IF NOT EXISTS alert_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint  TEXT,
    severity     TEXT,
    first_sent   TEXT,
    last_sent    TEXT,
    payload      TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    sent_at      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    fingerprint  TEXT,
    channels     TEXT,
    ok           INTEGER,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    started_at   TEXT NOT NULL,
    mode         TEXT NOT NULL,
    ok           INTEGER,
    detail       TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    return value.astimezone(timezone.utc).isoformat()


class Store:
    """Thin SQLite wrapper. Safe to construct on every run."""

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- forecasts

    def save_forecast(self, frame: pd.DataFrame, model: str, fetched_at: Optional[str] = None) -> int:
        """Persist an evaluated forecast frame. Re-running the same hour is idempotent."""
        if frame.empty:
            return 0
        stamp = fetched_at or iso(utcnow())
        columns = [
            "temperature_c", "wind_speed_kmh", "wind_direction_deg",
            "relative_humidity_pct", "wind_chill_c", "precipitation", "snowfall",
            "wind_gusts_10m", "cape", "pressure_msl", "freezing_level_height",
        ]
        rows = []
        for _, row in frame.iterrows():
            values = [
                (float(row[c]) if c in frame.columns and pd.notna(row.get(c)) else None)
                for c in columns
            ]
            rows.append([stamp, iso(row["time"]), model, int(row["altitude_m"])]
                        + values + [row.get("status")])

        placeholders = ",".join(["?"] * (4 + len(columns) + 1))
        self.conn.executemany(
            "INSERT OR REPLACE INTO forecasts VALUES ({})".format(placeholders), rows)
        self.conn.commit()
        LOG.info("Stored %d forecast rows (model=%s)", len(rows), model)
        return len(rows)

    def prune(self, retain_days: int) -> int:
        cutoff = iso(utcnow() - timedelta(days=retain_days))
        cur = self.conn.execute("DELETE FROM forecasts WHERE fetched_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM alert_log WHERE sent_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def history_for(self, altitude_m: int, valid_time: str) -> List[sqlite3.Row]:
        """Every forecast run's take on one (altitude, valid time) — model run drift."""
        return list(self.conn.execute(
            "SELECT * FROM forecasts WHERE altitude_m = ? AND valid_time = ? "
            "ORDER BY fetched_at", (altitude_m, valid_time)))

    # -------------------------------------------------------------- alert state

    def get_alert_state(self) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM alert_state WHERE id = 1")
        return cur.fetchone()

    def set_alert_state(self, fingerprint: Optional[str], severity: Optional[str],
                        payload: Optional[Dict[str, Any]] = None) -> None:
        now = iso(utcnow())
        existing = self.get_alert_state()
        if fingerprint is None:
            self.conn.execute("DELETE FROM alert_state WHERE id = 1")
        elif existing and existing["fingerprint"] == fingerprint:
            self.conn.execute(
                "UPDATE alert_state SET last_sent = ?, severity = ?, payload = ? WHERE id = 1",
                (now, severity, json.dumps(payload or {}, default=str)))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO alert_state VALUES (1, ?, ?, ?, ?, ?)",
                (fingerprint, severity, now, now, json.dumps(payload or {}, default=str)))
        self.conn.commit()

    def log_alert(self, kind: str, fingerprint: Optional[str],
                  channels: List[str], ok: bool, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO alert_log VALUES (?, ?, ?, ?, ?, ?)",
            (iso(utcnow()), kind, fingerprint, ",".join(channels), int(ok), detail[:2000]))
        self.conn.commit()

    def log_run(self, mode: str, ok: bool, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?)",
            (iso(utcnow()), mode, int(ok), detail[:2000]))
        self.conn.commit()

    def recent_runs(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)))


def fingerprint_events(events: List[Dict[str, Any]]) -> str:
    """Stable hash of an active hazard set.

    Deliberately keyed on *what* is red and *which hour it starts*, not on exact wind
    values — otherwise every model run's 0.3 km/h wobble would look like a new hazard.
    """
    if not events:
        return ""
    keys = sorted({
        "{}|{}|{}".format(
            event["altitude_m"],
            pd.Timestamp(event["time"]).strftime("%Y-%m-%dT%H"),
            ";".join(sorted(r.split(":")[1].split()[0] for r in event["reasons"])),
        )
        for event in events
    })
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:32]


def should_send(events: List[Dict[str, Any]], state: Optional[sqlite3.Row],
                reminder_after_hours: int) -> Dict[str, Any]:
    """Decide whether danger mode should send, and say why.

    Returns {"send": bool, "kind": "new"|"worsening"|"reminder"|"all_clear"|"none",
             "fingerprint": str, "reason": str}
    """
    fingerprint = fingerprint_events(events)
    had_alert = state is not None and state["fingerprint"]

    if not events:
        if had_alert:
            return {"send": True, "kind": "all_clear", "fingerprint": "",
                    "reason": "previously active red condition has cleared"}
        return {"send": False, "kind": "none", "fingerprint": "",
                "reason": "no red conditions, none previously active"}

    if not had_alert:
        return {"send": True, "kind": "new", "fingerprint": fingerprint,
                "reason": "new red condition"}

    if state["fingerprint"] != fingerprint:
        previous = json.loads(state["payload"] or "{}")
        worsening = len(events) > int(previous.get("event_count", 0))
        return {"send": True, "kind": "worsening" if worsening else "new",
                "fingerprint": fingerprint,
                "reason": "hazard set changed ({} -> {} events)".format(
                    previous.get("event_count", "?"), len(events))}

    last_sent = state["last_sent"]
    if last_sent:
        age = utcnow() - datetime.fromisoformat(last_sent)
        if age >= timedelta(hours=reminder_after_hours):
            return {"send": True, "kind": "reminder", "fingerprint": fingerprint,
                    "reason": "unchanged warning still active after {:.0f} h".format(
                        age.total_seconds() / 3600)}

    return {"send": False, "kind": "none", "fingerprint": fingerprint,
            "reason": "identical warning already sent, within reminder interval"}
