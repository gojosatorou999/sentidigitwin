"""Public transport: GTFS timetables and GTFS-Realtime vehicle positions.

Why a disaster twin cares about buses: a city bus fleet is a free, dense,
always-moving sensor network that reports exactly the thing satellites and
rain gauges cannot -- whether the road is still usable. Twenty buses stopped
for fifteen minutes on one arterial, while the rest of the city moves
normally, is the earliest honest signal of a flooded underpass that anyone
will get, and it arrives before the first citizen report.

That is the whole design intent here: vehicle positions are not drawn because
a moving dot is pretty, they are aggregated into a **stall rate per cell**
which the scorer can use (twin/anomaly.py) and the agent can cite.

Feed URLs are configuration, never code. No Indian transit agency publishes a
stable, documented, public GTFS-RT endpoint, so hard-coding one would mean
shipping a URL that is wrong for most deployments. Set them in .env::

    TWIN_GTFS_RT_URLS={"bengaluru": "https://example/vehiclepositions.pb"}
    TWIN_GTFS_STATIC_URLS={"bengaluru": "https://example/gtfs.zip"}
    TWIN_GTFS_RT_HEADERS={"bengaluru": {"Authorization": "..."}}

With none set, both adapters report "not configured" -- an ordinary state, not
an error, and the layer simply does not appear.
"""

import csv
import io
import logging
import zipfile
from datetime import datetime, timezone

from .. import config
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.transit")


def gtfs_realtime_available():
    """Is the protobuf binding installed?

    GTFS-RT is a protobuf wire format; without the binding there is nothing
    sane to parse it with. Reported as a capability rather than raised on
    import so the module still loads (and the rest of the twin still runs) on
    an environment where the optional dependency is absent.
    """
    try:
        from google.transit import gtfs_realtime_pb2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - ImportError, or a protobuf ABI mismatch
        return False


class GtfsRealtimeAdapter(IngestAdapter):
    """Live vehicle positions for one city, normalised to observations.

    Also accepts a JSON feed: a few Indian operators expose GTFS-RT rendered
    as JSON rather than protobuf, and refusing those on principle would mean
    refusing the only working feed in the deployment that has one.
    """

    source_key = "gtfs_realtime"
    #: Positions are stale within a minute; this matches the 2-minute poll in
    #: jobs.py so a poll either serves cache or fetches exactly once.
    cache_ttl_s = 90
    max_retries = 1

    def fetch_raw(self, city_slug=None, timeout_s=None, **_):
        url = (config.GTFS_RT_URLS or {}).get(city_slug)
        if not url:
            return {"vehicles": [], "not_configured": True,
                    "city": city_slug, "source": "GTFS-Realtime"}

        timeout_s = timeout_s or self.timeout_s
        headers = (config.GTFS_RT_HEADERS or {}).get(city_slug) or {}
        response = self.session.get(url, headers=headers, timeout=timeout_s)
        response.raise_for_status()

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "json" in content_type or response.content[:1] in (b"{", b"["):
            vehicles = _vehicles_from_json(response.json())
        else:
            vehicles = _vehicles_from_protobuf(response.content)

        return {
            "vehicles": vehicles,
            "city": city_slug,
            "source": "GTFS-Realtime",
            "attribution": "GTFS-Realtime feed (transit operator)",
        }

    def neutral_value(self, city_slug=None, **_):
        return {"vehicles": [], "city": city_slug, "unavailable": True,
                "source": "GTFS-Realtime"}

    def record_count(self, data):
        return len((data or {}).get("vehicles") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        return hashlib.sha1(("gtfsrt:%s" % kwargs.get("city_slug")).encode("utf-8")).hexdigest()


def _vehicles_from_protobuf(content):
    if not gtfs_realtime_available():
        raise RuntimeError(
            "gtfs-realtime-bindings is not installed; cannot parse a protobuf feed")

    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)

    vehicles = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        position = vehicle.position
        if not (position.latitude or position.longitude):
            continue
        vehicles.append(_vehicle_dict(
            uid=entity.id or vehicle.vehicle.id,
            lat=position.latitude,
            lon=position.longitude,
            bearing=position.bearing if position.HasField("bearing") else None,
            speed=position.speed if position.HasField("speed") else None,
            route_id=vehicle.trip.route_id or None,
            trip_id=vehicle.trip.trip_id or None,
            label=vehicle.vehicle.label or None,
            timestamp=vehicle.timestamp or None,
            current_status=_status_name(vehicle.current_status),
        ))
    return vehicles


def _vehicles_from_json(payload):
    """A JSON-rendered GTFS-RT feed.

    Field names follow the protobuf schema's JSON mapping, but real feeds are
    inconsistent about camelCase vs snake_case, so both spellings are read.
    """
    entities = payload.get("entity") or payload.get("entities") or []
    vehicles = []
    for entity in entities:
        vehicle = entity.get("vehicle") or {}
        position = vehicle.get("position") or {}
        lat = position.get("latitude") or position.get("lat")
        lon = position.get("longitude") or position.get("lon")
        if lat is None or lon is None:
            continue
        trip = vehicle.get("trip") or {}
        descriptor = vehicle.get("vehicle") or {}
        vehicles.append(_vehicle_dict(
            uid=entity.get("id") or descriptor.get("id"),
            lat=lat, lon=lon,
            bearing=position.get("bearing"),
            speed=position.get("speed"),
            route_id=trip.get("route_id") or trip.get("routeId"),
            trip_id=trip.get("trip_id") or trip.get("tripId"),
            label=descriptor.get("label"),
            timestamp=vehicle.get("timestamp"),
            current_status=vehicle.get("current_status") or vehicle.get("currentStatus"),
        ))
    return vehicles


