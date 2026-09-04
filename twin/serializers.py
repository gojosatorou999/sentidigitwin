"""JSON/GeoJSON builders and cache headers (section 7)."""

import hashlib
import json

from . import config

#: Coordinate quantisation for GeoJSON rings (section 7, response size guard).
COORD_PRECISION = 5


def quantise_ring(ring, precision=COORD_PRECISION):
    """Round a [[lng, lat], ...] ring to `precision` decimal places."""
    return [[round(float(lng), precision), round(float(lat), precision)]
            for lng, lat in ring]


def iso(dt):
    """Serialise a datetime as UTC ISO-8601 with a trailing Z (C8)."""
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def city_payload(city, zones=(), health=None, last_updated=None):
    """One entry of ``GET /api/twin/cities``."""
    zone_entries = [
        {
            "slug": z.slug,
            "display_name": z.display_name,
            "zone_type": z.zone_type,
            "parent": None,
            "boundary_source": z.boundary_source,
            "has_boundary": bool(z.boundary_geojson),
            "center": [z.center_latitude, z.center_longitude],
        }
        for z in zones
    ]
    # The synthetic "Whole City" entry is always first and is the default
    # selection in both dropdowns (section 2.2).
    zone_entries.insert(0, {
        "slug": config.ALL_ZONES,
        "display_name": "Whole City",
        "zone_type": "synthetic",
        "parent": None,
        "boundary_source": None,
        "has_boundary": True,
        "center": [city.center_latitude, city.center_longitude],
    })

    return {
        "slug": city.slug,
        "display_name": city.display_name,
        "state": city.state,
        "country": city.country,
        "zone_scheme": city.zone_scheme,
        "h3_resolution": city.h3_resolution,
        "center": [city.center_latitude, city.center_longitude],
        "bbox": [city.bbox_min_lon, city.bbox_min_lat,
                 city.bbox_max_lon, city.bbox_max_lat],
        "camera": {
            "zoom": city.default_zoom,
            "pitch": city.default_pitch,
            "bearing": city.default_bearing,
        },
        "zones": zone_entries,
        "cell_count": 0,
        "last_updated": iso(last_updated),
        "health": health or {},
    }


def etag_for(*parts):
    """Weak ETag from whatever identifies a payload version (section 7)."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x1f")
    return 'W/"%s"' % digest.hexdigest()[:32]


def payload_digest(obj):
    """Stable digest of an ingest payload, for TwinDataSnapshot (C7)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def apply_cache_headers(response, etag=None, max_age=60):
    """Cache-Control + ETag (section 7)."""
    if etag:
        response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=%d" % max_age
    return response


# --------------------------------------------------------------------------
# State (hexagon) payloads (section 7 /state route + response size guard)
# --------------------------------------------------------------------------

_STATE_FIELDS = (
    "h3", "risk_score", "status", "hazard_score", "vulnerability_multiplier",
    "hydro_score", "incident_score", "terrain_score", "infra_score", "env_score",
    "incident_count", "degraded_inputs", "computed_at",
)


def cell_state_properties(cell, state, fields=None):
    """The property dict for one cell+state pair, filtered to `fields` if given."""
    colour, opacity, height_m = config.band_for(state.risk_score if state else 0)[1:]
    full = {
        "h3": cell.h3_index,
        "risk_score": round(state.risk_score, 1) if state and state.risk_score is not None else 0.0,
        "status": state.status if state else "normal",
        "hazard_score": _round_or_none(state.hazard_score if state else None),
        "vulnerability_multiplier": _round_or_none(state.vulnerability_multiplier if state else None, 3),
        "hydro_score": _round_or_none(state.hydro_score if state else None),
        "incident_score": _round_or_none(state.incident_score if state else None),
        "terrain_score": _round_or_none(state.terrain_score if state else None),
        "infra_score": _round_or_none(state.infra_score if state else None),
        "env_score": _round_or_none(state.env_score if state else None),
        "incident_count": state.incident_count if state else 0,
        "degraded_inputs": (state.degraded_inputs or []) if state else [],
        "computed_at": iso(state.computed_at) if state else None,
        "zone_id": cell.zone_id,
        "colour": colour,
        "opacity": opacity,
        "height_m": round(height_m, 1),
    }
    if not fields:
        return full
    wanted = set(fields) | {"h3"}
    return {k: v for k, v in full.items() if k in wanted}


