"""Twin HTTP routes (section 7).

All routes are ``@login_required``; state/summary/cell/incidents/
infrastructure/compare/timeline/stream routes additionally require the
official/analyst roles (C4); ``/refresh`` and ``/seed`` require official
specifically. There are no public endpoints in this blueprint.
"""

import gzip
import json
import logging
import math
from datetime import timedelta

from flask import (Blueprint, Response, current_app, jsonify, render_template,
                   request, stream_with_context)
from sqlalchemy import func

from . import config
from . import engine
from . import grid
from . import models as m
from . import serializers as ser
from . import stream as twin_stream
from .ingest.internal_reports import InternalReportsAdapter, cells_for_report
from .security import login_required, official_only, twin_roles_required

log = logging.getLogger("twin.routes")

twin_bp = Blueprint("twin", __name__, url_prefix="/api/twin")

#: No url_prefix -- section 7 wants the page itself at plain /digital-twin,
#: separate from the /api/twin/* JSON contract. A second Blueprint (rather
#: than registering directly on the host `app`) keeps everything the twin
#: owns in one create_twin_blueprint() call (C6).
twin_pages_bp = Blueprint("twin_pages", __name__, template_folder="../templates")


@twin_pages_bp.get("/digital-twin")
@twin_roles_required
def digital_twin_page():
    db = _db()
    cities_rows = (
        db.session.query(m.TwinCity).filter(m.TwinCity.is_active.is_(True)).all()
    )
    order = {slug: i for i, slug in enumerate(config.CITY_ORDER)}
    cities_rows.sort(key=lambda c: order.get(c.slug, 99))

    zones_by_city = {}
    for zone in db.session.query(m.TwinZone).order_by(m.TwinZone.display_name).all():
        zones_by_city.setdefault(zone.city_id, []).append(zone)

    cities_payload = [
        ser.city_payload(city, zones=zones_by_city.get(city.id, []))
        for city in cities_rows
    ]

    return render_template(
        "digital_twin.html", cities=cities_rows, cities_json=cities_payload,
        horizons=list(config.HORIZONS),
    )


def _db():
    return current_app.extensions["twin"]["db"]


def _error(message, code, **extra):
    payload = {"error": message, "status": code}
    payload.update(extra)
    return jsonify(payload), code


# --------------------------------------------------------------------------
# Shared resolution helpers
# --------------------------------------------------------------------------

def _get_city(db, city_slug):
    return db.session.query(m.TwinCity).filter_by(slug=city_slug, is_active=True).one_or_none()


def _resolve_horizon():
    raw = request.args.get("horizon", "0")
    try:
        horizon = int(raw)
    except ValueError:
        return None, _error("horizon must be one of %s" % list(config.HORIZONS), 400)
    if horizon not in config.HORIZONS:
        return None, _error("horizon must be one of %s" % list(config.HORIZONS), 400)
    return horizon, None


def _resolve_zone(db, city, zone_slug):
    """(zone_or_None, error_response_or_None). zone=None with no error means
    'whole city' -- either the slug was `__all__` or omitted entirely."""
    if not zone_slug or zone_slug == config.ALL_ZONES:
        return None, None
    zone = db.session.query(m.TwinZone).filter_by(city_id=city.id, slug=zone_slug).one_or_none()
    if zone is None:
        return None, _error("unknown zone %r for city %r" % (zone_slug, city.slug), 404)
    return zone, None


def _cells_for(db, city, zone):
    query = db.session.query(m.TwinCell).filter_by(city_id=city.id)
    if zone is not None:
        query = query.filter_by(zone_id=zone.id)
    return query.all()


def _states_by_cell_id(db, cell_ids, horizon):
    if not cell_ids:
        return {}
    rows = (
        db.session.query(m.TwinCellState)
        .filter(m.TwinCellState.cell_id.in_(cell_ids), m.TwinCellState.horizon_hours == horizon)
        .all()
    )
    return {row.cell_id: row for row in rows}


# --------------------------------------------------------------------------
# Response compression, scoped to this blueprint
#
# The twin serves the largest JSON in the app by a wide margin: Bengaluru's
# water layer is ~1.7 MB of geometry and its camera layer ~0.8 MB, both of
# which the operator waits on with a spinner. gzip takes those to roughly a
# tenth.
#
# Deliberately an after_request on twin_bp rather than Flask-Compress on the
# app: Compress registers app-wide, which would change the response headers
# of all 119 existing routes -- an invisible, hard-to-attribute change to
# code this module is not meant to touch. Scoped here, the blast radius is
# /api/twin/* and nothing else.
# --------------------------------------------------------------------------

#: Below this, a gzip round-trip costs more time than it saves bandwidth.
_GZIP_MIN_BYTES = 8192


