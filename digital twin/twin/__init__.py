"""Sentinel digital twin -- dual-city urban twin module.

Single entry point, so wiring the twin into the host Sentinel AI app is one
line in ``app.py`` and touches none of its 119 existing routes (C6)::

    from twin import create_twin_blueprint
    create_twin_blueprint(app, db, scheduler)

``scheduler`` is optional; pass the existing APScheduler instance once Phase 3
lands and the twin will register its jobs on it.
"""

import logging

from . import config
from . import models as _models

__all__ = ["create_twin_blueprint", "config", "TWIN_VERSION"]

TWIN_VERSION = "0.1.0-phase0"

log = logging.getLogger("twin")


def create_twin_blueprint(
    app,
    db,
    scheduler=None,
    login_required=None,
    role_required=None,
    seed=True,
    url_prefix=None,
):
    """Define the twin models, register the blueprint, seed city metadata.

    Parameters
    ----------
    app, db
        The host Flask app and its Flask-SQLAlchemy handle. The twin defines
        its models against *this* ``db`` so both share one metadata object and
        one migration history.
    scheduler
        The host's APScheduler instance. Jobs are registered in Phase 3 and
        only when ``TWIN_SCHEDULER_ENABLED=1`` (section 5.4).
    login_required, role_required
        The host app's own decorators. Passed in rather than imported, so the
        twin adopts the app's auth instead of reimplementing it. Omit them and
        the twin falls back to its Flask-Login-based defaults (C4).
    seed
        Upsert the two cities and fourteen zones at registration, so
        ``GET /api/twin/cities`` is never empty (Phase 0 checkpoint).

    Returns the blueprint, or ``None`` when ``TWIN_ENABLED=0``.
    """
    if not config.TWIN_ENABLED:
        log.info("twin disabled via TWIN_ENABLED=0; blueprint not registered")
        return None

    from . import security
    security.configure(login_required=login_required, role_required=role_required)

    # Models must exist before routes import them.
    _models.init_models(db)

    from .routes import twin_bp, twin_pages_bp

    app.extensions.setdefault("twin", {})
    app.extensions["twin"].update({
        "db": db,
        "scheduler": scheduler,
        "version": TWIN_VERSION,
        "config": config,
    })

    if "twin" not in app.blueprints:
        app.register_blueprint(twin_bp, url_prefix=url_prefix)
    if "twin_pages" not in app.blueprints:
        app.register_blueprint(twin_pages_bp)

    if seed:
        _seed_when_ready(app, db)

    if scheduler is not None and config.SCHEDULER_ENABLED:
        _register_jobs(app, db, scheduler)

    log.info("twin %s registered at %s", TWIN_VERSION, url_prefix or "/api/twin")
    return twin_bp


def _seed_when_ready(app, db):
    """Seed metadata, tolerating a database whose migration has not run yet.

    Registration happens at import time, which on a fresh checkout is *before*
    ``flask db upgrade``. A missing table here must not stop the app from
    booting -- otherwise the migration that would create it can never run.
    """
    from sqlalchemy import inspect

    from .seed import seed_metadata

    with app.app_context():
        try:
            tables = set(inspect(db.engine).get_table_names())
        except Exception as exc:                     # pragma: no cover - env specific
            log.warning("twin seed skipped: cannot inspect database (%s)", exc)
            return

        required = {"twin_city", "twin_zone"}
        missing = required - tables
        if missing:
            log.warning(
                "twin seed skipped: missing table(s) %s -- run `flask db upgrade`",
                ", ".join(sorted(missing)),
            )
            return

        try:
            seed_metadata(db)
        except Exception as exc:                     # pragma: no cover - env specific
            db.session.rollback()
            log.exception("twin seed failed: %s", exc)


def _register_jobs(app, db, scheduler):
    """Register ingest/compute jobs on the host scheduler (Phase 3)."""
    try:
        from .jobs import register_jobs
    except ImportError:
        log.debug("twin jobs not implemented yet (Phase 3)")
        return
    register_jobs(app, db, scheduler)