def _round_or_none(value, ndigits=1):
    return None if value is None else round(value, ndigits)


def state_feature_collection(cells_with_state, fields=None):
    """Full-geometry GeoJSON FeatureCollection (section 7 with geometry=true)."""
    features = []
    for cell, state in cells_with_state:
        boundary = json.loads(cell.boundary_geojson) if cell.boundary_geojson else []
        features.append({
            "type": "Feature",
            "properties": cell_state_properties(cell, state, fields=fields),
            "geometry": {"type": "Polygon", "coordinates": [quantise_ring(boundary)]},
        })
    return {"type": "FeatureCollection", "features": features}


def state_compact_list(cells_with_state, fields=None):
    """The `geometry=false` default (section 7): a flat array, no rings.

    Client rebuilds geometry from `h3` via h3-js. This is the response size
    guard's main lever -- full geometry is ~5-10x the bytes at ~1,000 cells.
    """
    return [cell_state_properties(cell, state, fields=fields) for cell, state in cells_with_state]


# --------------------------------------------------------------------------
# Zones GeoJSON
# --------------------------------------------------------------------------

def zone_feature_collection(zones):
    features = []
    for zone in zones:
        if not zone.boundary_geojson:
            continue
        try:
            geometry = json.loads(zone.boundary_geojson)
        except (ValueError, TypeError):
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "slug": zone.slug,
                "display_name": zone.display_name,
                "zone_type": zone.zone_type,
                "boundary_source": zone.boundary_source,
                "population_estimate": zone.population_estimate,
            },
            "geometry": geometry,
        })
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------
# Incidents / infrastructure GeoJSON
# --------------------------------------------------------------------------

def incidents_feature_collection(reports):
    features = []
    for report in reports:
        if report.get("lat") is None or report.get("lon") is None:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "id": report.get("id"),
                "title": report.get("title"),
                "hazard_type": report.get("hazard_type"),
                "priority": report.get("priority"),
                "confidence": report.get("confidence"),
                "timestamp": report.get("timestamp"),
                "image_url": report.get("image_url"),
            },
            "geometry": {"type": "Point",
                        "coordinates": [round(report["lon"], 5), round(report["lat"], 5)]},
        })
    return {"type": "FeatureCollection", "features": features}


def infrastructure_feature_collection(assets):
    features = []
    for asset in assets:
        features.append({
            "type": "Feature",
            "properties": {
                "asset_type": asset.asset_type,
                "name": asset.name,
                "criticality": asset.criticality,
            },
            "geometry": {"type": "Point",
                        "coordinates": [round(asset.longitude, 5), round(asset.latitude, 5)]},
        })
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------
# Cell drill-down (section 7 /cell route)
# --------------------------------------------------------------------------

