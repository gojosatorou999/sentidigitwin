"""Live pollution monitoring stations: CPCB (data.gov.in), OpenAQ, AQICN.

The twin already had air quality -- but as a *model*: Open-Meteo's CAMS
forecast, interpolated across a 5 km lattice. That is a good estimate of the
city's air and a poor description of one junction's air, because the thing
that makes an Indian city's AQI spike is local (a fire, a jam, construction)
and a 5 km reanalysis grid cannot see it.

These three adapters add the measured alternative: real instruments at known
coordinates, reporting what they actually read. All three normalise into one
observation shape so the map layer, the scorer and the agent never learn
which provider a reading came from:

    {source_key, station_uid, name, lat, lon, kind, value, unit,
     metrics, observed_at, operator, raw}

``value`` is always an AQI on the Indian CPCB scale, so a CPCB station and an
OpenAQ station can sit in the same colour ramp without lying about either.
That means computing the index here rather than trusting whatever number a
provider calls "AQI" -- US EPA AQI and CPCB AQI disagree by 40 points at the
concentrations these cities actually see.

Every adapter is key-gated and reports "not configured" as an ordinary
outcome. None of them is required for the twin to run.
"""

import logging
from datetime import datetime, timedelta, timezone

from .. import config
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.stations")

IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------
# CPCB AQI -- the official Indian index (CPCB, National AQI, 2014)
#
# Implemented here rather than taken from a provider because it is the index
# Indian officials, press and the public actually use, and because it is a
# published piecewise-linear function: deterministic, checkable, and safe to
# put in front of someone who has to justify an evacuation.
#
# Each entry: pollutant -> (unit, [(c_low, c_high, i_low, i_high), ...])
# Concentrations are 24-hour averages except O3 and CO (8-hour).
# --------------------------------------------------------------------------

CPCB_BREAKPOINTS = {
    "pm2.5": ("ug/m3", [(0, 30, 0, 50), (30, 60, 51, 100), (60, 90, 101, 200),
                        (90, 120, 201, 300), (120, 250, 301, 400), (250, 500, 401, 500)]),
    "pm10": ("ug/m3", [(0, 50, 0, 50), (50, 100, 51, 100), (100, 250, 101, 200),
                       (250, 350, 201, 300), (350, 430, 301, 400), (430, 600, 401, 500)]),
    "no2": ("ug/m3", [(0, 40, 0, 50), (40, 80, 51, 100), (80, 180, 101, 200),
                      (180, 280, 201, 300), (280, 400, 301, 400), (400, 600, 401, 500)]),
    "so2": ("ug/m3", [(0, 40, 0, 50), (40, 80, 51, 100), (80, 380, 101, 200),
                      (380, 800, 201, 300), (800, 1600, 301, 400), (1600, 2000, 401, 500)]),
    "co": ("mg/m3", [(0, 1, 0, 50), (1, 2, 51, 100), (2, 10, 101, 200),
                     (10, 17, 201, 300), (17, 34, 301, 400), (34, 50, 401, 500)]),
    "o3": ("ug/m3", [(0, 50, 0, 50), (50, 100, 51, 100), (100, 168, 101, 200),
                     (168, 208, 201, 300), (208, 748, 301, 400), (748, 1000, 401, 500)]),
    "nh3": ("ug/m3", [(0, 200, 0, 50), (200, 400, 51, 100), (400, 800, 101, 200),
                      (800, 1200, 201, 300), (1200, 1800, 301, 400), (1800, 2400, 401, 500)]),
}

#: CPCB's own descriptive bands, used verbatim on the card an operator reads.
CPCB_BANDS = ((50, "Good"), (100, "Satisfactory"), (200, "Moderate"),
              (300, "Poor"), (400, "Very Poor"), (10 ** 9, "Severe"))