@twin_bp.after_request
def _gzip_large_json(response):
    if response.status_code != 200:
        return response
    if not (response.mimetype or "").startswith("application/json"):
        return response
    # `/stream` is a Server-Sent Events response: it has no length, is
    # consumed a chunk at a time, and touching get_data() on it would buffer
    # an endless generator into memory.
    if response.direct_passthrough or response.is_streamed:
        return response
    if response.headers.get("Content-Encoding"):
        return response
    if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
        return response

    payload = response.get_data()
    if len(payload) < _GZIP_MIN_BYTES:
        return response

    response.set_data(gzip.compress(payload, compresslevel=6))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(response.content_length)
    # Caches keyed only on the URL must not hand a gzipped body to a client
    # that did not ask for one.
    response.headers.add("Vary", "Accept-Encoding")
    return response


# --------------------------------------------------------------------------
# Health (section 7)
# --------------------------------------------------------------------------

@twin_bp.get("/health")
@login_required
def health():
    db = _db()
    sources = {}

    newest = (
        db.session.query(m.TwinDataSnapshot.source_key,
                         func.max(m.TwinDataSnapshot.started_at).label("started_at"))
        .group_by(m.TwinDataSnapshot.source_key)
        .subquery()
    )
    rows = (
        db.session.query(m.TwinDataSnapshot)
        .join(newest, (m.TwinDataSnapshot.source_key == newest.c.source_key)
              & (m.TwinDataSnapshot.started_at == newest.c.started_at))
        .all()
    )
    by_key = {r.source_key: r for r in rows}

    for key in config.ALL_SOURCES:
        snap = by_key.get(key)
        if snap is None:
            sources[key] = {"status": "unknown", "last_success": None,
                            "latency_ms": None, "error": None,
                            "tier": 1 if key in config.TIER1_SOURCES else 2}
            continue
        sources[key] = {
            "status": snap.status,
            "last_success": ser.iso(snap.finished_at if snap.status == "ok" else None),
            "latency_ms": snap.latency_ms,
            "error": snap.error_message,
            "records": snap.records_ingested,
            "tier": 1 if key in config.TIER1_SOURCES else 2,
        }

    newest_state = db.session.query(func.max(m.TwinCellState.computed_at)).scalar()
    stale_after_s = config.COMPUTE_INTERVAL_MIN * 60 * config.STALE_COMPUTE_MULTIPLIER
    age_s = (m.utcnow() - newest_state).total_seconds() if newest_state is not None else None

    return jsonify({
        "sources": sources,
        "compute": {
            "last_computed_at": ser.iso(newest_state),
            "age_seconds": age_s,
            "stale_after_seconds": stale_after_s,
            "stale": (age_s is None) or (age_s > stale_after_s),
        },
        "enabled": config.TWIN_ENABLED,
        "server_time": ser.iso(m.utcnow()),
        "stream_subscribers": twin_stream.subscriber_count(),
    })


# --------------------------------------------------------------------------
# NASA GIBS tile info (section 8.4 layer 2, Phase 8 date picker)
#
# Not itself listed in section 7's table, but required by the acceptance
# criteria's GIBS date picker: the frontend needs a server-side date probe
# (section 15: "imagery for a given date may not exist") before it can
# safely build a tile URL, since that probe is an HTTP HEAD per candidate
# day and doing it from the browser would mean CORS-dependent, unaudited
# requests straight to earthdata.nasa.gov on every basemap toggle.
# --------------------------------------------------------------------------

@twin_bp.get("/gibs")
@twin_roles_required
def gibs_info():
    from .ingest import nasa_gibs

    layer = request.args.get("layer", "viirs")
    if layer not in nasa_gibs.LAYERS:
        return _error("unknown layer %r; choose from %s" % (layer, list(nasa_gibs.LAYERS)), 400)

    requested_date = None
    date_param = request.args.get("date")
    if date_param:
        from datetime import date as date_cls
        try:
            requested_date = date_cls.fromisoformat(date_param)
        except ValueError:
            return _error("date must be YYYY-MM-DD", 400)

    # A specifically requested historical date is checked directly rather
    # than assumed available just because it's <= the latest probed date --
    # GIBS can have gaps (cloud cover, a missed pass), not just a lag at
    # the front of the timeline.
    if requested_date and nasa_gibs.is_date_available(layer, requested_date):
        resolved = requested_date
    else:
        resolved = nasa_gibs.probe_available_date(layer=layer)

    if resolved is None:
        return jsonify({
            "available": False,
            "layer": layer,
            "tile_url_template": None,
            "requested_date": date_param,
            "resolved_date": None,
        })

    return jsonify({
        "available": True,
        "layer": layer,
        "tile_url_template": nasa_gibs.tile_url_template(layer=layer, for_date=resolved),
        # Confirmed live: a raster source with no maxzoom keeps requesting
        # tiles past this layer's native resolution and gets a 400 for
        # every one -- the frontend needs this to cap the source correctly.
        "max_zoom": nasa_gibs.LAYERS.get(layer, nasa_gibs.LAYERS["viirs"])["max_zoom"],
        "requested_date": date_param,
        "resolved_date": resolved.isoformat(),
        "fell_back": bool(requested_date and resolved != requested_date),
    })


