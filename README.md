# Manaslu High-Altitude Forecast Alerter

Pulls free high-altitude weather forecasts for **Mt. Manaslu** (8,163 m), interpolates them to
five climbing altitude bands, evaluates climbing-relevant thresholds, and delivers a
**twice-daily digest** plus **immediate danger warnings** by email, Telegram and SMS.

> ### Scope and honesty note
> This is a **monitoring and alerting tool** built on free forecast-model data. It is **not a
> summit go/no-go decision tool**. Real expeditions must use a dedicated mountain forecaster.
> This disclaimer appears in every message the service sends.

To get it running, see **[HOWTORUN.md](HOWTORUN.md)**.

---

## What it does

| | |
|---|---|
| **Point** | 28.55 N, 84.56 E (near summit) |
| **Bands** | 4,700 m (Base Camp), 5,700 m (C1), 6,900 m (C3), 7,400 m (C4), 8,100 m (Summit) |
| **Source** | [Open-Meteo](https://open-meteo.com) GFS, no API key, cross-checked against ECMWF IFS |
| **Digest** | Twice daily, 01:00 and 13:00 UTC (06:45 / 18:45 Nepal) |
| **Danger check** | Hourly, alerts only on new or worsening red conditions |
| **Horizon** | 7 days hourly |

### The three things that make it more than a weather scrape

**1. Real vertical interpolation.** GFS has ~25 km horizontal resolution, so base camp and the
summit fall in the *same grid cell* — asking for a different lat/lon changes nothing. The
vertical differentiation comes from pressure levels (600–300 hPa), interpolated onto the target
altitudes using each forecast hour's **returned geopotential height**. That matters: on the day
this was built, 350 hPa sat at 8,635 m, not the textbook ~8,100 m. Using the static table would
have reported the summit's weather from 500 m too high.

**2. Wind interpolated as vectors.** Wind directions are converted to u/v components before
interpolation and recombined afterwards. Averaging degrees directly turns a steady northerly
(359° and 1°) into a southerly — a 180° error, silently.

**3. De-duplicated alerting.** The hourly danger check fingerprints the active hazard set and
stores it in SQLite. An unchanged storm is not re-sent every hour; a *new or worsening* hazard
breaks through immediately, an unchanged one repeats once every 12 h, and one all-clear is sent
when red lifts. The fingerprint ignores sub-km/h model wobble so re-runs do not look like new
hazards.

### Summit windows

The actual value-add: rather than reporting conditions, the service scans the forecast for runs
of **6+ consecutive hours where every climbing band is simultaneously green with no
precipitation**, and reports each window with its worst wind and coldest wind chill.

## Metrics and thresholds

Wind chill uses the NWS formula (`13.12 + 0.6215T − 11.37V^0.16 + 0.3965·T·V^0.16`), applied
only in its valid range (T ≤ 10 °C, V ≥ 4.8 km/h); outside it the air temperature is reported
unchanged rather than fabricated.

| Parameter | Green | Amber | Red |
|---|---|---|---|
| Wind speed (climbing band) | < 30 km/h | 30–50 | > 50 |
| Wind gusts (surface) | < 40 | 40–60 | > 60 |
| Snowfall | 0 | > 0–10 mm/24h | > 10 mm/24h or > 2 mm/h |
| Precipitation | 0 | trace | sustained |
| Wind chill | > −40 °C | −40 to −50 | < −50 °C |
| MSLP trend | steady | slow fall | > 6 hPa drop / 6 h |
| CAPE | ~0 | elevated | high |

All of these live in `config.yaml` — change them there, not in code.

## Layout

```
config.yaml              point, bands, thresholds, schedule, channel toggles
.env                     secrets (gitignored; copy .env.example)
src/fetch.py             Open-Meteo client
src/interpolate.py       geopotential-height interpolation
src/metrics.py           wind chill, thresholds, summit windows
src/format.py            email HTML/text, SMS, Telegram rendering
src/alerts.py            channel implementations
src/store.py             SQLite history + alert de-duplication
src/main.py              orchestrator (--mode digest|danger|test|status)
tests/                   95 tests, fully offline
deploy/                  systemd units + installer
.github/workflows/       cron: digest, danger, keepalive, tests
```

## Channels

All channels ship **disabled**; `console` is the default so the service runs end to end with no
credentials. Enable one in `config.yaml` only after its secrets are in `.env`.

- **Email** — Gmail SMTP, colour-coded HTML table with plain-text fallback.
- **Telegram** — free, unlimited length, recommended as the SMS replacement.
- **SMS** — Twilio. Billed **per 160-char segment** (~€0.075/segment to +49), so the message is
  compressed to headline + worst band + top warning, forced to ASCII (one emoji halves the
  segment to 70 chars and doubles the bill), and hard-capped at 2 segments.
  **SMS stops after 2026-09-10** (`channels.sms.active_until`), because the destination number
  changes after that date. Email and Telegram have no expiry and run indefinitely.

## Tests

```bash
python -m pytest tests/ -q      # 95 tests, no network required
```

The suite runs against a captured real Open-Meteo response in `tests/fixtures/`, and covers
wind-vector round-trips and the 359°/1° wrap, geopotential bracketing, wind chill against NWS
reference values, threshold boundaries, summit-window splitting, alert de-duplication
(new / worsening / reminder / all-clear), SMS segment budget and the disclaimer's presence in
every channel.

## Publishing this repository

Safe to make public as-is. No contact detail or credential is committed: recipients resolve from
`MANASLU_EMAIL_TO` / `MANASLU_SMS_TO` at runtime and `config.yaml` holds only the variable names.
Recipients are masked in all log output, since Actions logs are world-readable on a public repo.

```bash
./scripts/preflight_public.sh    # scans the tree and git history; also runs in CI
```

## Data source

Forecasts from [Open-Meteo](https://open-meteo.com/), free for non-commercial use, no API key.