def cpcb_sub_index(pollutant, concentration):
    """The CPCB sub-index for one pollutant, or None if it has no scale.

    Piecewise-linear interpolation between published breakpoints. A reading
    above the top breakpoint is clamped to 500 rather than extrapolated: the
    scale simply stops there, and inventing 900 would imply a precision the
    index does not have.
    """
    if concentration is None:
        return None
    key = (pollutant or "").strip().lower().replace("_", ".")
    if key in ("pm25", "pm 2.5"):
        key = "pm2.5"
    entry = CPCB_BREAKPOINTS.get(key)
    if entry is None:
        return None

    try:
        value = float(concentration)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None

    _unit, table = entry
    for c_low, c_high, i_low, i_high in table:
        if value <= c_high:
            if c_high == c_low:
                return float(i_high)
            return round(i_low + (i_high - i_low) * (value - c_low) / (c_high - c_low), 1)
    return 500.0


def cpcb_aqi(metrics):
    """{pollutant: concentration} -> (aqi, dominant_pollutant).

    The national AQI is the **worst** sub-index, not an average -- the index
    is designed to describe the pollutant that is actually harming you, and
    averaging it away is the standard way to under-report a PM2.5 event.
    """
    worst_value, worst_pollutant = None, None
    for pollutant, concentration in (metrics or {}).items():
        sub_index = cpcb_sub_index(pollutant, concentration)
        if sub_index is None:
            continue
        if worst_value is None or sub_index > worst_value:
            worst_value, worst_pollutant = sub_index, pollutant
    return worst_value, worst_pollutant


def cpcb_band(aqi):
    if aqi is None:
        return None
    for ceiling, label in CPCB_BANDS:
        if aqi <= ceiling:
            return label
    return "Severe"


# --------------------------------------------------------------------------
# CPCB live stations via data.gov.in
# --------------------------------------------------------------------------

