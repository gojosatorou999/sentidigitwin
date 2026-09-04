"""Overpass API adapters: infrastructure, water/drains, and admin boundaries.

Overpass is the flakiest dependency in this whole module -- confirmed while
building this: the primary instance alone has thrown 406 (missing headers),
504 (backend timeout on a real query), and 429 (rate limit) inside a single
session. None of that is a network outage; it is normal load on a free public
service. So every query here goes through :func:`run_overpass_query`, which
tries a short list of mirrors with generous timeouts and a real User-Agent,
and every *caller* still goes through :class:`~twin.ingest.base.IngestAdapter`
so a bad day for Overpass degrades one sub-score, never a 500 (C1).

Two different call sites use this module:

- ``OverpassInfrastructureAdapter`` -- weekly, per-city, request-path shaped
  (timeout budget applies).
- ``scripts/fetch_boundaries.py`` -- one-time, interactive, patient (pass a
  larger timeout explicitly; there is no request path to block).
"""

import logging
import time

from .. import config
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.overpass")

#: Confirmed while building this: the kumi and openstreetmap.ru mirrors are
#: unreachable (connection timeout, not an HTTP error) from at least some
#: networks, and burn the full timeout each before falling through -- worse
#: than just retrying the primary, which does intermittently 406/504/429 but
#: also intermittently works. So the default list is the primary only, tried
#: `retries_per_mirror` times; pass `mirrors=` explicitly to add others back
#: for a network where they *are* reachable.
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
)

_HEADERS = {
    "User-Agent": "sentinel-twin/0.1 (+digital-twin-module; research use)",
    "Accept": "*/*",
}


def run_overpass_query(session, query, timeout_s, mirrors=OVERPASS_MIRRORS,
                        retries_per_mirror=3, backoff_s=4.0):
    """POST `query` to Overpass, trying each mirror, raising the last error."""
    last_exc = None
    for mirror in mirrors:
        for attempt in range(retries_per_mirror + 1):
            try:
                response = session.post(
                    mirror, data={"data": query}, headers=_HEADERS, timeout=timeout_s)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.debug("overpass mirror %s attempt %d failed: %s", mirror, attempt, exc)
                if attempt < retries_per_mirror:
                    time.sleep(backoff_s)
    raise last_exc or RuntimeError("no overpass mirrors configured")


# --------------------------------------------------------------------------
# Infrastructure + terrain features (section 4.3)
# --------------------------------------------------------------------------

def build_infra_query(bbox, asset_types, timeout_s=60):
    """Overpass QL for point-like criticality assets in a bbox.

    `bbox` is (min_lon, min_lat, max_lon, max_lat); Overpass wants
    (south, west, north, east).
    """
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    bbox_str = "%f,%f,%f,%f" % (south, west, north, east)
    clauses = []
    for asset_type in asset_types:
        for filt in config.OVERPASS_FILTERS.get(asset_type, ()):
            clauses.append("%s(%s);" % (filt, bbox_str))
    body = "\n  ".join(clauses)
    return "[out:json][timeout:%d];\n(\n  %s\n);\nout center tags;" % (int(timeout_s), body)


def build_terrain_query(bbox, timeout_s=90):
    """Overpass QL for water bodies + drains, with full line/way geometry."""
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    bbox_str = "%f,%f,%f,%f" % (south, west, north, east)
    clauses = []
    for asset_type in config.TERRAIN_ASSET_TYPES:
        for filt in config.OVERPASS_FILTERS.get(asset_type, ()):
            clauses.append("%s(%s);" % (filt, bbox_str))
    body = "\n  ".join(clauses)
    return "[out:json][timeout:%d];\n(\n  %s\n);\nout geom;" % (int(timeout_s), body)


