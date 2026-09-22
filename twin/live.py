"""Live point observations: fetch, place on the grid, persist, serve.

The adapters in ``twin/ingest/`` know how to talk to one provider each. This
module is where their output becomes part of the twin: assigned to an H3 cell,
upserted so re-polling is idempotent, and expired so nothing stale is ever
drawn as current.

The rule that shapes everything here is that **a live layer must never lie
about being live**. Every observation carries the time its *source* measured
it (``observed_at``), not the time the twin fetched it, and the API exposes
the resulting age so the UI can label a reading as stale rather than draw a
20-minute-old bus as if it were moving now.
"""

import logging
from datetime import datetime, timedelta, timezone

import h3

from . import alerts as alerts_module
from . import config
from . import models as m
from .ingest.global_events import (
    GdacsAdapter, UsgsQuakeAdapter, gdacs_alert_dicts, usgs_alert_dicts,
)
from .ingest.stations import AqicnAdapter, CpcbAqiAdapter, OpenAqAdapter
from .ingest.transit import GtfsRealtimeAdapter, stall_rate_by_cell

log = logging.getLogger("twin.live")

#: A vehicle nobody has reported for this long is deleted, not kept as a ghost.
VEHICLE_TTL_MIN = 30
#: A station reading older than this is served with status="stale".
STATION_STALE_MIN = 120


def refresh_stations(db, city):
    """Poll every configured air-quality network for one city."""
    bbox = (city.bbox_min_lon, city.bbox_min_lat, city.bbox_max_lon, city.bbox_max_lat)
    observations, statuses = [], {}

    for adapter, kwargs in (
        (CpcbAqiAdapter(), {"city": city.display_name}),
        (OpenAqAdapter(), {"bbox": bbox}),
        (AqicnAdapter(), {"bbox": bbox}),
    ):
        data, snapshot = adapter.run(db, city_id=city.id, **kwargs)
        statuses[adapter.source_key] = snapshot.status if snapshot else "failed"
        if (data or {}).get("not_configured"):
            statuses[adapter.source_key] = "not_configured"
        observations.extend((data or {}).get("stations") or [])

    written = persist_observations(db, city, observations, bbox=bbox)
    return {"city": city.slug, "written": written, "sources": statuses,
            "fetched": len(observations)}


def refresh_transit(db, city):
    """Poll live vehicle positions for one city and refresh the layer."""
    adapter = GtfsRealtimeAdapter()
    data, snapshot = adapter.run(db, city_id=city.id, city_slug=city.slug)
    status = snapshot.status if snapshot else "failed"
    if (data or {}).get("not_configured"):
        return {"city": city.slug, "written": 0, "status": "not_configured"}

    bbox = (city.bbox_min_lon, city.bbox_min_lat, city.bbox_max_lon, city.bbox_max_lat)
    vehicles = (data or {}).get("vehicles") or []
    written = persist_observations(db, city, vehicles, bbox=bbox)
    removed = _expire_vehicles(db, city)
    db.session.commit()

    return {"city": city.slug, "written": written, "removed": removed,
            "status": status, "fetched": len(vehicles)}


def refresh_global_events(db, city):
    """GDACS + USGS events near one city, stored as external alerts."""
    created = updated = 0
    statuses = {}

    for adapter, to_alerts, source in (
        (GdacsAdapter(), gdacs_alert_dicts, "gdacs"),
        (UsgsQuakeAdapter(), usgs_alert_dicts, "usgs"),
    ):
        data, snapshot = adapter.run(db, city_id=city.id)
        statuses[adapter.source_key] = snapshot.status if snapshot else "failed"
        for raw in to_alerts(data, city):
            try:
                outcome = alerts_module._upsert_alert(db, city, raw, source=source)
            except Exception:  # noqa: BLE001
                log.exception("global event upsert failed: %s", raw.get("source_uid"))
                continue
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1

    db.session.commit()
    return {"city": city.slug, "created": created, "updated": updated,
            "sources": statuses}


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def persist_observations(db, city, observations, bbox=None):
    """Upsert observations, assigning each to an H3 cell. Returns rows written.

    Out-of-bbox points are dropped: a national feed filtered by city name
    still returns the odd station 400 km away (two Indian cities share a name
    more often than is convenient), and one of those on the map is worse than
    a gap, because it makes the layer untrustworthy everywhere.
    """
    if not observations:
        return 0

    keys = {(o.get("source_key"), o.get("station_uid"))
            for o in observations if o.get("station_uid")}
    existing = {}
    if keys:
        source_keys = {k[0] for k in keys}
        for row in (db.session.query(m.TwinObservation)
                    .filter(m.TwinObservation.source_key.in_(source_keys),
                            m.TwinObservation.city_id == city.id)
                    .all()):
            existing[(row.source_key, row.station_uid)] = row

    written = 0
    for observation in observations:
        station_uid = observation.get("station_uid")
        lat, lon = observation.get("lat"), observation.get("lon")
        if not station_uid or lat is None or lon is None:
            continue
        if bbox and not _within(bbox, lat, lon):
            continue

        key = (observation.get("source_key"), station_uid)
        row = existing.get(key)
        if row is None:
            row = m.TwinObservation(source_key=key[0], station_uid=station_uid)
            db.session.add(row)
            existing[key] = row

        observed_at = _parse_iso(observation.get("observed_at"))
        row.city_id = city.id
        row.kind = observation.get("kind") or "unknown"
        row.name = observation.get("name")
        row.operator = observation.get("operator")
        row.latitude = lat
        row.longitude = lon
        row.h3_index = h3.latlng_to_cell(lat, lon, config.H3_RESOLUTION)
        row.value = observation.get("value")
        row.unit = observation.get("unit")
        row.metrics = observation.get("metrics") or {}
        row.observed_at = observed_at
        row.fetched_at = m.utcnow()
        row.status = _freshness(observed_at, row.kind)
        written += 1

    db.session.commit()
    return written