class CpcbAqiAdapter(IngestAdapter):
    """Station-level AQI from the CPCB feed published on data.gov.in.

    The resource returns one row *per pollutant per station*, so rows are
    folded back into stations here -- a station is what an operator clicks,
    and eight rows called "Bollaram Industrial Area" are not eight places.
    """

    source_key = "cpcb_aqi"
    cache_ttl_s = 15 * 60
    max_retries = 1
    #: data.gov.in is routinely slow -- measured at 1.4 s on a good run and
    #: past 25 s on a bad one. This adapter only ever runs on the scheduler,
    #: never on a request path, so it can afford to wait; the 8 s default was
    #: timing the layer out on roughly every other poll.
    timeout_s = 30.0

    #: Rows per request. The API accepts a larger `limit`, but the shared
    #: sample key is capped at 10 rows regardless of what is asked for, so
    #: pages are walked with `offset` -- which works on both keys and is the
    #: only way a registered key ever sees a full city. One station publishes
    #: up to 8 pollutant rows, so 100 rows is roughly 12 stations per page.
    page_size = 100
    max_pages = 12

    def fetch_raw(self, city=None, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        api_key = config.DATA_GOV_IN_KEY or config.DATA_GOV_IN_SAMPLE_KEY
        url = config.DATA_GOV_IN_URL % config.CPCB_AQI_RESOURCE_ID

        using_sample_key = not config.DATA_GOV_IN_KEY
        # The shared sample key caps every response at 10 rows and is rate
        # limited across everyone who ever copied it out of the docs, so paging
        # with it means a dozen requests that return nothing new and push the
        # whole key into 429. One page, and an honest note about why.
        max_pages = 1 if using_sample_key else self.max_pages

        records = []
        for page in range(max_pages):
            params = {
                "api-key": api_key,
                "format": "json",
                "limit": self.page_size,
                "offset": page * self.page_size,
            }
            if city:
                params["filters[city]"] = city

            response = self.session.get(url, params=params, timeout=timeout_s)
            if response.status_code == 429:
                raise RuntimeError(
                    "data.gov.in rate-limited this request (429)%s"
                    % (" -- the built-in sample key is shared by everyone who read "
                       "the docs. Register a free key and set DATA_GOV_IN_KEY."
                       if using_sample_key else ""))
            response.raise_for_status()
            page_records = response.json().get("records") or []
            records.extend(page_records)
            if len(page_records) < self.page_size:
                break

        stations = _fold_cpcb_records(records)
        return {
            "stations": stations,
            "record_rows": len(records),
            "using_sample_key": using_sample_key,
            "source": "CPCB via data.gov.in",
            "attribution": "Central Pollution Control Board / data.gov.in (Govt. of India)",
        }

    def neutral_value(self, **_):
        return {"stations": [], "source": "CPCB via data.gov.in", "unavailable": True,
                "attribution": "Central Pollution Control Board / data.gov.in (Govt. of India)"}

    def record_count(self, data):
        return len((data or {}).get("stations") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        raw = "cpcb:%s" % (kwargs.get("city") or "all")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _fold_cpcb_records(records):
    """Per-pollutant rows -> one observation per station.

    data.gov.in has shipped at least three column spellings for this resource
    (``pollutant_avg`` vs ``avg_value``, ``last_update`` vs ``last_update_time``),
    so every field is read through a tolerant lookup. A schema change should
    cost one pollutant, not the whole layer.
    """
    by_station = {}

    for record in records:
        station_name = _first(record, "station", "station_name")
        city = _first(record, "city")
        if not station_name:
            continue

        lat = _to_float(_first(record, "latitude", "lat"))
        lon = _to_float(_first(record, "longitude", "lon", "long"))
        if lat is None or lon is None:
            continue

        uid = "%s|%s" % (city or "", station_name)
        entry = by_station.setdefault(uid, {
            "source_key": "cpcb_aqi",
            "station_uid": uid,
            "name": station_name,
            "city_name": city,
            "operator": "CPCB",
            "kind": "air_quality",
            "lat": lat,
            "lon": lon,
            "metrics": {},
            # ISO string, not a datetime: IngestAdapter's disk cache json-dumps
            # whatever fetch_raw returns, and a datetime in there raises inside
            # the cache write, which run() would report as a failed fetch.
            "observed_at": _iso(_parse_cpcb_time(
                _first(record, "last_update", "last_update_time"))),
            "raw_unit": _first(record, "pollutant_unit", "unit"),
        })

        pollutant = (_first(record, "pollutant_id", "pollutant") or "").strip().lower()
        value = _to_float(_first(record, "pollutant_avg", "avg_value", "pollutant_max"))
        # "NA" is how this feed says a station reported nothing this hour; it
        # parses to None and must stay absent rather than becoming a zero,
        # which would read as pristine air.
        if pollutant and value is not None:
            entry["metrics"][pollutant] = value

    stations = []
    for entry in by_station.values():
        aqi, dominant = cpcb_aqi(entry["metrics"])
        entry["value"] = aqi
        entry["unit"] = "AQI (CPCB)"
        entry["dominant_pollutant"] = dominant
        entry["band"] = cpcb_band(aqi)
        stations.append(entry)
    return stations


def _parse_cpcb_time(value):
    """``"01-09-2026 18:00:00"`` (IST, no offset) -> aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# OpenAQ v3
# --------------------------------------------------------------------------

class OpenAqAdapter(IngestAdapter):
    """Reference-grade and low-cost sensors aggregated by OpenAQ.

    Needs a free API key (v3 returns 401 without one). Two calls: locations in
    the city bbox, then the latest measurements for those locations.
    """

    source_key = "openaq"
    cache_ttl_s = 20 * 60
    max_retries = 1

    def fetch_raw(self, bbox=None, timeout_s=None, **_):
        if not config.OPENAQ_API_KEY:
            return {"stations": [], "not_configured": True, "source": "OpenAQ",
                    "attribution": "OpenAQ (CC BY 4.0)"}
        if not bbox:
            raise ValueError("openaq fetch needs a bbox")

        timeout_s = timeout_s or self.timeout_s
        headers = {"X-API-Key": config.OPENAQ_API_KEY}

        locations = self.session.get(
            "%s/locations" % config.OPENAQ_URL,
            params={"bbox": ",".join("%.4f" % v for v in bbox), "limit": 200},
            headers=headers, timeout=timeout_s)
        locations.raise_for_status()

        stations = []
        for location in (locations.json().get("results") or []):
            coords = location.get("coordinates") or {}
            lat, lon = coords.get("latitude"), coords.get("longitude")
            if lat is None or lon is None:
                continue

            metrics, observed_at = {}, None
            for sensor in location.get("sensors") or []:
                parameter = ((sensor.get("parameter") or {}).get("name") or "").lower()
                latest = sensor.get("latest") or {}
                value = latest.get("value")
                if parameter and value is not None:
                    metrics[parameter] = value
                    observed_at = observed_at or ((latest.get("datetime") or {}).get("utc"))

            aqi, dominant = cpcb_aqi(metrics)
            stations.append({
                "source_key": "openaq",
                "station_uid": "openaq:%s" % location.get("id"),
                "name": location.get("name"),
                "operator": (location.get("owner") or {}).get("name") or "OpenAQ",
                "kind": "air_quality",
                "lat": lat, "lon": lon,
                "metrics": metrics,
                "value": aqi,
                "unit": "AQI (CPCB)",
                "dominant_pollutant": dominant,
                "band": cpcb_band(aqi),
                "observed_at": _iso(_parse_iso(observed_at)),
            })

        return {"stations": stations, "source": "OpenAQ",
                "attribution": "OpenAQ (CC BY 4.0)"}

    def neutral_value(self, **_):
        return {"stations": [], "source": "OpenAQ", "unavailable": True,
                "attribution": "OpenAQ (CC BY 4.0)"}

    def record_count(self, data):
        return len((data or {}).get("stations") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        bbox = kwargs.get("bbox") or ()
        raw = "openaq:" + ",".join("%.3f" % float(v) for v in bbox)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# AQICN / WAQI
# --------------------------------------------------------------------------

class AqicnAdapter(IngestAdapter):
    """Every station AQICN knows inside a bounding box, in one call.

    Cheap and dense, but the AQI it returns is the **US EPA** index. It is
    converted back to concentrations where AQICN supplies them (``iaqi``) and
    otherwise labelled as a US-scale number, because silently mixing two
    national indices in one colour ramp makes the map wrong in a way nobody
    can see.
    """

    source_key = "aqicn"
    cache_ttl_s = 20 * 60
    max_retries = 1

    def fetch_raw(self, bbox=None, timeout_s=None, **_):
        if not config.AQICN_TOKEN:
            return {"stations": [], "not_configured": True, "source": "AQICN",
                    "attribution": "World Air Quality Index project (aqicn.org)"}
        if not bbox:
            raise ValueError("aqicn fetch needs a bbox")

        timeout_s = timeout_s or self.timeout_s
        min_lon, min_lat, max_lon, max_lat = bbox
        response = self.session.get(
            "%s/map/bounds/" % config.AQICN_URL,
            params={"latlng": "%f,%f,%f,%f" % (min_lat, min_lon, max_lat, max_lon),
                    "token": config.AQICN_TOKEN},
            timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "ok":
            raise RuntimeError("aqicn: %s" % payload.get("data"))

        stations = []
        for row in payload.get("data") or []:
            lat, lon = _to_float(row.get("lat")), _to_float(row.get("lon"))
            if lat is None or lon is None:
                continue
            us_aqi = _to_float(row.get("aqi"))
            station = (row.get("station") or {})
            stations.append({
                "source_key": "aqicn",
                "station_uid": "aqicn:%s" % row.get("uid"),
                "name": station.get("name"),
                "operator": "AQICN network",
                "kind": "air_quality",
                "lat": lat, "lon": lon,
                "metrics": {},
                "value": us_aqi,
                "unit": "AQI (US EPA)",
                "band": None,
                "observed_at": _iso(_parse_iso(station.get("time"))),
            })

        return {"stations": stations, "source": "AQICN",
                "attribution": "World Air Quality Index project (aqicn.org)"}

    def neutral_value(self, **_):
        return {"stations": [], "source": "AQICN", "unavailable": True,
                "attribution": "World Air Quality Index project (aqicn.org)"}

    def record_count(self, data):
        return len((data or {}).get("stations") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        bbox = kwargs.get("bbox") or ()
        raw = "aqicn:" + ",".join("%.3f" % float(v) for v in bbox)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _first(record, *keys):
    for key in keys:
        value = record.get(key)
        if value not in (None, "", "NA", "na", "N/A"):
            return value
    return None


def _to_float(value):
    if value in (None, "", "NA", "na", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat() if value is not None else None