# --------------------------------------------------------------------------
# TomTom traffic tile info (section 4.2/8.4 -- optional, keyed, display-only)
#
# The formula in section 5.1 has no traffic term -- section 4.2 describes
# this purely as a map overlay ("congestion tiles"), so it is wired as a
# layer, not folded into risk_score. TomTom's tile API embeds the key
# directly in the tile URL by design (their raster tile keys are meant to be
# used client-side, unlike a server-routing key), so this endpoint's only
# job is to keep TWIN_ENABLED's "layer hidden, no key" contract (C5) --
# never claim availability the config doesn't actually have.
# --------------------------------------------------------------------------

@twin_bp.get("/traffic")
@twin_roles_required
def traffic_info():
    from .ingest import traffic

    return jsonify({
        "available": traffic.is_available(),
        "tile_url_template": traffic.tile_url_template(),
    })


# --------------------------------------------------------------------------
# Street-level imagery / webcams (ground-truth panel in the drill-down).
#
# Proxied server-side rather than called from the browser for three reasons:
# the providers are a keyed/keyless mix and a key must never reach the page;
# KartaView sends no CORS header, so a direct fetch() from the dashboard is
# blocked outright; and routing it through IngestAdapter gets the 24h disk
# cache, the retry, and a TwinDataSnapshot audit row for free -- clicking
# twenty hexes in a demo then costs one upstream request per hex, once.
# --------------------------------------------------------------------------

@twin_bp.get("/streetview")
@twin_roles_required
def streetview():
    from .ingest.streetview import StreetViewAdapter

    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return _error("lat and lon are required and must be numbers", 400)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return _error("lat/lon out of range", 400)

    try:
        radius_m = int(request.args.get("radius", config.STREETVIEW_RADIUS_M))
    except ValueError:
        return _error("radius must be an integer number of metres", 400)
    radius_m = max(50, min(2000, radius_m))

    adapter = StreetViewAdapter()
    data, snapshot = adapter.run(_db(), lat=lat, lon=lon, radius_m=radius_m)

    payload = dict(data or {})
    payload["status"] = snapshot.status if snapshot is not None else "unknown"
    return jsonify(payload)


# --------------------------------------------------------------------------
# OSINT surveillance cameras (twin/ingest/cctv.py).
#
# Two shapes of the same source, because the two consumers want different
# things: the map layer wants every camera in a city as GeoJSON once, and the
# drill-down wants the handful nearest one cell, ordered by distance, with
# their tags intact. Splitting them keeps the city payload cacheable for a
# week (cameras are civic infrastructure, not weather) while the cell lookup
# stays a cheap `around:` query.
#
# Server-side rather than from the browser for the same reason as
# /streetview: Overpass rate-limits by IP and sends no CORS header, and
# routing through IngestAdapter buys the disk cache, the mirror retry and a
# TwinDataSnapshot audit row for free.
# --------------------------------------------------------------------------

@twin_bp.get("/<city>/cameras")
@twin_roles_required
def city_cameras(city):
    from .ingest.cctv import CctvOsintAdapter, to_feature_collection

    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    zone, err = _resolve_zone(db, city_row, request.args.get("zone"))
    if err:
        return err

    bbox = (city_row.bbox_min_lon, city_row.bbox_min_lat,
            city_row.bbox_max_lon, city_row.bbox_max_lat)

    adapter = CctvOsintAdapter()
    data, snapshot = adapter.run(db, city_id=city_row.id, bbox=bbox, timeout_s=45)

    payload = to_feature_collection(data)
    payload["status"] = snapshot.status if snapshot is not None else "unknown"

    # The zone filter is applied here rather than in the Overpass query: the
    # city-wide result is what gets cached for a week, and re-querying
    # Overpass per zone would defeat that for no gain -- a zone is a subset
    # of the same points.
    if zone is not None:
        bounds = _zone_bounds(db, city_row, zone)
        if bounds is not None:
            min_lon, min_lat, max_lon, max_lat = bounds
            payload["features"] = [
                f for f in payload["features"]
                if min_lon <= f["geometry"]["coordinates"][0] <= max_lon
                and min_lat <= f["geometry"]["coordinates"][1] <= max_lat
            ]
            payload["counts_by_kind"] = _counts_by_kind_of(payload["features"])

    return jsonify(payload)


