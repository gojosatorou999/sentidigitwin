"""Shared spatial math: no DB-specific SQL, pure Python (C2).

Everything here operates on plain (lat, lon) tuples or GeoJSON-shaped dicts so
it has no opinion about SQLAlchemy, H3, or the request lifecycle.
"""

import math

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres, or None if any coordinate is missing.

    The null-tolerant, rounded form the ingest adapters want. Four of them
    had carried a byte-identical private copy of this, and a fifth had the
    same code without the rounding -- which is how "why is this camera 12 m
    from that one in one panel and 12.0392 in another" starts.
    """
    if None in (lat1, lon1, lat2, lon2):
        return None
    return round(haversine_m(lat1, lon1, lat2, lon2), 1)


def idw_interpolate(target_lat, target_lon, samples, power=2, min_distance_m=25.0):
    """Inverse-distance-weighted interpolation.

    `samples` is an iterable of (lat, lon, value). A sample within
    `min_distance_m` of the target is returned directly (avoids a divide by
    ~0 blowing up the weights). Returns None if `samples` is empty or every
    value in it is None.
    """
    usable = [(lat, lon, v) for lat, lon, v in samples if v is not None]
    if not usable:
        return None

    weighted_sum = 0.0
    weight_total = 0.0
    for lat, lon, value in usable:
        d = haversine_m(target_lat, target_lon, lat, lon)
        if d <= min_distance_m:
            return value
        w = 1.0 / (d ** power)
        weighted_sum += w * value
        weight_total += w

    if weight_total == 0.0:
        return None
    return weighted_sum / weight_total


def percentile_rank(value, population):
    """Fraction of `population` <= value, in [0, 1]. Empty population -> 0.5."""
    values = [v for v in population if v is not None]
    if not values or value is None:
        return 0.5
    values = sorted(values)
    n = len(values)
    below = sum(1 for v in values if v <= value)
    return below / n if n else 0.5


def build_lattice(bbox, spacing_km=5.0):
    """A regular (lat, lon) point lattice covering a bbox, for weather sampling.

    `bbox` is (min_lon, min_lat, max_lon, max_lat). Never call the weather API
    per H3 cell (section 5.4/15) -- this lattice is the fan-out unit instead.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    center_lat = (min_lat + max_lat) / 2
    lat_step_deg = spacing_km / 111.0
    lon_step_deg = spacing_km / (111.0 * max(0.1, math.cos(math.radians(center_lat))))

    points = []
    lat = min_lat
    while lat <= max_lat + 1e-9:
        lon = min_lon
        while lon <= max_lon + 1e-9:
            points.append((round(lat, 5), round(lon, 5)))
            lon += lon_step_deg
        lat += lat_step_deg
    return points


def nearest_point_index(tree, points, lat, lon):
    """Index into `points` of the nearest entry to (lat, lon), via a KD-ish
    linear scan (fine at lattice sizes of ~100 points; avoids a heavier dep).
    `tree` is unused, kept for a future spatial-index swap-in without
    changing call sites.
    """
    best_i, best_d = 0, float("inf")
    for i, (plat, plon) in enumerate(points):
        d = haversine_m(lat, lon, plat, plon)
        if d < best_d:
            best_d, best_i = d, i
    return best_i


class ZoneIndex:
    """Point-in-polygon zone assignment via an STRtree (shapely, no PostGIS)."""

    def __init__(self, zone_polygons):
        """`zone_polygons` is a list of (zone_id, shapely Polygon)."""
        self._by_id = {}
        geoms = []
        for zone_id, poly in zone_polygons:
            if poly is None or poly.is_empty:
                continue
            self._by_id[id(poly)] = zone_id
            geoms.append(poly)
        self._tree = STRtree(geoms) if geoms else None

    def zone_for(self, lat, lon):
        if self._tree is None:
            return None
        pt = Point(lon, lat)
        for idx in self._tree.query(pt):
            geom = self._tree.geometries[idx] if hasattr(self._tree, "geometries") else idx
            if geom.contains(pt):
                return self._by_id.get(id(geom))
        return None


def polygon_from_geojson(geojson_geometry):
    """A shapely geometry from a GeoJSON geometry dict, or None."""
    if not geojson_geometry:
        return None
    try:
        return shape(geojson_geometry)
    except (ValueError, KeyError, TypeError):
        return None


