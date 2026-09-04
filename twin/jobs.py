"""APScheduler job registration (section 5.4, Phase 3).

Registered on the *existing* APScheduler instance passed into
`create_twin_blueprint(app, db, scheduler=...)` -- this module creates no
scheduler of its own (C6). Every job wraps its body in `app.app_context()`
and a broad except, because an unhandled exception inside an APScheduler job
silently kills that job's future runs, which is a worse failure mode than
anything C1 is trying to prevent on the request path.

`TWIN_SCHEDULER_ENABLED` (checked by the caller in twin/__init__.py, not
here) is the guard against double-running these jobs under multi-worker
Gunicorn (section 15).
"""

import logging

from . import config
from . import engine
from . import grid
from . import models as m
from . import stream
from .ingest.overpass import OverpassInfrastructureAdapter
from .ingest.rainviewer import RainViewerAdapter

log = logging.getLogger("twin.jobs")


def register_jobs(app, db, scheduler):
    scheduler.add_job(
        func=lambda: _run_compute(app, db), trigger="interval",
        minutes=config.COMPUTE_INTERVAL_MIN, id="twin_compute_state",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        func=lambda: _run_radar_manifest(app, db), trigger="interval",
        minutes=config.RADAR_INTERVAL_MIN, id="twin_ingest_radar_index",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        func=lambda: _run_infrastructure_refresh(app, db), trigger="interval",
        weeks=1, id="twin_refresh_infrastructure",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    log.info(
        "twin jobs registered: compute every %dmin, radar every %dmin, "
        "infrastructure weekly",
        config.COMPUTE_INTERVAL_MIN, config.RADAR_INTERVAL_MIN,
    )


def _run_compute(app, db):
    """twin_compute_state (section 5.4): the only job that calls the weather/
    air-quality/flood/incident adapters -- their own cadence is enforced by
    each adapter's cache_ttl_s (section 5.4's "15 min", "30 min", "6 h"
    intervals), not by separate scheduler jobs, so there is exactly one code
    path that fans out to them (twin.engine.compute_state) and exactly one
    cache layer deciding whether that fan-out reaches the network this tick.
    """
    with app.app_context():
        for city in db.session.query(m.TwinCity).filter_by(is_active=True).all():
            try:
                summary = engine.compute_state(db, city)
                if summary.get("changed_cells"):
                    stream.publish(
                        "state_update", city=city.slug,
                        changed_cells=summary["changed_cells"],
                        degraded_sources=summary.get("degraded_sources", []),
                    )
            except Exception:  # noqa: BLE001 - a job must never die permanently
                log.exception("twin_compute_state failed for %s", city.slug)


def _run_radar_manifest(app, db):
    """twin_ingest_radar_index, reduced to caching the frame manifest only
    (section 15: RainViewer has no per-cell point API)."""
    with app.app_context():
        adapter = RainViewerAdapter()
        try:
            adapter.run(db, city_id=None)
        except Exception:  # noqa: BLE001
            log.exception("twin_ingest_radar_index failed")


def _run_infrastructure_refresh(app, db):
    """twin_refresh_infrastructure: weekly Overpass pass + terrain re-cache."""
    with app.app_context():
        for city in db.session.query(m.TwinCity).filter_by(is_active=True).all():
            try:
                grid.seed_infrastructure_and_terrain(db, city)
                grid.cache_terrain_scores(db, city)
            except Exception:  # noqa: BLE001
                log.exception("twin_refresh_infrastructure failed for %s", city.slug)