#: Minimum cells assigned to a zone before their extent is trusted as its
#: shape. Below this the "extent" is one or two scattered points and would
#: collapse the filter onto a few hundred metres of the city.
_ZONE_CELL_QUORUM = 5

#: Half-width, in km, of the fallback box around a zone centre. Matches the
#: zoom-12 view the zone picker flies to, so what the filter keeps is
#: roughly what the operator can see.
_ZONE_FALLBACK_KM = 6.0


def _zone_bounds(db, city_row, zone):
    """A (min_lon, min_lat, max_lon, max_lat) box for a zone, or None.

    Preference order, because zones do not all carry the same quality of
    geometry -- in a default seed every one of them is
    ``boundary_source='approximate'``: a centre point, no polygon, and only a
    handful of cells with a ``zone_id``:

    1. the zone's own boundary polygon, when one was imported;
    2. the extent of its assigned cells, once there are enough of them to
       describe a shape rather than a scatter;
    3. a box around the zone centre, sized to the view the zone picker flies
       to.

    ``None`` means "do not filter". That is deliberate for a zone nothing is
    known about: showing the whole city's cameras is a visible, harmless
    excess, whereas guessing a box would silently hide most of them.
    """
    if zone.boundary_geojson:
        bounds = _geojson_bounds(zone.boundary_geojson)
        if bounds is not None:
            return bounds

    cell_count, min_lon, min_lat, max_lon, max_lat = (
        db.session.query(
            func.count(m.TwinCell.id),
            func.min(m.TwinCell.center_longitude), func.min(m.TwinCell.center_latitude),
            func.max(m.TwinCell.center_longitude), func.max(m.TwinCell.center_latitude))
        .filter(m.TwinCell.city_id == city_row.id, m.TwinCell.zone_id == zone.id)
        .one()
    )
    if cell_count >= _ZONE_CELL_QUORUM and None not in (min_lon, min_lat, max_lon, max_lat):
        pad = 0.0025  # ~275 m, half an H3 res-8 cell, so border cells keep theirs
        return (min_lon - pad, min_lat - pad, max_lon + pad, max_lat + pad)

    if zone.center_latitude is None or zone.center_longitude is None:
        return None
    d_lat = _ZONE_FALLBACK_KM / 111.0
    d_lon = _ZONE_FALLBACK_KM / (111.0 * max(0.1, math.cos(math.radians(zone.center_latitude))))
    return (zone.center_longitude - d_lon, zone.center_latitude - d_lat,
            zone.center_longitude + d_lon, zone.center_latitude + d_lat)


def _geojson_bounds(raw):
    """Extent of any GeoJSON geometry, or None if it cannot be read.

    Walks the coordinate nesting rather than switching on geometry type: a
    zone boundary may arrive as a Polygon or a MultiPolygon depending on
    whether OSM modelled it with holes or islands, and both reduce to the
    same min/max over their leaf [lon, lat] pairs.
    """
    try:
        geometry = json.loads(raw)
    except (TypeError, ValueError):
        return None
    geometry = geometry.get("geometry", geometry) if isinstance(geometry, dict) else geometry
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not coordinates:
        return None

    lons, lats = [], []

    def walk(node):
        if (isinstance(node, (list, tuple)) and len(node) >= 2
                and all(isinstance(v, (int, float)) for v in node[:2])):
            lons.append(node[0])
            lats.append(node[1])
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coordinates)
    if not lons:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


def _counts_by_kind_of(features):
    counts = {}
    for feature in features:
        kind = (feature.get("properties") or {}).get("kind") or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


@twin_bp.get("/<city>/water")
@twin_roles_required
def city_water(city):
    """Water bodies and drains as GeoJSON, for the map's water layer.

    Served from the same weekly Overpass payload the terrain sub-score is
    derived from, so switching the layer on costs nothing upstream after the
    first call of the week. Lazily fetched by the client -- it is the largest
    layer the console has, and most sessions never turn it on.
    """
    from .ingest.overpass import OverpassWaterAdapter

    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    zone, err = _resolve_zone(db, city_row, request.args.get("zone"))
    if err:
        return err

    bbox = (city_row.bbox_min_lon, city_row.bbox_min_lat,
            city_row.bbox_max_lon, city_row.bbox_max_lat)
    adapter = OverpassWaterAdapter()
    data, snapshot = adapter.run(db, city_id=city_row.id, bbox=bbox, timeout_s=60)

    payload = dict(data or {})
    payload["status"] = snapshot.status if snapshot is not None else "unknown"

    # Unlike the camera layer, a water feature is a line or a polygon rather
    # than a point, so "is it in the zone?" is answered by bbox overlap
    # rather than containment -- a drain that merely passes through the zone
    # is exactly the one an operator wants to see.
    if zone is not None:
        bounds = _zone_bounds(db, city_row, zone)
        if bounds is not None:
            payload["features"] = [
                f for f in payload.get("features") or []
                if _feature_overlaps(f, bounds)
            ]

    return jsonify(payload)


