"""Idempotent metadata seeding for cities and zones.

This is the cheap seed: two cities and fourteen zone rows, no network calls.
The expensive seed -- H3 grid, elevation, Overpass infrastructure -- lives in
``scripts/seed_twin.py`` (Phase 1).

Runs at blueprint registration so ``GET /api/twin/cities`` is never empty.
"""

import logging

from . import config
from . import models as m

log = logging.getLogger("twin.seed")


def seed_cities(db, commit=True):
    """Upsert TwinCity rows from CITY_DEFS. Returns (created, updated)."""
    created = updated = 0

    for slug in config.CITY_ORDER:
        spec = config.CITY_DEFS[slug]
        min_lon, min_lat, max_lon, max_lat = spec["bbox"]
        outlet_lat, outlet_lon = spec["basin_outlet"]

        city = db.session.query(m.TwinCity).filter_by(slug=slug).one_or_none()
        if city is None:
            city = m.TwinCity(slug=slug)
            db.session.add(city)
            created += 1
        else:
            updated += 1

        city.display_name = spec["display_name"]
        city.state = spec["state"]
        city.country = spec["country"]
        city.center_latitude = spec["center_latitude"]
        city.center_longitude = spec["center_longitude"]
        city.bbox_min_lon = min_lon
        city.bbox_min_lat = min_lat
        city.bbox_max_lon = max_lon
        city.bbox_max_lat = max_lat
        city.default_zoom = spec["default_zoom"]
        city.default_pitch = spec["default_pitch"]
        city.default_bearing = spec["default_bearing"]
        city.zone_scheme = spec["zone_scheme"]
        city.h3_resolution = config.H3_RESOLUTION
        city.basin_name = spec["basin_name"]
        city.basin_outlet_lat = outlet_lat
        city.basin_outlet_lon = outlet_lon
        city.is_active = True

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return created, updated


def seed_zones(db, commit=True):
    """Upsert TwinZone rows from ZONE_DEFS.

    Boundaries are left NULL here; ``scripts/fetch_boundaries.py`` fills them
    in Phase 1 and sets ``boundary_source``. A zone without a polygon is a
    usable dropdown entry (it has a centroid), it simply cannot be flown to
    by bounds yet.
    """
    created = updated = 0

    for city_slug, zones in config.ZONE_DEFS.items():
        city = db.session.query(m.TwinCity).filter_by(slug=city_slug).one_or_none()
        if city is None:
            log.warning("seed_zones: city %s missing; run seed_cities first", city_slug)
            continue

        for spec in zones:
            zone = (
                db.session.query(m.TwinZone)
                .filter_by(city_id=city.id, slug=spec["slug"])
                .one_or_none()
            )
            if zone is None:
                zone = m.TwinZone(city_id=city.id, slug=spec["slug"])
                db.session.add(zone)
                created += 1
            else:
                updated += 1

            zone.display_name = spec["display_name"]
            zone.zone_type = spec.get("zone_type", "zone")
            zone.center_latitude = spec["center"][0]
            zone.center_longitude = spec["center"][1]
            # Do not clobber a boundary already fetched by Phase 1.
            if zone.boundary_source is None:
                zone.boundary_source = "approximate"

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return created, updated


def seed_metadata(db, commit=True):
    """seed_cities + seed_zones in one transaction."""
    c_created, c_updated = seed_cities(db, commit=False)
    z_created, z_updated = seed_zones(db, commit=False)
    if commit:
        db.session.commit()
    log.info(
        "twin metadata seeded: cities +%d/~%d, zones +%d/~%d",
        c_created, c_updated, z_created, z_updated,
    )
    return {
        "cities_created": c_created,
        "cities_updated": c_updated,
        "zones_created": z_created,
        "zones_updated": z_updated,
    }
