"""Global corroborating event feeds: GDACS and USGS. Both keyless.

Neither of these will usually have anything to say about Hyderabad or
Bengaluru, and that is the point: they are corroboration, not detection. When
a citizen report and a rain spike suggest something, an independent
international feed either backs it up or stays silent, and an analyst reading
a flag benefits from knowing which.

Both are normalised into the same dict shape ``twin/alerts.py::_upsert_alert``
consumes, so a GDACS flood event and an IMD warning land in one table and are
told apart by ``source`` rather than by having two parallel code paths.

Events are filtered to within ``GLOBAL_EVENT_RADIUS_KM`` of the city centre.
A magnitude 6 earthquake is always real and almost never this city's problem.
"""

import logging
from datetime import datetime, timedelta, timezone

from .. import config
from .. import geo
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.global_events")

#: GDACS alert level -> CAP severity vocabulary, so one severity scale reaches
#: the scorer regardless of which feed a record came from.
GDACS_SEVERITY = {"red": "Extreme", "orange": "Severe", "green": "Minor"}


class GdacsAdapter(IngestAdapter):
    """Disaster events from the Global Disaster Alert and Coordination System."""

    source_key = "gdacs"
    #: Global events move slowly; polling harder buys nothing.
    cache_ttl_s = 15 * 60
    max_retries = 1

    def fetch_raw(self, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        response = self.session.get(config.GDACS_URL, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()

        events = []
        for feature in payload.get("features") or []:
            properties = feature.get("properties") or {}
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coordinates) < 2:
                continue
            events.append({
                "uid": str(properties.get("eventid") or properties.get("eventname") or ""),
                "event_type": properties.get("eventtype"),
                "name": properties.get("eventname") or properties.get("htmldescription"),
                "alert_level": (properties.get("alertlevel") or "").lower(),
                "country": properties.get("country"),
                "from_date": properties.get("fromdate"),
                "to_date": properties.get("todate"),
                "url": (properties.get("url") or {}).get("report")
                if isinstance(properties.get("url"), dict) else properties.get("url"),
                "lon": float(coordinates[0]),
                "lat": float(coordinates[1]),
                "description": properties.get("htmldescription") or properties.get("description"),
            })

        return {"events": events, "source": "GDACS",
                "attribution": "GDACS (European Commission / UN)"}

    def neutral_value(self, **_):
        return {"events": [], "source": "GDACS", "unavailable": True,
                "attribution": "GDACS (European Commission / UN)"}

    def record_count(self, data):
        return len((data or {}).get("events") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        return hashlib.sha1(b"gdacs:all").hexdigest()


class UsgsQuakeAdapter(IngestAdapter):
    """Earthquakes in the last 24 hours, worldwide."""

    source_key = "usgs_quakes"
    cache_ttl_s = 10 * 60
    max_retries = 1

    def fetch_raw(self, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        response = self.session.get(config.USGS_QUAKE_URL, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()

        quakes = []
        for feature in payload.get("features") or []:
            properties = feature.get("properties") or {}
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coordinates) < 2:
                continue
            quakes.append({
                "uid": feature.get("id"),
                "magnitude": properties.get("mag"),
                "place": properties.get("place"),
                "time_ms": properties.get("time"),
                "url": properties.get("url"),
                "alert": properties.get("alert"),
                "tsunami": properties.get("tsunami"),
                "lon": float(coordinates[0]),
                "lat": float(coordinates[1]),
                "depth_km": float(coordinates[2]) if len(coordinates) > 2 else None,
            })

        return {"quakes": quakes, "source": "USGS",
                "attribution": "U.S. Geological Survey"}

    def neutral_value(self, **_):
        return {"quakes": [], "source": "USGS", "unavailable": True,
                "attribution": "U.S. Geological Survey"}

    def record_count(self, data):
        return len((data or {}).get("quakes") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        return hashlib.sha1(b"usgs:all_day").hexdigest()


# --------------------------------------------------------------------------
# Normalisation into the alert shape
# --------------------------------------------------------------------------

def gdacs_alert_dicts(data, city, radius_km=None):
    """GDACS events near `city`, in twin/alerts.py's expected shape."""
    radius_km = radius_km or config.GLOBAL_EVENT_RADIUS_KM
    alerts = []

    for event in (data or {}).get("events") or []:
        distance_km = geo.haversine_m(
            city.center_latitude, city.center_longitude,
            event["lat"], event["lon"]) / 1000.0
        if distance_km > radius_km:
            continue

        severity = GDACS_SEVERITY.get(event.get("alert_level"), "Minor")
        alerts.append({
            "source_uid": "gdacs:%s" % event.get("uid"),
            "identifier": event.get("uid"),
            "sender_name": "GDACS",
            "event": "%s: %s" % (event.get("event_type") or "Event", event.get("name") or ""),
            "category": "Geo" if event.get("event_type") == "EQ" else "Met",
            "severity": severity,
            "certainty": "Observed",
            "urgency": "Expected",
            "msg_type": "Alert",
            "headline": event.get("name"),
            "description": event.get("description"),
            "area_desc": "%s (%.0f km from %s)" % (
                event.get("country") or "", distance_km, city.display_name),
            "sent": event.get("from_date"),
            "effective": event.get("from_date"),
            "expires": event.get("to_date"),
            "raw_url": event.get("url"),
            "geometry": _circle_geojson(event["lat"], event["lon"], 15.0),
            "geometry_kind": "point",
            "distance_km": round(distance_km, 1),
        })
    return alerts


def usgs_alert_dicts(data, city, radius_km=None):
    """Earthquakes near `city`, in twin/alerts.py's expected shape.

    The affected-radius heuristic is deliberately crude and labelled as an
    estimate: felt radius grows roughly an order of magnitude per two
    magnitude points, and no open feed publishes a real shaking footprint for
    every quake.
    """
    radius_km = radius_km or config.GLOBAL_EVENT_RADIUS_KM
    alerts = []

    for quake in (data or {}).get("quakes") or []:
        distance_km = geo.haversine_m(
            city.center_latitude, city.center_longitude,
            quake["lat"], quake["lon"]) / 1000.0
        if distance_km > radius_km:
            continue

        magnitude = quake.get("magnitude") or 0.0
        felt_radius_km = max(5.0, min(300.0, 10.0 ** (0.5 * float(magnitude) - 1.0)))
        sent = _epoch_ms_to_iso(quake.get("time_ms"))

        alerts.append({
            "source_uid": "usgs:%s" % quake.get("uid"),
            "identifier": quake.get("uid"),
            "sender_name": "USGS",
            "event": "M%.1f earthquake" % float(magnitude),
            "category": "Geo",
            "severity": _quake_severity(magnitude),
            "certainty": "Observed",
            "urgency": "Past",
            "msg_type": "Alert",
            "headline": quake.get("place"),
            "description": "Magnitude %s at %s km depth, %.0f km from %s." % (
                magnitude, quake.get("depth_km"), distance_km, city.display_name),
            "area_desc": quake.get("place"),
            "sent": sent,
            "effective": sent,
            # Seismic events are instantaneous; a 6-hour window keeps one on
            # the board for a shift without letting it linger for days.
            "expires": _iso_plus_hours(sent, 6),
            "raw_url": quake.get("url"),
            "geometry": _circle_geojson(quake["lat"], quake["lon"], felt_radius_km),
            "geometry_kind": "point",
            "distance_km": round(distance_km, 1),
        })
    return alerts


def _quake_severity(magnitude):
    try:
        magnitude = float(magnitude)
    except (TypeError, ValueError):
        return "Unknown"
    if magnitude >= 6.5:
        return "Extreme"
    if magnitude >= 5.5:
        return "Severe"
    if magnitude >= 4.0:
        return "Moderate"
    return "Minor"


def _circle_geojson(lat, lon, radius_km, n_points=24):
    """A GeoJSON Polygon approximating a circle, in (lon, lat) order."""
    from shapely.geometry import mapping

    polygon = geo.circle_polygon(lat, lon, radius_km, n_points=n_points)
    return mapping(polygon)


def _epoch_ms_to_iso(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _iso_plus_hours(iso_text, hours):
    if not iso_text:
        return None
    try:
        return (datetime.fromisoformat(iso_text) + timedelta(hours=hours)).isoformat()
    except (TypeError, ValueError):
        return None