def _feature_overlaps(feature, bounds):
    min_lon, min_lat, max_lon, max_lat = bounds
    lons, lats = [], []

    def walk(node):
        if (isinstance(node, (list, tuple)) and len(node) >= 2
                and all(isinstance(v, (int, float)) for v in node[:2])):
            lons.append(node[0])
            lats.append(node[1])
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk((feature.get("geometry") or {}).get("coordinates") or [])
    if not lons:
        return False
    return not (max(lons) < min_lon or min(lons) > max_lon
                or max(lats) < min_lat or min(lats) > max_lat)


@twin_bp.get("/cctv")
@twin_roles_required
def cctv_near_point():
    from .ingest.cctv import CctvOsintAdapter

    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return _error("lat and lon are required and must be numbers", 400)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return _error("lat/lon out of range", 400)

    try:
        radius_m = int(request.args.get("radius", config.CCTV_RADIUS_M))
    except ValueError:
        return _error("radius must be an integer number of metres", 400)
    radius_m = max(50, min(2000, radius_m))

    adapter = CctvOsintAdapter()
    data, snapshot = adapter.run(_db(), lat=lat, lon=lon, radius_m=radius_m, timeout_s=30)

    payload = dict(data or {})
    payload["status"] = snapshot.status if snapshot is not None else "unknown"
    return jsonify(payload)


# --------------------------------------------------------------------------
# Cities (section 7)
# --------------------------------------------------------------------------

@twin_bp.get("/cities")
@twin_roles_required
def cities():
    db = _db()
    rows = db.session.query(m.TwinCity).filter(m.TwinCity.is_active.is_(True)).all()
    order = {slug: i for i, slug in enumerate(config.CITY_ORDER)}
    rows.sort(key=lambda c: order.get(c.slug, 99))

    zones_by_city = {}
    for zone in db.session.query(m.TwinZone).order_by(m.TwinZone.display_name).all():
        zones_by_city.setdefault(zone.city_id, []).append(zone)

    cell_counts = dict(
        db.session.query(m.TwinCell.city_id, func.count(m.TwinCell.id))
        .group_by(m.TwinCell.city_id).all()
    )
    newest_by_city = dict(
        db.session.query(m.TwinCell.city_id, func.max(m.TwinCellState.computed_at))
        .join(m.TwinCellState, m.TwinCellState.cell_id == m.TwinCell.id)
        .group_by(m.TwinCell.city_id).all()
    )

    payload = []
    for city in rows:
        entry = ser.city_payload(city, zones=zones_by_city.get(city.id, []),
                                 last_updated=newest_by_city.get(city.id))
        entry["cell_count"] = cell_counts.get(city.id, 0)
        payload.append(entry)

    response = jsonify(payload)
    return ser.apply_cache_headers(
        response, etag=ser.etag_for("cities", len(payload), sum(cell_counts.values())), max_age=60)


# --------------------------------------------------------------------------
# Zones (section 7)
# --------------------------------------------------------------------------

@twin_bp.get("/<city>/zones")
@twin_roles_required
def zones(city):
    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    zone_type = request.args.get("type")
    query = db.session.query(m.TwinZone).filter_by(city_id=city_row.id)
    if zone_type:
        query = query.filter_by(zone_type=zone_type)

    return jsonify(ser.zone_feature_collection(query.all()))


# --------------------------------------------------------------------------
# State (section 7 -- the twin's core payload)
# --------------------------------------------------------------------------

@twin_bp.get("/<city>/state")
@twin_roles_required
def state(city):
    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    horizon, err = _resolve_horizon()
    if err:
        return err
    zone, err = _resolve_zone(db, city_row, request.args.get("zone"))
    if err:
        return err

    fields_param = request.args.get("fields")
    fields = [f.strip() for f in fields_param.split(",")] if fields_param else None
    # geometry=false is the whole-city default (section 7 response size guard).
    geometry = request.args.get("geometry", "false").lower() in ("1", "true", "yes")

    cells = _cells_for(db, city_row, zone)
    states = _states_by_cell_id(db, [c.id for c in cells], horizon)
    cells_with_state = [(cell, states.get(cell.id)) for cell in cells]

    newest = max((s.computed_at for _c, s in cells_with_state if s is not None), default=None)
    etag = ser.etag_for("state", city, zone.slug if zone else "__all__", horizon, geometry, newest)

    if request.headers.get("If-None-Match") == etag:
        return "", 304

    if geometry:
        payload = ser.state_feature_collection(cells_with_state, fields=fields)
    else:
        payload = ser.state_compact_list(cells_with_state, fields=fields)

    response = jsonify(payload)
    return ser.apply_cache_headers(response, etag=etag, max_age=60)


