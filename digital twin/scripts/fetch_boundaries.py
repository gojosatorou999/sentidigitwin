"""Fetch and commit city clip polygons + zone polygons (section 2.3).

    python -m scripts.fetch_boundaries --city all
    python -m scripts.fetch_boundaries --city hyderabad --clip-only

Two different failure budgets, per the spec review:

- The **clip polygon** (admin_level=8) is mandatory. Phase 1's grid
  generation refuses to run without it (see twin/grid.py). This script exits
  non-zero if it cannot resolve one.
- **Zone polygons** are best-effort. Real OSM coverage for GHMC's 6 named
  zones and BBMP's 8 legacy zones is patchy -- confirmed while building this:
  Bengaluru's OSM data has moved on to 5 new city corporations (the 2024-25
  Greater Bengaluru Authority restructuring the README warned about) and
  Hyderabad's only OSM-tagged "zones" don't carry the GHMC circle names this
  project needs. When a zone can't be resolved, this script writes a
  circular approximation around its configured centroid instead of failing
  (`boundary_source="approximate"`), exactly as section 2.3 specifies.

Idempotent: reruns overwrite the committed GeoJSON, they don't duplicate it.
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twin import config, geo  # noqa: E402
from twin.ingest.overpass import fetch_admin_relations, fetch_relation_geometry  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("fetch_boundaries")

# One-time-script timeouts, deliberately generous -- this is not a request
# path (C1's <=8s budget does not apply here; see twin/ingest/overpass.py).
CLIP_TIMEOUT_S = 60
GEOMETRY_TIMEOUT_S = 90
INTER_QUERY_DELAY_S = 2.0  # be a polite Overpass citizen; avoid the 429 seen in testing


def _write_geojson(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    log.info("wrote %s", path)


def _polygon_to_feature(polygon, properties):
    import shapely.geometry

    return {
        "type": "Feature",
        "properties": properties,
        "geometry": shapely.geometry.mapping(polygon),
    }


#: Sub-division tokens: a relation whose name contains one of these is a
#: fragment of the city (a corporation, zone, circle, or ward within it), not
#: the whole-city polygon we want as a clip -- even if it's the only
#: admin_level match. Deprioritised, never auto-selected over a cleaner name.
_SUBDIVISION_TOKENS = ("corporation", "zone", "circle", "ward", "mandal", "taluk")


def fetch_clip_polygon(session, city_slug):
    """The best whole-city administrative relation for a city, as a polygon.

    Searches admin_level 6-8 in one pass rather than trying the configured
    level first -- confirmed while building this that the *actual* whole-city
    relation sits at different levels per city (Hyderabad's is admin_level=8,
    Bengaluru's is admin_level=7; OSM is not consistent here), so probing
    just the configured level and giving up is the wrong failure mode.
    """
    spec = config.CITY_DEFS[city_slug]
    bbox = spec["bbox"]

    candidates = fetch_admin_relations(
        session, bbox, admin_levels=[6, 7, 8], name_pattern=spec["display_name"],
        timeout_s=CLIP_TIMEOUT_S,
    )
    if not candidates:
        log.error("%s: could not resolve any clip polygon candidate via Overpass", city_slug)
        return None, None

    target = spec["display_name"].strip().lower()

    def rank(el):
        name = (el.get("tags", {}).get("name") or "").strip().lower()
        exact = name == target
        is_fragment = any(tok in name for tok in _SUBDIVISION_TOKENS)
        level_distance = abs(int(el.get("tags", {}).get("admin_level", 99))
                             - spec["clip_admin_level"])
        return (0 if exact else 1, 1 if is_fragment else 0, level_distance)

    candidates.sort(key=rank)
    chosen = candidates[0]
    log.info("%s: selected relation %s (%r, admin_level=%s) from %d candidate(s)",
             city_slug, chosen["id"], chosen.get("tags", {}).get("name"),
             chosen.get("tags", {}).get("admin_level"), len(candidates))

    time.sleep(INTER_QUERY_DELAY_S)
    element = fetch_relation_geometry(session, chosen["id"], timeout_s=GEOMETRY_TIMEOUT_S)
    if element is None:
        log.error("%s: relation %s returned no geometry", city_slug, chosen["id"])
        return None, None

    polygon = geo.assemble_relation_polygon(element)
    if polygon is None:
        log.error("%s: relation %s geometry could not be assembled into a polygon",
                  city_slug, chosen["id"])
        return None, None

    return polygon, chosen.get("tags", {}).get("name")


def fetch_zone_polygons(session, city_slug):
    """Best-effort zone polygons; approximate circle for anything unresolved."""
    spec = config.CITY_DEFS[city_slug]
    bbox = spec["bbox"]
    zone_defs = config.ZONE_DEFS[city_slug]

    candidates = []
    for level in spec["zone_admin_levels"]:
        time.sleep(INTER_QUERY_DELAY_S)
        try:
            candidates.extend(
                fetch_admin_relations(session, bbox, admin_levels=[level], timeout_s=CLIP_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: admin_level=%d probe failed: %s", city_slug, level, exc)

    features = []
    for zone in zone_defs:
        match = _best_name_match(zone["display_name"], candidates)
        polygon, source = None, "approximate"

        if match is not None:
            time.sleep(INTER_QUERY_DELAY_S)
            try:
                element = fetch_relation_geometry(session, match["id"], timeout_s=GEOMETRY_TIMEOUT_S)
                if element is not None:
                    polygon = geo.assemble_relation_polygon(element)
                    if polygon is not None:
                        source = "osm"
            except Exception as exc:  # noqa: BLE001
                log.warning("%s/%s: geometry fetch for matched relation failed: %s",
                            city_slug, zone["slug"], exc)

        if polygon is None:
            lat, lon = zone["center"]
            polygon = geo.circle_polygon(lat, lon, radius_km=3.0)
            log.info("%s/%s: no OSM match, using approximate circle boundary",
                     city_slug, zone["slug"])

        features.append(_polygon_to_feature(polygon, {
            "slug": zone["slug"],
            "display_name": zone["display_name"],
            "boundary_source": source,
        }))

    return {"type": "FeatureCollection", "features": features}


def _best_name_match(display_name, candidates, min_overlap=0.5):
    """A loose token-overlap match; OSM zone names rarely match ours exactly
    (see the module docstring), so this is intentionally forgiving.
    """
    target_tokens = set(display_name.lower().replace(".", "").split())
    best, best_score = None, 0.0
    for el in candidates:
        name = (el.get("tags", {}).get("name") or "")
        tokens = set(name.lower().replace(".", "").split())
        if not tokens:
            continue
        overlap = len(target_tokens & tokens) / max(1, len(target_tokens))
        if overlap > best_score:
            best, best_score = el, overlap
    return best if best_score >= min_overlap else None


def run(cities, clip_only=False):
    session = requests.Session()
    results = {}

    for city_slug in cities:
        log.info("=== %s ===", city_slug)
        polygon, matched_name = fetch_clip_polygon(session, city_slug)
        if polygon is None:
            results[city_slug] = {"clip": False, "zones": False}
            continue

        _write_geojson(
            os.path.join(config.BOUNDARY_DIR, "%s_clip.geojson" % city_slug),
            _polygon_to_feature(polygon, {
                "slug": city_slug,
                "matched_osm_name": matched_name,
                "boundary_source": "osm",
                "admin_level": config.CITY_DEFS[city_slug]["clip_admin_level"],
            }),
        )
        results[city_slug] = {"clip": True}

        if clip_only:
            continue

        time.sleep(INTER_QUERY_DELAY_S)
        zone_collection = fetch_zone_polygons(session, city_slug)
        _write_geojson(
            os.path.join(config.BOUNDARY_DIR, "%s.geojson" % city_slug),
            zone_collection,
        )
        results[city_slug]["zones"] = True

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="all", choices=["all"] + list(config.CITY_ORDER))
    parser.add_argument("--clip-only", action="store_true",
                        help="Skip zone polygons; fetch only the mandatory clip polygon.")
    args = parser.parse_args()

    cities = list(config.CITY_ORDER) if args.city == "all" else [args.city]
    results = run(cities, clip_only=args.clip_only)

    log.info("summary: %s", results)
    if any(not r.get("clip") for r in results.values()):
        log.error("one or more cities have NO clip polygon; grid generation cannot proceed for them")
        sys.exit(1)


if __name__ == "__main__":
    main()
