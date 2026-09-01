"""Alert de-duplication -- the logic that stops 24 identical SMS a day."""
import json
import sqlite3
from datetime import timedelta

import pandas as pd
import pytest

from src.store import Store, fingerprint_events, iso, should_send, utcnow


@pytest.fixture
def store(tmp_path):
    with Store(str(tmp_path / "test.sqlite")) as s:
        yield s


def _event(altitude=8100, hour="2026-09-02T06:00Z", reasons=("RED: wind 62 km/h",)):
    return {"altitude_m": altitude, "time": pd.Timestamp(hour), "label": "Summit",
            "climbing": True, "wind_speed_kmh": 62.0, "wind_chill_c": -45.0,
            "reasons": list(reasons)}


def test_fingerprint_is_stable_across_calls():
    events = [_event()]
    assert fingerprint_events(events) == fingerprint_events(events)


def test_fingerprint_ignores_small_wind_wobble():
    """A 0.4 km/h difference between model runs is not a new hazard."""
    a = _event()
    b = _event()
    b["wind_speed_kmh"] = 62.4
    assert fingerprint_events([a]) == fingerprint_events([b])


def test_fingerprint_changes_when_a_new_band_goes_red():
    a = [_event(8100)]
    b = [_event(8100), _event(7400)]
    assert fingerprint_events(a) != fingerprint_events(b)


def test_fingerprint_changes_when_the_hazard_type_changes():
    a = [_event(reasons=["RED: wind 62 km/h"])]
    b = [_event(reasons=["RED: snowfall 3.0 mm/h, 12.0 mm/24h"])]
    assert fingerprint_events(a) != fingerprint_events(b)


def test_fingerprint_of_nothing_is_empty():
    assert fingerprint_events([]) == ""


def test_first_red_sends(store):
    decision = should_send([_event()], store.get_alert_state(), 12)
    assert decision["send"] and decision["kind"] == "new"


def test_identical_red_is_suppressed(store):
    events = [_event()]
    first = should_send(events, store.get_alert_state(), 12)
    store.set_alert_state(first["fingerprint"], "red", {"event_count": 1})
    second = should_send(events, store.get_alert_state(), 12)
    assert not second["send"]


def test_worsening_condition_breaks_through_suppression(store):
    first = should_send([_event(8100)], store.get_alert_state(), 12)
    store.set_alert_state(first["fingerprint"], "red", {"event_count": 1})
    second = should_send([_event(8100), _event(7400)], store.get_alert_state(), 12)
    assert second["send"] and second["kind"] == "worsening"


def test_reminder_fires_after_the_interval(store):
    events = [_event()]
    decision = should_send(events, store.get_alert_state(), 12)
    store.set_alert_state(decision["fingerprint"], "red", {"event_count": 1})
    stale = iso(utcnow() - timedelta(hours=13))
    store.conn.execute("UPDATE alert_state SET last_sent = ? WHERE id = 1", (stale,))
    store.conn.commit()
    again = should_send(events, store.get_alert_state(), 12)
    assert again["send"] and again["kind"] == "reminder"


def test_reminder_does_not_fire_early(store):
    events = [_event()]
    decision = should_send(events, store.get_alert_state(), 12)
    store.set_alert_state(decision["fingerprint"], "red", {"event_count": 1})
    stale = iso(utcnow() - timedelta(hours=6))
    store.conn.execute("UPDATE alert_state SET last_sent = ? WHERE id = 1", (stale,))
    store.conn.commit()
    assert not should_send(events, store.get_alert_state(), 12)["send"]


def test_all_clear_is_sent_once_then_silence(store):
    decision = should_send([_event()], store.get_alert_state(), 12)
    store.set_alert_state(decision["fingerprint"], "red", {"event_count": 1})

    clear = should_send([], store.get_alert_state(), 12)
    assert clear["send"] and clear["kind"] == "all_clear"

    store.set_alert_state(None, None)
    assert not should_send([], store.get_alert_state(), 12)["send"]


def test_quiet_stays_quiet(store):
    decision = should_send([], store.get_alert_state(), 12)
    assert not decision["send"] and decision["kind"] == "none"


def test_saving_the_same_forecast_twice_is_idempotent(store, frame, payload):
    stamp = payload["_fetched_at"]
    store.save_forecast(frame, "gfs_seamless", stamp)
    store.save_forecast(frame, "gfs_seamless", stamp)
    count = store.conn.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"]
    assert count == len(frame)


def test_history_tracks_successive_model_runs(store, frame):
    store.save_forecast(frame.head(5), "gfs_seamless", "2026-09-01T00:00:00+00:00")
    store.save_forecast(frame.head(5), "gfs_seamless", "2026-09-01T06:00:00+00:00")
    row = frame.iloc[0]
    history = store.history_for(int(row.altitude_m), iso(row.time))
    assert len(history) == 2


def test_prune_removes_old_rows_only(store, frame):
    store.save_forecast(frame.head(3), "gfs_seamless", iso(utcnow() - timedelta(days=200)))
    store.save_forecast(frame.head(3), "gfs_seamless", iso(utcnow()))
    store.prune(retain_days=120)
    count = store.conn.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"]
    assert count == 3


def test_run_and_alert_logs_are_written(store):
    store.log_run("digest", True, "fine")
    store.log_alert("new", "abc", ["email"], True, "ok")
    assert len(store.recent_runs()) == 1
    assert store.conn.execute("SELECT COUNT(*) c FROM alert_log").fetchone()["c"] == 1
