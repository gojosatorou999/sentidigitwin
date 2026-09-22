"""Hourly wind, cloud, rain and heat fields on a lattice, for advection.

``open_meteo.OpenMeteoForecastAdapter`` answers "what is the weather at this
point" and collapses the hourly series into 3h/6h/24h *totals*. That is the
right shape for scoring a cell right now, and the wrong shape for asking
where a system will be in three hours: a total has no direction, and a sum
over six hours cannot say which of those hours the rain lands in.

This adapter keeps the series intact and adds the two fields the totals
never carried -- wind speed and bearing -- so ``twin/forecast.py`` can move a
rain or cloud field across the map instead of only reading it in place.

Keyless, like every other Open-Meteo call in this project: no per-deployment
setup, nothing to leak. One request covers every lattice point.
"""

import logging
import math

from .base import IngestAdapter
from .open_meteo import FORECAST_URL
from .base import request_json

log = logging.getLogger("twin.ingest.windfield")

#: Hours of forecast to keep. Beyond about 12 hours an advection projection
#: made from a single wind vector per point stops being defensible -- the
#: field itself reshapes -- so the series is fetched long and used short.
FORECAST_HOURS = 24

_HOURLY_FIELDS = (
    "precipitation",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
)


def wind_vector(speed_kmh, direction_deg):
    """Wind as (u, v) in km/h, where u is eastward and v is northward.

    Meteorological convention is the trap here: ``wind_direction_10m`` is the
    direction the wind blows *from*, not towards. A 90 degree wind is an
    easterly, which moves air *westward*. Getting this backwards puts every
    projected storm on the opposite side of the city, which looks entirely
    plausible on a map and is exactly wrong, so the conversion lives in one
    tested function rather than inline at each call site.
    """
    if speed_kmh is None or direction_deg is None:
        return None
    radians = math.radians(float(direction_deg))
    return (-float(speed_kmh) * math.sin(radians),
            -float(speed_kmh) * math.cos(radians))


class WindFieldAdapter(IngestAdapter):
    """Hourly wind/cloud/rain/heat series for each point on a lattice.

    ``fetch_raw(points=[(lat, lon), ...])`` returns one dict per point::

        {"lat", "lon", "hours": [{"hour", "precip_mm", "cloud_pct",
                                  "wind_speed_kmh", "wind_dir_deg",
                                  "u_kmh", "v_kmh", "temp_c",
                                  "humidity_pct", "apparent_c"}, ...]}

    ``hour`` is an offset from the first returned hour, not a wall clock, so
    a consumer never has to reason about the provider's timezone handling.
    """

    source_key = "windfield"
    #: Wind fields turn over faster than the 15-minute forecast cache but not
    #: fast enough to justify refetching per agent poll.
    cache_ttl_s = 10 * 60

    def fetch_raw(self, points, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        payload = request_json(
            self.session, "GET", FORECAST_URL, timeout_s,
            params={
                "latitude": ",".join(str(lat) for lat, _ in points),
                "longitude": ",".join(str(lon) for _, lon in points),
                "hourly": ",".join(_HOURLY_FIELDS),
                "forecast_days": 2,
                "timezone": "UTC",
            },
        )
        entries = payload if isinstance(payload, list) else [payload]
        if len(entries) != len(points):
            raise ValueError("windfield point count mismatch: sent %d got %d"
                             % (len(points), len(entries)))

        return [self._one_point(lat, lon, entry)
                for (lat, lon), entry in zip(points, entries)]

    def _one_point(self, lat, lon, entry):
        hourly = entry.get("hourly") or {}
        times = hourly.get("time") or []
        count = min(FORECAST_HOURS, len(times))

        hours = []
        for index in range(count):
            speed = _at(hourly.get("wind_speed_10m"), index)
            bearing = _at(hourly.get("wind_direction_10m"), index)
            vector = wind_vector(speed, bearing)
            hours.append({
                "hour": index,
                "precip_mm": _at(hourly.get("precipitation"), index),
                "cloud_pct": _at(hourly.get("cloud_cover"), index),
                "wind_speed_kmh": speed,
                "wind_dir_deg": bearing,
                "u_kmh": vector[0] if vector else None,
                "v_kmh": vector[1] if vector else None,
                "temp_c": _at(hourly.get("temperature_2m"), index),
                "humidity_pct": _at(hourly.get("relative_humidity_2m"), index),
                "apparent_c": _at(hourly.get("apparent_temperature"), index),
            })

        return {"lat": lat, "lon": lon, "hours": hours}

    def record_count(self, data):
        return sum(len(point.get("hours") or []) for point in (data or []))

    def neutral_value(self, points, **_):
        """Calm and dry, per the section 4.1 fallback contract.

        Zeroed wind matters more here than zeroed rain: with no vector,
        ``forecast.advect`` moves nothing and projects nothing, so a failed
        fetch produces no flags rather than flags pointing in a made-up
        direction. Silence is the only safe degradation for a forecast.
        """
        return [{
            "lat": lat, "lon": lon,
            "hours": [{
                "hour": index, "precip_mm": 0.0, "cloud_pct": 0.0,
                "wind_speed_kmh": 0.0, "wind_dir_deg": None,
                "u_kmh": 0.0, "v_kmh": 0.0, "temp_c": None,
                "humidity_pct": None, "apparent_c": None,
            } for index in range(FORECAST_HOURS)],
        } for lat, lon in points]


def _at(series, index):
    if not series or index >= len(series):
        return None
    return series[index]
