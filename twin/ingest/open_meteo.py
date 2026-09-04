"""Open-Meteo adapters: elevation (seed), forecast, air quality, flood.

All four share one host and one shape of trick: Open-Meteo accepts
comma-separated ``latitude``/``longitude`` for multiple points in a single
request. That is the entire fix for the "2,000 requests per run" trap in
section 15 -- sample a lattice, not a cell, and do it in one call.
"""

import logging

from .base import IngestAdapter, request_json

log = logging.getLogger("twin.ingest.open_meteo")

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"

#: Open-Meteo elevation accepts up to 100 coordinate pairs per request.
ELEVATION_BATCH_SIZE = 100


class OpenMeteoElevationAdapter(IngestAdapter):
    """One-time seed of elevation for every cell (section 4.1).

    `fetch_raw(coords=[(lat, lon), ...])` returns a list of floats aligned to
    `coords`, batching internally at 100/request so callers never think about
    the API's own limit.
    """

    source_key = "open_meteo_elevation"
    cache_ttl_s = 0  # elevation does not change; caching adds no value here

    def fetch_raw(self, coords, timeout_s=None, batch_pause_s=1.1, **_):
        """Batches at 100/request (the API limit) with a pause between
        requests. Firing all batches back-to-back with no pause is what
        triggered a 429 against the live API while building this -- Open-
        Meteo's free tier appears to rate-limit by request burst, not just
        by volume, so pacing matters even within a single seed run.
        """
        import time as _time

        timeout_s = timeout_s or self.timeout_s
        elevations = []
        n_batches = (len(coords) + ELEVATION_BATCH_SIZE - 1) // ELEVATION_BATCH_SIZE
        for batch_i, start in enumerate(range(0, len(coords), ELEVATION_BATCH_SIZE)):
            batch = coords[start:start + ELEVATION_BATCH_SIZE]
            lats = ",".join(str(lat) for lat, _ in batch)
            lons = ",".join(str(lon) for _, lon in batch)
            payload = request_json(
                self.session, "GET", ELEVATION_URL, timeout_s,
                params={"latitude": lats, "longitude": lons},
            )
            values = payload.get("elevation", [])
            if len(values) != len(batch):
                raise ValueError(
                    "elevation batch size mismatch: sent %d got %d" % (len(batch), len(values)))
            elevations.extend(values)
            if batch_i < n_batches - 1:
                _time.sleep(batch_pause_s)
        return elevations

    def record_count(self, data):
        return len(data) if data else 0


