"""H3 cell grid generation and zone assignment (section 3).

Two hard rules from the spec review, enforced here rather than merely
documented:

- **Never generate from the raw bbox.** :func:`generate_cells_for_city`
  refuses to run without a clip polygon on disk. The bbox in
  ``twin/config.py`` is a camera/query extent, roughly 2-3x the municipal
  area; grid-from-bbox blows the cell-count, compute-time, and payload
  budgets simultaneously (section 3 warning).
- **h3-py v4 only.** ``polygon_to_cells`` takes an ``h3.LatLngPoly``
  positionally, ``cell_to_boundary`` returns (lat, lng) and must be swapped
  before it is valid GeoJSON. Both mistakes are exactly the "#1 h3 v4 bug"
  called out in the README.
"""

import json
import logging
import os

import h3
from shapely.geometry import shape

from . import config
from . import geo
from . import models as m
from .ingest.open_meteo import OpenMeteoElevationAdapter
from .ingest.overpass import OverpassInfrastructureAdapter

log = logging.getLogger("twin.grid")


# --------------------------------------------------------------------------
# Boundary loading
# --------------------------------------------------------------------------

def _boundary_path(city_slug, suffix="_clip"):
    return os.path.join(config.BOUNDARY_DIR, "%s%s.geojson" % (city_slug, suffix))


def load_clip_polygon(city_slug):
    """The committed admin_level=8 polygon for a city, or None if not fetched."""
    path = _boundary_path(city_slug, "_clip")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        geojson = json.load(fh)
    try:
        return shape(geojson["geometry"] if "geometry" in geojson else geojson)
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("clip polygon for %s is malformed: %s", city_slug, exc)
        return None


def load_zone_polygons(city_slug):
    """{zone_slug: (shapely Polygon, source)} from the committed zone GeoJSON.

    Missing entirely for a city that has none fetched yet -- callers must
    treat every zone as `boundary_source="approximate"` in that case
    (section 2.3), never raise.
    """
    path = _boundary_path(city_slug, "")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        collection = json.load(fh)

    result = {}
    for feature in collection.get("features", []):
        props = feature.get("properties", {})
        slug = props.get("slug")
        if not slug:
            continue
        try:
            poly = shape(feature["geometry"])
        except (KeyError, ValueError, TypeError):
            continue
        result[slug] = (poly, props.get("boundary_source", "osm"))
    return result


# --------------------------------------------------------------------------
# H3 grid generation (section 3 pseudocode, corrected for h3-py v4)
# --------------------------------------------------------------------------

def _shapely_polygon_to_h3poly(polygon):
    """A shapely Polygon/MultiPolygon exterior -> h3.LatLngPoly (or None).

    Only the largest exterior ring is used for a MultiPolygon input: the
    twin's clip polygons are single-city extents, and a stray sliver
    (an exclave, a digitising artifact) is not worth the complexity of a
    multi-polygon cell union here.
    """
    from shapely.geometry import MultiPolygon

    if isinstance(polygon, MultiPolygon):
        polygon = max(polygon.geoms, key=lambda g: g.area)

    outer = [(lat, lon) for lon, lat in polygon.exterior.coords]
    holes = [[(lat, lon) for lon, lat in ring.coords] for ring in polygon.interiors]
    return h3.LatLngPoly(outer, *holes)