def _vehicle_dict(uid, lat, lon, bearing, speed, route_id, trip_id, label,
                  timestamp, current_status):
    observed_at = _epoch_to_iso(timestamp)
    return {
        "source_key": "gtfs_realtime",
        "station_uid": "veh:%s" % (uid or trip_id or label or "unknown"),
        "kind": "transit_vehicle",
        "name": label or route_id or str(uid),
        "operator": None,
        "lat": float(lat),
        "lon": float(lon),
        # Speed is the layer's headline number: it is what makes a stalled
        # fleet legible at a glance.
        "value": float(speed) * 3.6 if speed is not None else None,
        "unit": "km/h",
        "metrics": {
            "route_id": route_id,
            "trip_id": trip_id,
            "bearing": bearing,
            "current_status": current_status,
        },
        "observed_at": observed_at,
    }


def _status_name(value):
    names = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}
    return names.get(value)


def _epoch_to_iso(timestamp):
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# GTFS static
# --------------------------------------------------------------------------

class GtfsStaticAdapter(IngestAdapter):
    """Stops and routes from a GTFS zip.

    Only ``stops.txt`` and ``routes.txt`` are read. The rest of a GTFS bundle
    (stop_times.txt is routinely 100 MB+) describes *scheduled* service, and
    the twin is asking a live question -- where is service actually happening
    now -- which only the realtime feed can answer.
    """

    source_key = "gtfs_static"
    #: A timetable changes a few times a year.
    cache_ttl_s = 7 * 24 * 60 * 60
    max_retries = 1
    timeout_s = 60.0

    def fetch_raw(self, city_slug=None, timeout_s=None, **_):
        url = (config.GTFS_STATIC_URLS or {}).get(city_slug)
        if not url:
            return {"stops": [], "routes": [], "not_configured": True,
                    "city": city_slug, "source": "GTFS"}

        timeout_s = timeout_s or self.timeout_s
        response = self.session.get(url, timeout=timeout_s)
        response.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            stops = _read_csv_member(bundle, "stops.txt")
            routes = _read_csv_member(bundle, "routes.txt")

        return {
            "city": city_slug,
            "stops": [
                {
                    "stop_id": row.get("stop_id"),
                    "name": row.get("stop_name"),
                    "lat": _to_float(row.get("stop_lat")),
                    "lon": _to_float(row.get("stop_lon")),
                }
                for row in stops
                if _to_float(row.get("stop_lat")) is not None
            ],
            "routes": [
                {
                    "route_id": row.get("route_id"),
                    "short_name": row.get("route_short_name"),
                    "long_name": row.get("route_long_name"),
                    "type": row.get("route_type"),
                }
                for row in routes
            ],
            "source": "GTFS",
            "attribution": "GTFS feed (transit operator)",
        }

    def neutral_value(self, city_slug=None, **_):
        return {"stops": [], "routes": [], "city": city_slug, "unavailable": True,
                "source": "GTFS"}

    def record_count(self, data):
        return len((data or {}).get("stops") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        return hashlib.sha1(("gtfs:%s" % kwargs.get("city_slug")).encode("utf-8")).hexdigest()


def _read_csv_member(bundle, name):
    """One CSV member of a GTFS zip, tolerant of a UTF-8 BOM.

    GTFS files exported from Windows tooling routinely start with a BOM, which
    turns the first column's name into ``\\ufeffstop_id`` and makes every
    lookup of ``stop_id`` return None -- an empty layer with no error.
    """
    try:
        with bundle.open(name) as member:
            text = io.TextIOWrapper(member, encoding="utf-8-sig", newline="")
            return list(csv.DictReader(text))
    except KeyError:
        log.info("gtfs bundle has no %s", name)
        return []


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Aggregation: vehicles -> a per-cell disruption signal
# --------------------------------------------------------------------------

def stall_rate_by_cell(vehicles, h3_for, stale_minutes=None, now=None):
    """{h3_index: {"vehicles": n, "stalled": n, "stall_rate": 0..1}}.

    A vehicle counts as stalled when it is stopped (speed at or near zero, or
    GTFS-RT says ``STOPPED_AT``) *or* its last report is older than
    ``stale_minutes``. Both matter: a bus in floodwater stops reporting, and
    treating silence as "fine" is exactly backwards.

    Cells with only one or two vehicles are still reported, but a caller
    turning this into a score must apply its own minimum sample size -- one
    bus at a terminus is always "stalled" and means nothing.
    """
    stale_minutes = stale_minutes if stale_minutes is not None else config.TRANSIT_STALE_MIN
    now = now or datetime.now(timezone.utc)

    by_cell = {}
    for vehicle in vehicles or []:
        lat, lon = vehicle.get("lat"), vehicle.get("lon")
        if lat is None or lon is None:
            continue
        cell = h3_for(lat, lon)
        if not cell:
            continue

        entry = by_cell.setdefault(cell, {"vehicles": 0, "stalled": 0})
        entry["vehicles"] += 1

        speed = vehicle.get("value")
        status = (vehicle.get("metrics") or {}).get("current_status")
        observed_at = vehicle.get("observed_at")

        stalled = False
        if speed is not None and speed <= 2.0:
            stalled = True
        if status == "STOPPED_AT":
            stalled = True
        if observed_at:
            try:
                age_min = (now - datetime.fromisoformat(observed_at)).total_seconds() / 60.0
                if age_min > stale_minutes:
                    stalled = True
            except (TypeError, ValueError):
                pass

        if stalled:
            entry["stalled"] += 1

    for entry in by_cell.values():
        entry["stall_rate"] = (entry["stalled"] / entry["vehicles"]
                               if entry["vehicles"] else 0.0)
    return by_cell