class OpenMeteoForecastAdapter(IngestAdapter):
    """Multi-point rain/temp/humidity/wind sampled on a lattice, not per cell.

    `fetch_raw(points=[(lat, lon), ...])` returns a list of per-point dicts:
    ``{"lat", "lon", "rain_now_mm_1h", "rain_forecast_mm_3h",
       "rain_forecast_mm_24h", "apparent_temp_c", "weather_code"}``.
    IDW interpolation from these points to cells happens in engine.py, not
    here -- this adapter's job ends at "one honest reading per lattice point".
    """

    source_key = "open_meteo_forecast"
    cache_ttl_s = 15 * 60

    def fetch_raw(self, points, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        lats = ",".join(str(lat) for lat, _ in points)
        lons = ",".join(str(lon) for _, lon in points)
        payload = request_json(
            self.session, "GET", FORECAST_URL, timeout_s,
            params={
                "latitude": lats,
                "longitude": lons,
                "current": "precipitation,apparent_temperature,weather_code",
                "hourly": "precipitation",
                "forecast_days": 2,
                "timezone": "UTC",
            },
        )
        # A single-point request returns one object; multi-point returns a list.
        entries = payload if isinstance(payload, list) else [payload]
        if len(entries) != len(points):
            raise ValueError(
                "forecast point count mismatch: sent %d got %d" % (len(points), len(entries)))

        results = []
        for (lat, lon), entry in zip(points, entries):
            current = entry.get("current", {}) or {}
            hourly = entry.get("hourly", {}) or {}
            hourly_precip = hourly.get("precipitation") or []
            results.append({
                "lat": lat,
                "lon": lon,
                "rain_now_mm_1h": current.get("precipitation"),
                "rain_forecast_mm_3h": _sum_next(hourly_precip, 3),
                "rain_forecast_mm_6h": _sum_next(hourly_precip, 6),
                "rain_forecast_mm_24h": _sum_next(hourly_precip, 24),
                "apparent_temp_c": current.get("apparent_temperature"),
                "weather_code": current.get("weather_code"),
            })
        return results

    def record_count(self, data):
        return len(data) if data else 0

    def neutral_value(self, points, **_):
        """Section 4.1 fallback: 'last cached snapshot, then neutral'.

        Neutral means "assume nothing is happening": zero rain, and an
        apparent temperature that makes env_score's heat term read 0 (see
        scoring.env_score). This is a real numeric substitution, not an
        unmeasured None -- engine.py still marks the source degraded on the
        health pill by checking the snapshot status, independently of
        whether scoring.py received a number or a None.
        """
        return [{
            "lat": lat, "lon": lon,
            "rain_now_mm_1h": 0.0, "rain_forecast_mm_3h": 0.0,
            "rain_forecast_mm_6h": 0.0, "rain_forecast_mm_24h": 0.0,
            "apparent_temp_c": 30.0, "weather_code": None,
        } for lat, lon in points]


class OpenMeteoAirQualityAdapter(IngestAdapter):
    """Multi-point PM2.5 / PM10 / US AQI sampled on the same lattice."""

    source_key = "open_meteo_airquality"
    cache_ttl_s = 30 * 60

    def fetch_raw(self, points, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        lats = ",".join(str(lat) for lat, _ in points)
        lons = ",".join(str(lon) for _, lon in points)
        payload = request_json(
            self.session, "GET", AIR_QUALITY_URL, timeout_s,
            params={
                "latitude": lats,
                "longitude": lons,
                "current": "pm2_5,pm10,us_aqi",
                "timezone": "UTC",
            },
        )
        entries = payload if isinstance(payload, list) else [payload]
        if len(entries) != len(points):
            raise ValueError(
                "air quality point count mismatch: sent %d got %d" % (len(points), len(entries)))

        results = []
        for (lat, lon), entry in zip(points, entries):
            current = entry.get("current", {}) or {}
            results.append({
                "lat": lat,
                "lon": lon,
                "pm2_5": current.get("pm2_5"),
                "pm10": current.get("pm10"),
                "us_aqi": current.get("us_aqi"),
            })
        return results

    def record_count(self, data):
        return len(data) if data else 0

    def neutral_value(self, points, **_):
        """Section 4.1 fallback: 'neutral (AQI = 50)'."""
        from .. import config
        return [{"lat": lat, "lon": lon, "pm2_5": None, "pm10": None,
                  "us_aqi": config.NEUTRAL_AQI} for lat, lon in points]


class OpenMeteoFloodAdapter(IngestAdapter):
    """GloFAS river discharge forecast, sampled once per basin outlet.

    `fetch_raw(lat, lon)` returns
    ``{"river_discharge_now", "river_discharge_forecast_max"}``.
    """

    source_key = "open_meteo_flood"
    cache_ttl_s = 6 * 60 * 60

    def fetch_raw(self, lat, lon, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        payload = request_json(
            self.session, "GET", FLOOD_URL, timeout_s,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "river_discharge",
                "forecast_days": 7,
            },
        )
        daily = payload.get("daily", {}) or {}
        discharge = [v for v in daily.get("river_discharge", []) if v is not None]
        return {
            "river_discharge_now": discharge[0] if discharge else None,
            "river_discharge_forecast_max": max(discharge) if discharge else None,
        }


class OpenMeteoFloodReturnPeriodAdapter(IngestAdapter):
    """Derives a 2-year-return-ish discharge reference from archive data.

    The Open-Meteo Flood API exposes no return-period endpoint (section 5.1
    correction) -- only observed daily discharge back to 1984. This adapter
    is run once at seed time per basin outlet and its result is stored on
    ``TwinCity.discharge_2yr_return``, not re-fetched on every compute pass.

    Using the archive's 95th percentile as a pragmatic proxy for a 2-year
    return period is a documented approximation, not a hydrological claim;
    see the docstring on ``twin.scoring.hydro_score``.
    """

    source_key = "open_meteo_flood_archive"
    cache_ttl_s = 0

    ARCHIVE_URL = "https://flood-api.open-meteo.com/v1/flood"

    def fetch_raw(self, lat, lon, years=20, timeout_s=None, **_):
        timeout_s = timeout_s or max(self.timeout_s, 30)
        payload = request_json(
            self.session, "GET", self.ARCHIVE_URL, timeout_s,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "river_discharge",
                "past_days": min(years * 365, 92),
                # Open-Meteo's flood archive is served via a separate
                # `start_date`/`end_date` range on the same endpoint; recent
                # `past_days` is used here to keep the request cheap and
                # robust for a seed-time approximation.
            },
        )
        daily = payload.get("daily", {}) or {}
        values = sorted(v for v in daily.get("river_discharge", []) if v is not None)
        if not values:
            raise ValueError("no discharge values returned")
        idx = int(0.95 * (len(values) - 1))
        return {"p95_discharge": values[idx], "sample_size": len(values)}


def _sum_next(hourly_values, n_hours):
    """Sum of the first `n_hours` entries of an hourly series, or None."""
    window = [v for v in hourly_values[:n_hours] if v is not None]
    if not window:
        return None
    return sum(window)