def generate_cells_for_city(db, city, force=False):
    """Idempotent H3 res-8 grid generation for one TwinCity row.

    Returns the number of cells upserted. Raises RuntimeError (caller's
    responsibility to report, not silently swallow -- this is a seed-time
    operation, not a request-path one, so C1's "never raise" does not apply
    here) if no clip polygon has been fetched yet.
    """
    clip_polygon = load_clip_polygon(city.slug)
    if clip_polygon is None:
        raise RuntimeError(
            "no clip polygon for %s at %s -- run scripts/fetch_boundaries.py first "
            "(see the bbox warning in DIGITAL_TWIN_README.md section 3)"
            % (city.slug, _boundary_path(city.slug, "_clip")))

    existing_count = db.session.query(m.TwinCell).filter_by(city_id=city.id).count()
    if existing_count and not force:
        log.info("grid for %s already has %d cells; skipping (force=True to regenerate)",
                  city.slug, existing_count)
        return existing_count

    h3poly = _shapely_polygon_to_h3poly(clip_polygon)
    cells = h3.polygon_to_cells(h3poly, config.H3_RESOLUTION)
    if not cells:
        raise RuntimeError("polygon_to_cells produced zero cells for %s" % city.slug)

    zone_polys = load_zone_polygons(city.slug)
    zone_rows = {z.slug: z for z in db.session.query(m.TwinZone).filter_by(city_id=city.id).all()}
    zone_index_entries = [
        (zone_rows[slug].id, poly)
        for slug, (poly, _source) in zone_polys.items()
        if slug in zone_rows
    ]
    zone_index = geo.ZoneIndex(zone_index_entries)

    existing_by_h3 = {
        c.h3_index: c for c in db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
    }

    upserted = 0
    for cell_index in cells:
        lat, lon = h3.cell_to_latlng(cell_index)
        ring = h3.cell_to_boundary(cell_index)  # ((lat, lng), ...) -- v4 always this order
        # GeoJSON wants (lng, lat): swap here, once, in the one place a v4
        # migration bug would otherwise ship silently (README section 3).
        boundary_coords = [[round(lng, 5), round(lat_, 5)] for lat_, lng in ring]

        cell_row = existing_by_h3.get(cell_index)
        if cell_row is None:
            cell_row = m.TwinCell(h3_index=cell_index, city_id=city.id)
            db.session.add(cell_row)

        cell_row.center_latitude = lat
        cell_row.center_longitude = lon
        cell_row.boundary_geojson = json.dumps(boundary_coords)
        cell_row.area_sqkm = h3.cell_area(cell_index, unit="km^2")
        cell_row.zone_id = zone_index.zone_for(lat, lon)
        upserted += 1

    db.session.commit()
    log.info("grid for %s: %d cells upserted", city.slug, upserted)
    return upserted


# --------------------------------------------------------------------------
# Elevation seed (section 4.1 / Phase 1 checkpoint)
# --------------------------------------------------------------------------

def seed_elevation(db, city, batch_size=100):
    """Fill elevation_m for every cell missing it. Open-Meteo first, then
    Open Topo Data (1 req/s, so this path is deliberately slow); cells that
    exhaust both fallbacks get `elevation_source="unknown"` and a neutral
    terrain contribution rather than blocking the seed (C1's spirit, even
    though this is an offline script).
    """
    cells = (
        db.session.query(m.TwinCell)
        .filter(m.TwinCell.city_id == city.id, m.TwinCell.elevation_m.is_(None))
        .all()
    )
    if not cells:
        return 0

    coords = [(c.center_latitude, c.center_longitude) for c in cells]
    adapter = OpenMeteoElevationAdapter()
    filled = 0

    import time as _time

    last_exc = None
    for attempt in range(3):
        try:
            elevations = adapter.fetch_raw(coords, timeout_s=30)
            for cell_row, value in zip(cells, elevations):
                cell_row.elevation_m = value
                cell_row.elevation_source = "open_meteo"
                filled += 1
            db.session.commit()
            log.info("elevation seed for %s: %d cells via Open-Meteo", city.slug, filled)
            return filled
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # A 429 from Open-Meteo's free tier is typically a short,
            # per-minute cooldown -- worth a real wait before giving up on a
            # fast path and dropping to the 1 req/s Open Topo Data fallback,
            # which is ~15 minutes for a ~900-cell city.
            wait_s = 20 * (attempt + 1)
            log.warning("Open-Meteo elevation attempt %d/3 failed for %s (%s); retrying in %ds",
                       attempt + 1, city.slug, exc, wait_s)
            _time.sleep(wait_s)

    log.warning("Open-Meteo elevation exhausted retries for %s (%s); falling back to Open Topo Data",
               city.slug, last_exc)
    filled += _seed_elevation_opentopodata(db, cells)
    return filled


