"""Open-Meteo client: pressure-level + surface forecast for a single point.

No API key is required. The free endpoint is rate-limited, so requests are retried
with a linear backoff and the raw payload is handed back untouched for the
interpolation layer to reshape.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

LOG = logging.getLogger("manaslu.fetch")

API_URL = "https://api.open-meteo.com/v1/forecast"

# Per pressure level, requested for every level in config.forecast.pressure_levels_hpa
LEVEL_VARIABLES = (
    "wind_speed",
    "wind_direction",
    "temperature",
    "relative_humidity",
    "geopotential_height",
)

# Column / surface context variables
SURFACE_VARIABLES = (
    "precipitation",
    "snowfall",
    "freezing_level_height",
    "wind_gusts_10m",
    "cloud_cover",
    "cape",
    "pressure_msl",
    "weather_code",
)


class FetchError(RuntimeError):
    """Raised when the forecast could not be retrieved after all retries."""


def build_hourly_params(levels_hpa: List[int]) -> List[str]:
    """Compose the `hourly=` variable list: every level variable, then surface ones."""
    names: List[str] = []
    for level in levels_hpa:
        for var in LEVEL_VARIABLES:
            names.append("{}_{}hPa".format(var, level))
    names.extend(SURFACE_VARIABLES)
    return names


def fetch_forecast(cfg: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    """Fetch one model's forecast. Returns the decoded Open-Meteo JSON."""
    loc = cfg["location"]
    fc = cfg["forecast"]
    models = [model] if model else list(fc["models"])

    params = {
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "hourly": ",".join(build_hourly_params(fc["pressure_levels_hpa"])),
        "models": ",".join(models),
        "timezone": "UTC",
        "forecast_days": fc["forecast_days"],
        "wind_speed_unit": "kmh",
    }

    retries = int(fc.get("retries", 3))
    backoff = float(fc.get("retry_backoff_seconds", 5))
    timeout = float(fc.get("timeout_seconds", 30))
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            LOG.info("Fetching %s forecast (attempt %d/%d)", models, attempt, retries)
            response = httpx.get(API_URL, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if "hourly" not in payload or "time" not in payload.get("hourly", {}):
                raise FetchError("Response contained no hourly block: {}".format(
                    payload.get("reason", "unknown")))
            payload["_fetched_at"] = datetime.now(timezone.utc).isoformat()
            payload["_model"] = models[0]
            LOG.info(
                "Got %d hourly steps for %.4f,%.4f (grid elevation %.0f m)",
                len(payload["hourly"]["time"]),
                payload.get("latitude", 0.0),
                payload.get("longitude", 0.0),
                payload.get("elevation", 0.0),
            )
            return payload
        except (httpx.HTTPError, FetchError, ValueError) as exc:
            last_error = exc
            LOG.warning("Fetch attempt %d failed: %s", attempt, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)

    raise FetchError("Open-Meteo fetch failed after {} attempts: {}".format(retries, last_error))


def fetch_cross_check(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fetch the optional second model. Never fatal — returns None on failure."""
    model = cfg["forecast"].get("cross_check_model")
    if not model:
        return None
    try:
        return fetch_forecast(cfg, model=model)
    except FetchError as exc:
        LOG.warning("Cross-check model %s unavailable: %s", model, exc)
        return None