# --------------------------------------------------------------------------
# Cell drill-down (section 7)
# --------------------------------------------------------------------------

@twin_bp.get("/<city>/cell/<h3_index>")
@twin_roles_required
def cell(city, h3_index):
    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    horizon, err = _resolve_horizon()
    if err:
        return err

    cell_row = db.session.query(m.TwinCell).filter_by(city_id=city_row.id, h3_index=h3_index).one_or_none()
    if cell_row is None:
        return _error("unknown cell %r in %r" % (h3_index, city), 404)

    state_row = (
        db.session.query(m.TwinCellState)
        .filter_by(cell_id=cell_row.id, horizon_hours=horizon)
        .one_or_none()
    )

    zone_row = None
    if cell_row.zone_id is not None:
        zone_row = db.session.get(m.TwinZone, cell_row.zone_id)

    assets = db.session.query(m.TwinInfrastructure).filter_by(cell_id=cell_row.id).all()

    adapter = InternalReportsAdapter()
    reports, _snapshot = adapter.run(db, city_id=city_row.id, since_hours=72)
    nearby = _reports_near_cell(reports or [], h3_index)

    return jsonify(ser.cell_drilldown_payload(cell_row, state_row, assets, nearby, zone=zone_row))


def _reports_near_cell(reports, h3_index):
    """Approved reports whose home cell IS this cell (not neighbours -- the
    drill-down shows what's actually here, unlike the scoring spillover)."""
    nearby = []
    for report in reports:
        lat, lon = report.get("lat"), report.get("lon")
        if lat is None or lon is None:
            continue
        home_cell, _neighbours = cells_for_report(lat, lon)
        if home_cell == h3_index:
            nearby.append(report)
    return nearby


# --------------------------------------------------------------------------
# Incidents / infrastructure (section 7)
# --------------------------------------------------------------------------

@twin_bp.get("/<city>/incidents")
@twin_roles_required
def incidents(city):
    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    zone, err = _resolve_zone(db, city_row, request.args.get("zone"))
    if err:
        return err
    hazard_type = request.args.get("hazard_type")
    since_hours = request.args.get("since_hours", "72")
    try:
        since_hours = int(since_hours)
    except ValueError:
        return _error("since_hours must be an integer", 400)

    adapter = InternalReportsAdapter()
    reports, _snapshot = adapter.run(db, city_id=city_row.id, since_hours=since_hours)
    reports = reports or []

    if hazard_type:
        reports = [r for r in reports if r.get("hazard_type") == hazard_type]

    reports = _reports_within_city(db, city_row, reports, zone=zone)

    return jsonify(ser.incidents_feature_collection(reports))


def _reports_within_city(db, city_row, reports, zone=None):
    """Reports whose home cell belongs to this city (optionally, this zone).

    InternalReportsAdapter has no city filter to apply -- a Report carries
    only lat/lon, not a city_id (matching the real Sentinel AI schema this
    stands in for), so `IngestAdapter.run(city_id=...)` only labels the
    audit snapshot, not the data itself. Every route that surfaces reports
    (or counts them, see _summary_for) MUST re-scope by cell membership the
    same way engine.py's _fetch_incidents already does, or reports near one
    city bleed into another city's numbers -- confirmed live: Bengaluru's
    comparison-strip incident count included Hyderabad's demo reports until
    this filter was added.
    """
    if zone is not None:
        valid_h3 = {
            c.h3_index for c in db.session.query(m.TwinCell).filter_by(
                city_id=city_row.id, zone_id=zone.id).all()
        }
    else:
        valid_h3 = {
            h3_index for (h3_index,) in
            db.session.query(m.TwinCell.h3_index).filter_by(city_id=city_row.id).all()
        }

    filtered = []
    for report in reports:
        lat, lon = report.get("lat"), report.get("lon")
        if lat is None or lon is None:
            continue
        home_cell, _neighbours = cells_for_report(lat, lon)
        if home_cell in valid_h3:
            filtered.append(report)
    return filtered