def _seed_elevation_opentopodata(db, cells, commit_every=20):
    """1 req/s rate-limited fallback (section 4.1). Cells still missing an
    elevation after this are marked `elevation_source="unknown"`.

    Commits every `commit_every` cells rather than once at the end: this
    fallback runs at ~1 cell/second, so a ~900-cell city takes ~15 minutes,
    and a kill or timeout partway through must not discard everything
    already fetched.
    """
    import time as _time

    import requests

    filled = 0
    since_commit = 0
    for cell_row in cells:
        if cell_row.elevation_m is not None:
            continue
        try:
            response = requests.get(
                "https://api.opentopodata.org/v1/srtm30m",
                params={"locations": "%f,%f" % (cell_row.center_latitude, cell_row.center_longitude)},
                timeout=config.HTTP_TIMEOUT_S,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            elevation = results[0].get("elevation") if results else None
            if elevation is not None:
                cell_row.elevation_m = elevation
                cell_row.elevation_source = "opentopodata"
                filled += 1
            else:
                cell_row.elevation_source = "unknown"
        except Exception as exc:  # noqa: BLE001
            log.debug("opentopodata failed for cell %s: %s", cell_row.h3_index, exc)
            cell_row.elevation_source = "unknown"

        since_commit += 1
        if since_commit >= commit_every:
            db.session.commit()
            since_commit = 0
            log.info("opentopodata progress: %d/%d cells filled so far", filled, len(cells))

        _time.sleep(1.05)  # 1 req/s limit

    db.session.commit()
    return filled


# --------------------------------------------------------------------------
# Infrastructure + terrain caching (section 4.3, Phase 1 checkpoint)
# --------------------------------------------------------------------------

def seed_infrastructure_and_terrain(db, city, timeout_s=90):
    """Overpass infra/water/drains -> TwinInfrastructure rows + cached
    per-cell terrain inputs (dist_to_water_m, drain_length_m,
    infra_criticality_cached). Uses the IngestAdapter machinery so a dead
    Overpass writes a proper TwinDataSnapshot and leaves cached fields at
    their neutral defaults instead of raising.
    """
    bbox = (city.bbox_min_lon, city.bbox_min_lat, city.bbox_max_lon, city.bbox_max_lat)
    adapter = OverpassInfrastructureAdapter()
    data, snapshot = adapter.run(db, city_id=city.id, bbox=bbox, timeout_s=timeout_s)

    if not data:
        log.warning("no infrastructure data for %s (snapshot status=%s)",
                    city.slug, snapshot.status if snapshot else "?")
        return {"assets": 0, "water_features": 0, "drain_features": 0}

    cells = db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
    if not cells:
        log.warning("seed_infrastructure_and_terrain: no cells for %s; run generate_cells_for_city first",
                    city.slug)
        return {"assets": 0, "water_features": 0, "drain_features": 0}

    _write_infrastructure_rows(db, city, cells, data.get("assets", []))
    _compute_water_distance(cells, data.get("water_features", []))
    _compute_drain_length(cells, data.get("drain_features", []))
    db.session.commit()

    return {
        "assets": len(data.get("assets", [])),
        "water_features": len(data.get("water_features", [])),
        "drain_features": len(data.get("drain_features", [])),
    }


def _write_infrastructure_rows(db, city, cells, assets):
    """Upsert TwinInfrastructure by osm_id and roll criticality up to cells."""
    by_cell_id = {}
    existing = {
        row.osm_id: row
        for row in db.session.query(m.TwinInfrastructure).filter_by(city_id=city.id).all()
        if row.osm_id
    }

    for asset in assets:
        cell_index = h3.latlng_to_cell(asset["lat"], asset["lon"], config.H3_RESOLUTION)
        row = existing.get(asset["osm_id"])
        if row is None:
            row = m.TwinInfrastructure(city_id=city.id, osm_id=asset["osm_id"])
            db.session.add(row)
        row.asset_type = asset["asset_type"]
        row.name = asset.get("name")
        row.criticality = asset["criticality"]
        row.latitude = asset["lat"]
        row.longitude = asset["lon"]
        row.tags = asset.get("tags") or {}
        row.source = "overpass"
        row.fetched_at = m.utcnow()

        cell = next((c for c in cells if c.h3_index == cell_index), None)
        if cell is not None:
            row.cell_id = cell.id
            by_cell_id.setdefault(cell.id, 0.0)
            by_cell_id[cell.id] += asset["criticality"]

    for cell in cells:
        cell.infra_criticality_cached = by_cell_id.get(cell.id, 0.0)


def _compute_water_distance(cells, water_features):
    """dist_to_water_m for every cell, via nearest-vertex approximation.

    A true nearest-point-on-line-string distance is one shapely call per
    (cell, feature) pair -- fine at ~1,000 cells x a few hundred water
    features, so this uses the exact shapely distance rather than a coarser
    approximation.
    """
    from shapely.geometry import LineString, Point

    if not water_features:
        for cell in cells:
            cell.dist_to_water_m = None
        return

    lines = []
    for feature in water_features:
        coords = feature.get("geometry") or []
        if len(coords) < 2:
            continue
        lines.append(LineString([(lon, lat) for lat, lon in coords]))

    if not lines:
        for cell in cells:
            cell.dist_to_water_m = None
        return

    for cell in cells:
        pt = Point(cell.center_longitude, cell.center_latitude)
        best_m = min(
            geo.haversine_m(
                cell.center_latitude, cell.center_longitude,
                *_nearest_latlon(pt, line),
            )
            for line in lines
        )
        cell.dist_to_water_m = best_m


def _nearest_latlon(point, line):
    from shapely.ops import nearest_points
    _, nearest_on_line = nearest_points(point, line)
    return nearest_on_line.y, nearest_on_line.x  # (lat, lon)


def _compute_drain_length(cells, drain_features):
    """drain_length_m per cell: metres of drain/canal/stream geometry whose
    midpoint falls in that cell (a cheap proxy for a true polygon-clip
    length, adequate at H3 res 8 where drains rarely cross more than one or
    two cell boundaries within a single OSM way segment).
    """
    for cell in cells:
        cell.drain_length_m = 0.0

    by_h3 = {c.h3_index: c for c in cells}

    for feature in drain_features:
        coords = feature.get("geometry") or []
        for i in range(len(coords) - 1):
            (lat1, lon1), (lat2, lon2) = coords[i], coords[i + 1]
            seg_len = geo.haversine_m(lat1, lon1, lat2, lon2)
            mid_lat, mid_lon = (lat1 + lat2) / 2, (lon1 + lon2) / 2
            cell_index = h3.latlng_to_cell(mid_lat, mid_lon, config.H3_RESOLUTION)
            cell = by_h3.get(cell_index)
            if cell is not None:
                cell.drain_length_m = (cell.drain_length_m or 0.0) + seg_len


# --------------------------------------------------------------------------
# Terrain score caching (depends on elevation + water/drain, so it runs last)
# --------------------------------------------------------------------------

def cache_terrain_scores(db, city):
    """Compute and store TwinCell.terrain_score_cached for every cell.

    Must run after seed_elevation and seed_infrastructure_and_terrain, since
    the low-lying term is a percentile rank over the *whole city's* elevation
    distribution (section 5.1) -- it cannot be computed cell-by-cell in
    isolation.
    """
    from . import scoring

    cells = db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
    population = [c.elevation_m for c in cells if c.elevation_m is not None]

    updated = 0
    for cell in cells:
        cell.terrain_score_cached = scoring.terrain_score(
            cell.elevation_m, population, cell.dist_to_water_m, cell.drain_length_m)
        updated += 1

    db.session.commit()
    return updated