def _freshness(observed_at, kind):
    """ok | stale | unknown -- judged against what the source *claims*.

    A transit feed that has stopped updating and an air-quality station that
    reports hourly need different thresholds; treating them alike would badge
    every CPCB station stale two minutes after the hour.
    """
    if observed_at is None:
        return "unknown"
    age_min = (m.utcnow() - observed_at).total_seconds() / 60.0
    ceiling = config.TRANSIT_STALE_MIN if kind == "transit_vehicle" else STATION_STALE_MIN
    return "ok" if age_min <= ceiling else "stale"


def _expire_vehicles(db, city):
    """Delete vehicles nobody has reported recently."""
    cutoff = m.utcnow() - timedelta(minutes=VEHICLE_TTL_MIN)
    stale = (db.session.query(m.TwinObservation)
             .filter(m.TwinObservation.city_id == city.id,
                     m.TwinObservation.kind == "transit_vehicle",
                     m.TwinObservation.fetched_at < cutoff)
             .all())
    for row in stale:
        db.session.delete(row)
    return len(stale)


def _within(bbox, lat, lon):
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _parse_iso(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Queries and serialisation
# --------------------------------------------------------------------------

def observations(db, city, kind=None, limit=5000):
    query = db.session.query(m.TwinObservation).filter(
        m.TwinObservation.city_id == city.id)
    if kind:
        query = query.filter(m.TwinObservation.kind == kind)
    return query.order_by(m.TwinObservation.observed_at.desc()).limit(limit).all()


def observations_feature_collection(db, city, kind=None, limit=5000):
    """GeoJSON for a live point layer, with per-feature age in seconds.

    ``age_seconds`` is computed server-side rather than left to the browser
    because the two clocks disagree -- an operator's laptop being three
    minutes fast would show negative ages, which reads as a bug in the data.
    """
    now = m.utcnow()
    features = []
    oldest = newest = None

    for row in observations(db, city, kind=kind, limit=limit):
        age_seconds = None
        if row.observed_at is not None:
            age_seconds = int((now - row.observed_at).total_seconds())
            oldest = age_seconds if oldest is None else max(oldest, age_seconds)
            newest = age_seconds if newest is None else min(newest, age_seconds)

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row.longitude, row.latitude]},
            "properties": {
                "id": row.id,
                "uid": row.station_uid,
                "source": row.source_key,
                "kind": row.kind,
                "name": row.name,
                "operator": row.operator,
                "value": row.value,
                "unit": row.unit,
                "status": row.status,
                "metrics": row.metrics or {},
                "h3": row.h3_index,
                "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                "age_seconds": age_seconds,
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "count": len(features),
        "newest_age_seconds": newest,
        "oldest_age_seconds": oldest,
        "attribution": _attribution_for(kind),
    }


def _attribution_for(kind):
    if kind == "transit_vehicle":
        return "GTFS-Realtime feed (transit operator)"
    if kind == "air_quality":
        return ("CPCB / data.gov.in (Govt. of India), OpenAQ (CC BY 4.0), "
                "World Air Quality Index project")
    return "See per-feature source"


def transit_disruption(db, city):
    """{h3_index: {...}} stall rates, computed from stored vehicle rows.

    Reads persisted observations rather than re-polling so that the scorer and
    the map always describe the same instant -- recomputing from a fresh fetch
    would let the score cite vehicles the operator cannot see.
    """
    rows = observations(db, city, kind="transit_vehicle")
    vehicles = [{
        "lat": row.latitude, "lon": row.longitude, "value": row.value,
        "metrics": row.metrics or {},
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
    } for row in rows]

    return stall_rate_by_cell(
        vehicles,
        h3_for=lambda lat, lon: h3.latlng_to_cell(lat, lon, config.H3_RESOLUTION))


def live_summary(db, city):
    """Per-layer counts and freshness, for the console's live-status strip."""
    now = m.utcnow()
    summary = {}

    for kind in ("air_quality", "transit_vehicle", "water_level"):
        rows = observations(db, city, kind=kind)
        ages = [int((now - row.observed_at).total_seconds())
                for row in rows if row.observed_at is not None]
        summary[kind] = {
            "count": len(rows),
            "fresh": sum(1 for row in rows if row.status == "ok"),
            "newest_age_seconds": min(ages) if ages else None,
            "sources": sorted({row.source_key for row in rows}),
        }

    summary["alerts"] = {
        "active": alerts_module.active_alert_count(db, city),
    }
    return summary