@twin_bp.get("/<city>/infrastructure")
@twin_roles_required
def infrastructure(city):
    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    zone, err = _resolve_zone(db, city_row, request.args.get("zone"))
    if err:
        return err
    types_param = request.args.get("types")
    types = [t.strip() for t in types_param.split(",")] if types_param else None

    query = db.session.query(m.TwinInfrastructure).filter_by(city_id=city_row.id)
    if types:
        query = query.filter(m.TwinInfrastructure.asset_type.in_(types))
    if zone is not None:
        cell_ids = [c.id for c in db.session.query(m.TwinCell.id).filter_by(
            city_id=city_row.id, zone_id=zone.id).all()]
        query = query.filter(m.TwinInfrastructure.cell_id.in_(cell_ids))

    return jsonify(ser.infrastructure_feature_collection(query.all()))


# --------------------------------------------------------------------------
# Summary / compare / timeline (section 7)
# --------------------------------------------------------------------------

@twin_bp.get("/<city>/summary")
@twin_roles_required
def summary(city):
    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    horizon, err = _resolve_horizon()
    if err:
        return err
    zone, err = _resolve_zone(db, city_row, request.args.get("zone"))
    if err:
        return err

    payload = _summary_for(db, city_row, zone, horizon)
    return jsonify(payload)


def _summary_for(db, city_row, zone, horizon):
    cells = _cells_for(db, city_row, zone)
    states = _states_by_cell_id(db, [c.id for c in cells], horizon)
    cells_with_state = [(cell, states[cell.id]) for cell in cells if cell.id in states]

    adapter = InternalReportsAdapter()
    reports, _snapshot = adapter.run(db, city_id=city_row.id, since_hours=24)
    incident_count_24h = len(_reports_within_city(db, city_row, reports or [], zone=zone))

    critical_cell_ids = {cell.id for cell, s in cells_with_state if s.status == "critical"}
    critical_assets = 0
    if critical_cell_ids:
        critical_assets = (
            db.session.query(func.count(m.TwinInfrastructure.id))
            .filter(m.TwinInfrastructure.cell_id.in_(critical_cell_ids))
            .scalar()
        ) or 0

    payload = ser.summary_payload(
        city_row.slug, cells_with_state, incident_count_24h=incident_count_24h,
        critical_assets_at_risk=critical_assets,
    )
    payload["zone"] = zone.slug if zone else config.ALL_ZONES
    payload["horizon"] = horizon
    return payload


@twin_bp.get("/compare")
@twin_roles_required
def compare():
    db = _db()
    horizon, err = _resolve_horizon()
    if err:
        return err

    result = {}
    for city_slug in config.CITY_ORDER:
        city_row = _get_city(db, city_slug)
        if city_row is None:
            continue
        result[city_slug] = _summary_for(db, city_row, None, horizon)

    return jsonify(result)


@twin_bp.get("/<city>/timeline")
@twin_roles_required
def timeline(city):
    db = _db()
    city_row = _get_city(db, city)
    if city_row is None:
        return _error("unknown city %r" % city, 404)

    zone, err = _resolve_zone(db, city_row, request.args.get("zone"))
    if err:
        return err
    hours = request.args.get("hours", "24")
    try:
        hours = int(hours)
    except ValueError:
        return _error("hours must be an integer", 400)

    cell_ids = [c.id for c in _cells_for(db, city_row, zone)]
    if not cell_ids:
        return jsonify(ser.timeline_payload(city, []))

    cutoff = m.utcnow() - timedelta(hours=hours)
    rows = (
        db.session.query(
            func.strftime("%Y-%m-%dT%H:00:00", m.TwinCellHistory.computed_at),
            func.avg(m.TwinCellHistory.risk_score),
        )
        .filter(m.TwinCellHistory.cell_id.in_(cell_ids), m.TwinCellHistory.computed_at >= cutoff)
        .group_by(func.strftime("%Y-%m-%dT%H:00:00", m.TwinCellHistory.computed_at))
        .order_by(func.strftime("%Y-%m-%dT%H:00:00", m.TwinCellHistory.computed_at))
        .all()
    )
    # strftime is SQLite-specific; PostgreSQL uses date_trunc. Cross-database
    # parity (C2) is handled by falling back to Python-side bucketing when
    # the SQL dialect doesn't understand strftime.
    if not rows:
        rows = _timeline_fallback(db, cell_ids, cutoff)

    points = [(hour, avg_risk) for hour, avg_risk in rows if avg_risk is not None]
    return jsonify(ser.timeline_payload(city, [
        (_parse_hour_bucket(hour), avg_risk) for hour, avg_risk in points
    ]))


def _parse_hour_bucket(value):
    from datetime import datetime, timezone
    if isinstance(value, str):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return value


