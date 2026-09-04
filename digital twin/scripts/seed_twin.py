"""Phase 1 seed orchestrator (section 12 checkpoint).

    python -m scripts.seed_twin --city all
    python -m scripts.seed_twin --city hyderabad --skip-infrastructure

Idempotent: reruns skip grid generation for a city that already has cells
(pass --force-grid to regenerate) and only fetch elevation for cells that are
still missing it. Expected total runtime is dominated by Open Topo Data's
1 req/s fallback path, which only activates if Open-Meteo's batch elevation
call fails -- normally this finishes in well under a minute per city.

Prerequisite: `python -m scripts.fetch_boundaries --city all` must have
already committed a clip polygon for each city (section 2.3); this script
raises immediately, per city, if one is missing rather than silently
grid-from-bbox (see the section 3 warning this project was built around).
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("seed_twin")


def _load_app():
    from dev_app import create_app, db
    return create_app(), db


def seed_one_city(db, city_slug, force_grid=False, skip_infrastructure=False):
    from twin import config, grid
    from twin import models as m

    city = db.session.query(m.TwinCity).filter_by(slug=city_slug).one_or_none()
    if city is None:
        raise RuntimeError("city %r not seeded -- run twin.seed.seed_cities first" % city_slug)

    t0 = time.monotonic()
    log.info("[%s] generating H3 grid...", city_slug)
    cell_count = grid.generate_cells_for_city(db, city, force=force_grid)
    log.info("[%s] grid: %d cells (%.1fs)", city_slug, cell_count, time.monotonic() - t0)

    t1 = time.monotonic()
    log.info("[%s] seeding elevation...", city_slug)
    filled = grid.seed_elevation(db, city)
    log.info("[%s] elevation: %d cells filled (%.1fs)", city_slug, filled, time.monotonic() - t1)

    infra_summary = {"assets": 0, "water_features": 0, "drain_features": 0}
    if not skip_infrastructure:
        t2 = time.monotonic()
        log.info("[%s] fetching Overpass infrastructure + terrain features...", city_slug)
        infra_summary = grid.seed_infrastructure_and_terrain(db, city)
        log.info("[%s] infrastructure: %s (%.1fs)", city_slug, infra_summary, time.monotonic() - t2)

    t3 = time.monotonic()
    log.info("[%s] caching terrain scores...", city_slug)
    terrain_count = grid.cache_terrain_scores(db, city)
    log.info("[%s] terrain scores: %d cells (%.1fs)", city_slug, terrain_count, time.monotonic() - t3)

    elapsed = time.monotonic() - t0
    non_null_elevation = sum(
        1 for c in db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
        if c.elevation_m is not None
    )
    non_null_terrain = sum(
        1 for c in db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
        if c.terrain_score_cached is not None
    )

    return {
        "city": city_slug,
        "cells": cell_count,
        "elevation_filled": filled,
        "non_null_elevation": non_null_elevation,
        "non_null_terrain": non_null_terrain,
        "infrastructure": infra_summary,
        "elapsed_s": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="all")
    parser.add_argument("--force-grid", action="store_true",
                        help="Regenerate the H3 grid even if cells already exist.")
    parser.add_argument("--skip-infrastructure", action="store_true",
                        help="Skip the Overpass infrastructure/terrain pass (useful for a quick grid+elevation-only run).")
    args = parser.parse_args()

    from twin import config

    cities = list(config.CITY_ORDER) if args.city == "all" else [args.city]

    app, db = _load_app()
    with app.app_context():
        results = []
        overall_start = time.monotonic()
        for city_slug in cities:
            try:
                results.append(seed_one_city(
                    db, city_slug, force_grid=args.force_grid,
                    skip_infrastructure=args.skip_infrastructure,
                ))
            except Exception:
                log.exception("[%s] seed failed", city_slug)
                results.append({"city": city_slug, "error": True})

        total_elapsed = time.monotonic() - overall_start
        log.info("=== seed_twin summary (%.1fs total) ===", total_elapsed)
        for r in results:
            log.info("  %s", r)

        if any(r.get("error") for r in results):
            sys.exit(1)


if __name__ == "__main__":
    main()