class OverpassInfrastructureAdapter(IngestAdapter):
    """Weekly refresh of criticality assets + terrain features (section 5.4).

    `fetch_raw(bbox=...)` returns
    ``{"assets": [...], "water_features": [...], "drain_features": [...]}``.
    Point assets carry (asset_type, name, lat, lon, osm_id, tags).
    Water/drain features carry (osm_id, tags, geometry: [[lat, lon], ...]).
    """

    source_key = "overpass"
    cache_ttl_s = 7 * 24 * 60 * 60  # weekly cadence

    def fetch_raw(self, bbox, timeout_s=None, **_):
        timeout_s = timeout_s or max(self.timeout_s, 60)
        asset_types = list(config.ASSET_CRITICALITY)

        infra_query = build_infra_query(bbox, asset_types, timeout_s=timeout_s - 5)
        infra_payload = run_overpass_query(self.session, infra_query, timeout_s)
        assets = [
            _element_to_asset(el) for el in infra_payload.get("elements", [])
        ]
        assets = [a for a in assets if a is not None]

        terrain_query = build_terrain_query(bbox, timeout_s=timeout_s - 5)
        terrain_payload = run_overpass_query(self.session, terrain_query, timeout_s)

        water_features, drain_features = [], []
        for el in terrain_payload.get("elements", []):
            geom = _way_geometry(el)
            if geom is None:
                continue
            entry = {"osm_id": "%s/%s" % (el.get("type"), el.get("id")),
                     "tags": el.get("tags", {}), "geometry": geom}
            tags = el.get("tags", {})
            if tags.get("waterway") in ("drain", "canal", "stream"):
                drain_features.append(entry)
            else:
                water_features.append(entry)

        return {
            "assets": assets,
            "water_features": water_features,
            "drain_features": drain_features,
        }

    def record_count(self, data):
        if not data:
            return 0
        return len(data.get("assets", [])) + len(data.get("water_features", [])) \
            + len(data.get("drain_features", []))


# --------------------------------------------------------------------------
# Water bodies and drains, as a map layer
#
# OverpassInfrastructureAdapter already pulls this geometry on its weekly
# run, but only ever to derive one scalar per cell (TwinCell.drain_length_m)
# -- the shapes themselves were computed and thrown away, which left the
# console's "Water & drains" toggle switching a layer that no endpoint could
# ever populate. For a twin whose dominant hazard is flooding, the lakes and
# storm drains are the single most useful context layer there is, so this
# adapter serves the same query straight to the map.
#
# It is a separate class rather than a flag on the infrastructure adapter for
# one concrete reason: the cache key. The base implementation hashes every
# kwarg, one of which is the Flask-SQLAlchemy handle whose repr embeds a
# memory address, so a shared key would change on every process restart. This
# one keys on the bbox alone and therefore actually survives a week.
# --------------------------------------------------------------------------

#: Waterway values kept as line features, mapped to the class the map styles
#: them by. Anything else with a geometry is treated as a water body.
WATERWAY_KINDS = {"drain": "drain", "canal": "canal", "stream": "stream",
                  "river": "river", "ditch": "drain"}

#: Coordinate precision in the served payload. 5 dp is ~1.1 m at this
#: latitude -- far finer than a drain centreline is surveyed to, and it
#: roughly halves the payload against Overpass's full 7 dp.
_COORD_DP = 5