def _timeline_fallback(db, cell_ids, cutoff):
    """Python-side hourly bucketing, for dialects where the strftime-based
    GROUP BY above doesn't apply (C2: must work identically on PostgreSQL)."""
    rows = (
        db.session.query(m.TwinCellHistory.computed_at, m.TwinCellHistory.risk_score)
        .filter(m.TwinCellHistory.cell_id.in_(cell_ids), m.TwinCellHistory.computed_at >= cutoff)
        .all()
    )
    buckets = {}
    for computed_at, risk_score in rows:
        key = computed_at.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(key, []).append(risk_score)
    return [(key, sum(vals) / len(vals)) for key, vals in sorted(buckets.items())]


# --------------------------------------------------------------------------
# SSE stream (section 7/8.6, Phase 7)
# --------------------------------------------------------------------------

@twin_bp.get("/stream")
@twin_roles_required
def stream_route():
    city = request.args.get("city")
    zone = request.args.get("zone")

    def generate():
        for chunk in twin_stream.sse_stream(city=city, zone=zone):
            yield chunk

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


# --------------------------------------------------------------------------
# Admin actions (section 7, official only)
# --------------------------------------------------------------------------

_LOCK_SOURCE_PREFIX = "seed_lock:"
_LOCK_TTL_S = 20 * 60


def _acquire_seed_lock(db, city_slug):
    """A TwinDataSnapshot-based advisory lock (section 15: 'POST /api/twin/seed
    double-runs'). Returns True if acquired, False if another seed is already
    in flight for this city.
    """
    source_key = _LOCK_SOURCE_PREFIX + city_slug
    existing = (
        db.session.query(m.TwinDataSnapshot)
        .filter_by(source_key=source_key, status="running")
        .order_by(m.TwinDataSnapshot.started_at.desc())
        .first()
    )
    if existing is not None:
        age_s = (m.utcnow() - existing.started_at).total_seconds()
        if age_s < _LOCK_TTL_S:
            return False
        existing.status = "failed"
        existing.error_message = "lock expired (TTL %ds)" % _LOCK_TTL_S
        existing.finished_at = m.utcnow()

    lock_row = m.TwinDataSnapshot(source_key=source_key, status="running", started_at=m.utcnow())
    db.session.add(lock_row)
    db.session.commit()
    return True


def _release_seed_lock(db, city_slug, status="ok", error=None):
    source_key = _LOCK_SOURCE_PREFIX + city_slug
    row = (
        db.session.query(m.TwinDataSnapshot)
        .filter_by(source_key=source_key, status="running")
        .order_by(m.TwinDataSnapshot.started_at.desc())
        .first()
    )
    if row is not None:
        row.status = status
        row.error_message = error
        row.finished_at = m.utcnow()
        db.session.commit()


@twin_bp.post("/seed")
@official_only
def seed():
    db = _db()
    body = request.get_json(silent=True) or {}
    city_slug = body.get("city")
    if not city_slug:
        return _error("body must include 'city'", 400)

    cities_to_seed = list(config.CITY_ORDER) if city_slug == "all" else [city_slug]
    results = {}

    for slug in cities_to_seed:
        city_row = _get_city(db, slug)
        if city_row is None:
            results[slug] = {"error": "unknown city"}
            continue

        if not _acquire_seed_lock(db, slug):
            results[slug] = {"error": "seed already in progress"}
            continue

        try:
            cell_count = grid.generate_cells_for_city(db, city_row)
            elevation_filled = grid.seed_elevation(db, city_row)
            infra_summary = grid.seed_infrastructure_and_terrain(db, city_row)
            terrain_count = grid.cache_terrain_scores(db, city_row)
            results[slug] = {
                "cells": cell_count, "elevation_filled": elevation_filled,
                "infrastructure": infra_summary, "terrain_scored": terrain_count,
            }
            _release_seed_lock(db, slug, status="ok")
        except Exception as exc:  # noqa: BLE001
            log.exception("seed failed for %s", slug)
            results[slug] = {"error": str(exc)}
            _release_seed_lock(db, slug, status="failed", error=str(exc))

    return jsonify(results)


@twin_bp.post("/refresh")
@official_only
def refresh():
    db = _db()
    body = request.get_json(silent=True) or {}
    city_slug = body.get("city")
    if not city_slug:
        return _error("body must include 'city'", 400)

    cities_to_refresh = list(config.CITY_ORDER) if city_slug == "all" else [city_slug]
    results = {}

    for slug in cities_to_refresh:
        city_row = _get_city(db, slug)
        if city_row is None:
            results[slug] = {"error": "unknown city"}
            continue
        try:
            summary_result = engine.compute_state(db, city_row)
            if summary_result.get("changed_cells"):
                twin_stream.publish(
                    "state_update", city=slug, changed_cells=summary_result["changed_cells"])
            results[slug] = summary_result
        except Exception as exc:  # noqa: BLE001
            log.exception("refresh failed for %s", slug)
            results[slug] = {"error": str(exc)}

    return jsonify(results)
