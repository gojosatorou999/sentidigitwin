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

from . import alerts as twin_alerts
from . import config
from . import engine
from . import grid
from . import live as twin_live
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

    if config.ALERTS_ENABLED:
        scheduler.add_job(
            func=lambda: _run_alert_poll(app, db), trigger="interval",
            minutes=config.ALERT_POLL_MIN, id="twin_ingest_alerts",
            replace_existing=True, max_instances=1, coalesce=True,
        )
    scheduler.add_job(
        func=lambda: _run_station_poll(app, db), trigger="interval",
        minutes=config.STATION_POLL_MIN, id="twin_ingest_stations",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        func=lambda: _run_transit_poll(app, db), trigger="interval",
        minutes=config.TRANSIT_POLL_MIN, id="twin_ingest_transit",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    if config.AGENT_ENABLED:
        scheduler.add_job(
            func=lambda: _run_agent(app, db), trigger="interval",
            minutes=config.AGENT_INTERVAL_MIN, id="twin_agent_triage",
            replace_existing=True, max_instances=1, coalesce=True,
        )

    log.info(
        "twin jobs registered: compute %dmin, radar %dmin, alerts %dmin, "
        "stations %dmin, transit %dmin, agent %s, infrastructure weekly",
        config.COMPUTE_INTERVAL_MIN, config.RADAR_INTERVAL_MIN,
        config.ALERT_POLL_MIN, config.STATION_POLL_MIN, config.TRANSIT_POLL_MIN,
        ("%dmin" % config.AGENT_INTERVAL_MIN) if config.AGENT_ENABLED else "off",
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


def _run_alert_poll(app, db):
    """twin_ingest_alerts: official CAP feeds + global corroborating events.

    New alerts are pushed over SSE immediately rather than waiting for the
    next compute tick: an IMD warning that arrives at 14:42 and appears on an
    operator's screen at 14:47 has spent a third of its three-hour lead time
    sitting in a database.
    """
    with app.app_context():
        for city in db.session.query(m.TwinCity).filter_by(is_active=True).all():
            try:
                result = twin_alerts.ingest_city_alerts(db, city)
                twin_live.refresh_global_events(db, city)
                if result.get("created"):
                    stream.publish("alerts", city=city.slug,
                                   created=result["created"], active=result.get("active"))
            except Exception:  # noqa: BLE001
                log.exception("twin_ingest_alerts failed for %s", city.slug)


def _run_station_poll(app, db):
    """twin_ingest_stations: measured air quality from real instruments."""
    with app.app_context():
        for city in db.session.query(m.TwinCity).filter_by(is_active=True).all():
            try:
                twin_live.refresh_stations(db, city)
            except Exception:  # noqa: BLE001
                log.exception("twin_ingest_stations failed for %s", city.slug)


def _run_transit_poll(app, db):
    """twin_ingest_transit: live vehicle positions, where a feed is configured."""
    with app.app_context():
        for city in db.session.query(m.TwinCity).filter_by(is_active=True).all():
            try:
                result = twin_live.refresh_transit(db, city)
                if result.get("written"):
                    stream.publish("transit", city=city.slug, vehicles=result["written"])
            except Exception:  # noqa: BLE001
                log.exception("twin_ingest_transit failed for %s", city.slug)


def _run_agent(app, db):
    """twin_agent_triage: the LangGraph agent's scheduled pass.

    Imported lazily so that a missing optional dependency (langgraph, or the
    LLM client) costs this one job rather than the whole scheduler -- and so
    the twin still boots on an install that has none of them.
    """
    with app.app_context():
        try:
            from .agent import run_triage
        except Exception:  # noqa: BLE001
            log.warning("twin agent unavailable (dependencies not installed); "
                        "skipping scheduled triage")
            return

        try:
            from .agent import run_forecast
        except Exception:  # noqa: BLE001 - triage still runs without it
            run_forecast = None

        for city in db.session.query(m.TwinCity).filter_by(is_active=True).all():
            try:
                result = run_triage(db, city)
                if result.get("flags_created"):
                    stream.publish("flags", city=city.slug,
                                   created=result["flags_created"],
                                   pending=result.get("pending"))
            except Exception:  # noqa: BLE001
                log.exception("twin_agent_triage failed for %s", city.slug)

            # The forecast pass is run in the same job rather than on its own
            # schedule: both write the same flag table, and two writers on
            # independent timers would race to upsert the same cluster key.
            if run_forecast is None or not config.FORECAST_ENABLED:
                continue
            try:
                projected = run_forecast(db, city)
                if projected.get("flags_created"):
                    stream.publish("flags", city=city.slug,
                                   created=projected["flags_created"],
                                   kind="forecast")
            except Exception:  # noqa: BLE001
                log.exception("twin_agent_forecast failed for %s", city.slug)