def circle_polygon(lat, lon, radius_km, n_points=32):
    """An approximate circular polygon around (lat, lon), for zones without a
    fetchable boundary (section 2.3 fallback: convex hull of constituent
    cells is the ideal; a plain circle is the pragmatic version of the same
    idea and is what callers use when they only have a centroid + radius).
    """
    lat_step = radius_km / 111.0
    lon_step = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    coords = []
    for i in range(n_points + 1):
        theta = 2 * math.pi * i / n_points
        coords.append((lon + lon_step * math.cos(theta), lat + lat_step * math.sin(theta)))
    from shapely.geometry import Polygon
    return Polygon(coords)


# --------------------------------------------------------------------------
# OSM relation -> polygon assembly (used by scripts/fetch_boundaries.py)
# --------------------------------------------------------------------------

def _chain_ways_into_rings(way_coord_lists, tolerance=1e-7):
    """Greedily join way segments sharing endpoints into closed rings.

    `way_coord_lists` is a list of [(lon, lat), ...] segments (as Overpass
    hands back per-way `out geom` geometry -- individual ways, not yet
    joined). Administrative boundary relations routinely split one ring
    across several ways, so this is required, not optional. Returns a list
    of closed rings; any segment that cannot be closed is dropped (a
    same-effect fallback to the approximate boundary handles the rest).
    """
    remaining = [list(seg) for seg in way_coord_lists if len(seg) >= 2]
    rings = []

    def close_enough(a, b):
        return abs(a[0] - b[0]) < tolerance and abs(a[1] - b[1]) < tolerance

    while remaining:
        ring = remaining.pop(0)
        progressed = True
        while progressed and not close_enough(ring[0], ring[-1]):
            progressed = False
            for i, seg in enumerate(remaining):
                if close_enough(ring[-1], seg[0]):
                    ring = ring + seg[1:]
                elif close_enough(ring[-1], seg[-1]):
                    ring = ring + list(reversed(seg))[1:]
                elif close_enough(ring[0], seg[-1]):
                    ring = seg[:-1] + ring
                elif close_enough(ring[0], seg[0]):
                    ring = list(reversed(seg))[:-1] + ring
                else:
                    continue
                remaining.pop(i)
                progressed = True
                break
        if close_enough(ring[0], ring[-1]) and len(ring) >= 4:
            rings.append(ring)
        # An unclosed ring is dropped silently; the caller treats a relation
        # that yields zero usable rings as unresolved and falls back.
    return rings


def assemble_relation_polygon(relation_element):
    """A shapely (Multi)Polygon from an Overpass `out geom` relation element.

    Returns None if no closed outer ring could be assembled -- callers must
    treat that exactly like a missing boundary (section 2.3): fall back to
    the approximate path, badge it honestly, never raise.
    """
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.validation import make_valid

    outer_segments, inner_segments = [], []
    for member in relation_element.get("members", []):
        if member.get("type") != "way":
            continue
        geometry = member.get("geometry")
        if not geometry:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geometry if "lat" in pt and "lon" in pt]
        if len(coords) < 2:
            continue
        if member.get("role") == "inner":
            inner_segments.append(coords)
        else:
            outer_segments.append(coords)

    outer_rings = _chain_ways_into_rings(outer_segments)
    inner_rings = _chain_ways_into_rings(inner_segments)
    if not outer_rings:
        return None

    outer_polys = []
    for ring in outer_rings:
        try:
            poly = Polygon(ring)
        except (ValueError, TypeError):
            continue
        if poly.is_valid and poly.area > 0:
            outer_polys.append(poly)

    if not outer_polys:
        return None

    holes_by_outer = {i: [] for i in range(len(outer_polys))}
    for ring in inner_rings:
        try:
            hole = Polygon(ring)
        except (ValueError, TypeError):
            continue
        if not hole.is_valid or hole.area == 0:
            continue
        for i, outer in enumerate(outer_polys):
            if outer.contains(hole.representative_point()):
                holes_by_outer[i].append(ring)
                break

    finished = []
    for i, outer in enumerate(outer_polys):
        holes = holes_by_outer[i]
        try:
            finished.append(Polygon(outer.exterior.coords, holes) if holes else outer)
        except (ValueError, TypeError):
            finished.append(outer)

    result = finished[0] if len(finished) == 1 else MultiPolygon(finished)
    if not result.is_valid:
        result = make_valid(result)
    return result if not result.is_empty else None