class OverpassWaterAdapter(IngestAdapter):
    """Water bodies + drains in a bbox, with geometry, for the map layer."""

    source_key = "overpass_water"
    cache_ttl_s = 7 * 24 * 60 * 60  # weekly, like the infrastructure refresh
    max_retries = 0                 # run_overpass_query already retries

    def fetch_raw(self, bbox, timeout_s=None, **_):
        timeout_s = timeout_s or max(self.timeout_s, 60)
        query = build_terrain_query(bbox, timeout_s=timeout_s - 5)
        payload = run_overpass_query(self.session, query, timeout_s)

        features = []
        for element in payload.get("elements", []):
            feature = _element_to_water_feature(element)
            if feature is not None:
                features.append(feature)

        return {
            "type": "FeatureCollection",
            "features": features,
            "attribution": "(c) OpenStreetMap contributors (ODbL)",
        }

    def neutral_value(self, **_):
        return {"type": "FeatureCollection", "features": [],
                "attribution": "(c) OpenStreetMap contributors (ODbL)",
                "unavailable": True}

    def record_count(self, data):
        return len((data or {}).get("features") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib

        bbox = kwargs.get("bbox") or ()
        raw = "overpass_water:" + ",".join("%.4f" % float(v) for v in bbox)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _element_to_water_feature(element):
    """One Overpass `out geom` way as a GeoJSON feature, or None.

    Only tags the map actually styles or labels by are carried through: the
    full tag dict on ~950 features was most of a 2 MB payload for data the
    layer never reads.
    """
    geometry = _way_geometry(element)
    if not geometry or len(geometry) < 2:
        return None

    tags = element.get("tags") or {}
    waterway = tags.get("waterway")
    kind = WATERWAY_KINDS.get(waterway) if waterway else "water"
    if kind is None:
        kind = "drain" if waterway else "water"

    # [lat, lon] from _way_geometry; GeoJSON wants [lon, lat].
    ring = [[round(lon, _COORD_DP), round(lat, _COORD_DP)] for lat, lon in geometry]

    # A closed way tagged as a water body is a polygon (a lake); a closed way
    # tagged as a waterway is still a line (a moat, a ring canal), so the
    # geometry type follows the tag, not just the coordinates.
    closed = ring[0] == ring[-1]
    if kind == "water" and closed and len(ring) >= 4:
        geojson_geometry = {"type": "Polygon", "coordinates": [ring]}
    else:
        geojson_geometry = {"type": "LineString", "coordinates": ring}

    properties = {"kind": kind, "osm_id": "%s/%s" % (element.get("type"), element.get("id"))}
    if tags.get("name"):
        properties["name"] = tags["name"]
    return {"type": "Feature", "geometry": geojson_geometry, "properties": properties}


def _element_to_asset(el):
    tags = el.get("tags", {}) or {}
    asset_type = _classify_asset(tags)
    if asset_type is None:
        return None
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None
    return {
        "asset_type": asset_type,
        "name": tags.get("name"),
        "lat": lat,
        "lon": lon,
        "osm_id": "%s/%s" % (el.get("type"), el.get("id")),
        "tags": tags,
        "criticality": config.ASSET_CRITICALITY.get(asset_type, 0.0),
    }


def _classify_asset(tags):
    amenity = tags.get("amenity")
    if amenity == "hospital":
        return "hospital"
    if amenity == "fire_station":
        return "fire_station"
    if amenity == "police":
        return "police"
    if tags.get("power") == "substation":
        return "power_substation"
    if tags.get("man_made") in ("water_works", "water_tower"):
        return "water_works"
    if amenity in ("school", "college"):
        return "school"
    if tags.get("railway") == "station" or amenity == "bus_station" \
            or tags.get("aeroway") == "aerodrome":
        return "transport_hub"
    if amenity in ("shelter", "community_centre"):
        return "shelter"
    return None


def _way_geometry(el):
    """[[lat, lon], ...] from an Overpass `out geom` way element, or None."""
    geometry = el.get("geometry")
    if not geometry:
        return None
    return [[pt["lat"], pt["lon"]] for pt in geometry if "lat" in pt and "lon" in pt]


# --------------------------------------------------------------------------
# Admin boundaries (used by scripts/fetch_boundaries.py, not by a job)
# --------------------------------------------------------------------------

def fetch_admin_relations(session, bbox, admin_levels, name_pattern=None, timeout_s=45):
    """Relations tagged boundary=administrative in a bbox, with full tags.

    Returns raw Overpass elements (no geometry) -- callers fetch geometry for
    the specific relation IDs they decide to keep, via
    :func:`fetch_relation_geometry`, because pulling full multipolygon
    geometry for every candidate relation is expensive and most candidates
    get discarded.
    """
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    bbox_str = "%f,%f,%f,%f" % (south, west, north, east)
    level_re = "|".join(str(lvl) for lvl in admin_levels)
    name_clause = ('["name"~"%s",i]' % name_pattern) if name_pattern else ""
    query = (
        '[out:json][timeout:%d];\n'
        'relation["boundary"="administrative"]["admin_level"~"^(%s)$"]%s(%s);\n'
        'out tags;'
    ) % (int(timeout_s), level_re, name_clause, bbox_str)
    payload = run_overpass_query(session, query, timeout_s)
    return payload.get("elements", [])


def fetch_relation_geometry(session, relation_id, timeout_s=90):
    """Full member-way geometry for one relation, for multipolygon assembly."""
    query = (
        '[out:json][timeout:%d];\n'
        'relation(%d);\n'
        'out geom;'
    ) % (int(timeout_s), relation_id)
    payload = run_overpass_query(session, query, timeout_s)
    for el in payload.get("elements", []):
        if el.get("type") == "relation" and el.get("id") == relation_id:
            return el
    return None