def build_explanation(cell, state, reports_nearby, assets):
    """A plain-English sentence for the drawer (section 8.2 mock, A3/A6)."""
    if state is None or state.risk_score is None:
        return "No score computed yet for this cell."

    drivers = []
    if state.hydro_score and state.hydro_score >= 40:
        rain = (state.raw_inputs or {}).get("rain_now_mm_1h")
        if rain:
            drivers.append("%.0f mm/h observed rainfall" % rain)
        else:
            drivers.append("elevated forecast rainfall")
    if state.incident_score and state.incident_score >= 20 and reports_nearby:
        drivers.append("%d approved report(s) nearby" % len(reports_nearby))
    if state.terrain_score and state.terrain_score >= 60:
        dist = (state.raw_inputs or {}).get("dist_to_water_m")
        if dist is not None and dist < 500:
            drivers.append("a low-lying cell %.0f m from the nearest water body" % dist)
        else:
            drivers.append("low-lying terrain")
    if state.env_score and state.env_score >= 60:
        drivers.append("poor air quality or heat stress")

    if not drivers:
        driver_text = "no single dominant driver"
    elif len(drivers) == 1:
        driver_text = drivers[0]
    else:
        driver_text = ", ".join(drivers[:-1]) + " and " + drivers[-1]

    sentence = "%s risk (%d/100) driven by %s." % (
        state.status.capitalize(), round(state.risk_score), driver_text)

    if state.degraded_inputs:
        sentence += " Note: %s data is degraded for this reading." % ", ".join(state.degraded_inputs)

    if assets:
        names = [a.name for a in assets if a.name][:3]
        if names:
            sentence += " Nearby critical assets: %s." % ", ".join(names)

    return sentence


def cell_drilldown_payload(cell, state, assets, reports_nearby, zone=None):
    return {
        "h3": cell.h3_index,
        "zone": zone.slug if zone else None,
        "zone_display_name": zone.display_name if zone else None,
        "center": [cell.center_latitude, cell.center_longitude],
        "area_sqkm": cell.area_sqkm,
        "state": cell_state_properties(cell, state),
        "raw_inputs": (state.raw_inputs or {}) if state else {},
        "assets": [
            {"asset_type": a.asset_type, "name": a.name, "criticality": a.criticality,
             "lat": a.latitude, "lon": a.longitude}
            for a in assets
        ],
        "reports": [
            {"id": r.get("id"), "title": r.get("title"), "hazard_type": r.get("hazard_type"),
             "priority": r.get("priority"), "confidence": r.get("confidence"),
             "timestamp": r.get("timestamp"), "image_url": r.get("image_url")}
            for r in reports_nearby
        ],
        "explanation": build_explanation(cell, state, reports_nearby, assets),
    }


# --------------------------------------------------------------------------
# Summary / compare / timeline (section 7)
# --------------------------------------------------------------------------

def summary_payload(city_slug, cells_with_state, incident_count_24h=0, critical_assets_at_risk=0):
    """KPI block for one city/zone/horizon (section 7 /summary route).

    `cells_with_state` is a list of (TwinCell, TwinCellState) pairs -- the
    twin has no ORM relationship wired between the two (see twin/models.py),
    so every serializer that needs both takes them as explicit pairs rather
    than reaching through a `.cell` backref.
    """
    if not cells_with_state:
        return {
            "city": city_slug, "avg_risk": 0.0, "max_risk": 0.0,
            "cells_by_status": {s: 0 for s in config.STATUS_ORDER},
            "incident_count_24h": incident_count_24h,
            "critical_assets_at_risk": 0, "top_5_cells": [],
        }

    risks = [state.risk_score or 0.0 for _cell, state in cells_with_state]
    by_status = {status: 0 for status in config.STATUS_ORDER}
    for _cell, state in cells_with_state:
        by_status[state.status] = by_status.get(state.status, 0) + 1

    top5 = sorted(cells_with_state, key=lambda pair: pair[1].risk_score or 0.0, reverse=True)[:5]

    return {
        "city": city_slug,
        "avg_risk": round(sum(risks) / len(risks), 1),
        "max_risk": round(max(risks), 1),
        "cells_by_status": by_status,
        "incident_count_24h": incident_count_24h,
        "critical_assets_at_risk": critical_assets_at_risk,
        "top_5_cells": [
            {"h3": cell.h3_index, "risk_score": round(state.risk_score or 0.0, 1),
             "status": state.status}
            for cell, state in top5
        ],
    }


def timeline_payload(city_slug, hourly_rows):
    """City/zone avg-risk-per-hour sparkline data (section 7 /timeline route)."""
    return {
        "city": city_slug,
        "points": [
            {"hour": iso(hour), "avg_risk": round(avg_risk, 1)}
            for hour, avg_risk in hourly_rows
        ],
    }
