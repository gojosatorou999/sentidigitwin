"""Approved Sentinel AI reports -> the twin's incident layer (section 4.1).

This is the module's one deliberate coupling to the host app, and it is kept
as loose as C6 allows: the twin never imports the host's ``Report`` model.
The host registers it once, at startup, via :func:`register_report_model`,
telling the twin which attribute names carry which meaning. Everything else
here -- the ingest adapter, the SSE approval hook -- works against that
registration, so the twin's own tests can register a fake ``Report`` and
never need the real Sentinel AI codebase.

Default attribute names match the shape implied by the README
(``Report.verification_status == 'approved'``); override any of them for a
schema that differs.
"""

import logging
from datetime import timedelta

from .. import config as twin_config
from .. import models as m
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.internal_reports")

_registration = {}


def register_report_model(
    report_model,
    latitude_attr="latitude",
    longitude_attr="longitude",
    status_attr="verification_status",
    approved_value="approved",
    priority_attr="priority",
    confidence_attr="confidence_score",
    timestamp_attr="created_at",
    id_attr="id",
    hazard_type_attr="hazard_type",
    title_attr="title",
    image_url_attr="image_url",
):
    """Tell the twin which model and attributes carry approved reports.

    Call this once from the host app, alongside ``create_twin_blueprint``::

        from twin.ingest.internal_reports import register_report_model
        register_report_model(Report)  # defaults match the README's schema

    Idempotent -- a later call replaces the registration.
    """
    _registration.clear()
    _registration.update({
        "model": report_model,
        "latitude_attr": latitude_attr,
        "longitude_attr": longitude_attr,
        "status_attr": status_attr,
        "approved_value": approved_value,
        "priority_attr": priority_attr,
        "confidence_attr": confidence_attr,
        "timestamp_attr": timestamp_attr,
        "id_attr": id_attr,
        "hazard_type_attr": hazard_type_attr,
        "title_attr": title_attr,
        "image_url_attr": image_url_attr,
    })
    log.info("twin: internal Report model registered (%s)", report_model.__name__)


def is_registered():
    return "model" in _registration


def _get(obj, key, default=None):
    attr = _registration.get(key)
    if attr is None:
        return default
    return getattr(obj, attr, default)


def report_to_dict(report):
    """A plain dict view of one report, in the shape scoring.py expects."""
    ts = _get(report, "timestamp_attr")
    return {
        "id": _get(report, "id_attr"),
        "lat": _get(report, "latitude_attr"),
        "lon": _get(report, "longitude_attr"),
        "priority": (_get(report, "priority_attr") or "low"),
        "confidence": _get(report, "confidence_attr"),
        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else ts,
        "hazard_type": _get(report, "hazard_type_attr"),
        "title": _get(report, "title_attr"),
        "image_url": _get(report, "image_url_attr"),
    }


class InternalReportsAdapter(IngestAdapter):
    """Approved reports from the last `since_hours`, as plain dicts.

    This is an in-process DB read, not an HTTP call, but it goes through the
    same IngestAdapter machinery so a broken registration or a DB error
    degrades exactly like a dead external API (C1) and is equally visible on
    the health pill (C7), instead of being a special case.
    """

    source_key = "internal_reports"
    cache_ttl_s = 2 * 60

    def fetch_raw(self, db, since_hours=72, timeout_s=None, **_):
        if not is_registered():
            raise RuntimeError(
                "internal Report model not registered; "
                "call twin.ingest.internal_reports.register_report_model() at startup")

        model = _registration["model"]
        status_attr = _registration["status_attr"]
        timestamp_attr = _registration["timestamp_attr"]
        cutoff = m.utcnow() - timedelta(hours=since_hours)

        query = db.session.query(model).filter(
            getattr(model, status_attr) == _registration["approved_value"],
            getattr(model, timestamp_attr) >= cutoff,
        )
        return [report_to_dict(r) for r in query.all()]

    def record_count(self, data):
        return len(data) if data else 0


# --------------------------------------------------------------------------
# Report-approval -> SSE hook (Phase 7). Registered separately from the
# adapter above because it needs `db` at import time; kept in this module
# because it is the other half of the same host coupling.
# --------------------------------------------------------------------------

#: The SQLAlchemy `after_update` listener is installed on the model at most
#: once, ever -- but the callback it invokes is swapped through this mutable
#: cell, so calling register_approval_hook() again (a second app boot in the
#: same process, or a test installing its own callback) reconfigures the
#: hook instead of being silently ignored or stacking a duplicate listener.
_active_callback = None
_listener_installed_on = None


def register_approval_hook(db, on_approved):
    """Fire `on_approved(report_dict)` whenever a registered report flips to
    approved. Implemented as a SQLAlchemy `after_update` listener (C6: no
    change to the Report model or schema, additive only).
    """
    global _active_callback, _listener_installed_on
    if not is_registered():
        log.warning("twin: cannot install approval hook, no Report model registered")
        return

    model = _registration["model"]
    _active_callback = on_approved

    if _listener_installed_on is model:
        return  # the after_update listener already dispatches to _active_callback

    from sqlalchemy import event

    event.listen(model, "after_update", _dispatch_approval)
    _listener_installed_on = model
    log.info("twin: report-approval SSE hook installed on %s", model.__name__)


def _dispatch_approval(mapper, connection, target):
    from sqlalchemy import inspect as sa_inspect

    status_attr = _registration["status_attr"]
    approved_value = _registration["approved_value"]

    state = sa_inspect(target)
    history = state.attrs[status_attr].history
    if not history.has_changes():
        return
    new_value = getattr(target, status_attr)
    if new_value != approved_value:
        return
    # Best-effort de-dupe for "already approved, re-saved to the same
    # value": SQLAlchemy only populates history.deleted with the prior
    # value when it actually differs from the new one, so this catches the
    # case where some other in-memory path still has the old value around,
    # but not a same-value reassignment against a freshly loaded row (that
    # case has an empty history.deleted with no distinguishable "old"
    # value to compare, confirmed empirically while building this). The
    # failure direction is a harmless extra SSE notification, never a
    # missed real approval -- acceptable for an incident-detection hook.
    old_values = history.deleted
    if old_values and old_values[0] == approved_value:
        return  # already was approved; not a fresh transition

    if _active_callback is None:
        return
    try:
        _active_callback(report_to_dict(target))
    except Exception:  # noqa: BLE001 - a broken listener must not break the save
        log.exception("twin: approval hook failed for report %s", getattr(target, "id", "?"))


def cells_for_report(lat, lon, h3_resolution=None):
    """(cell_h3_index, [neighbour_h3_indexes]) for a report's coordinates."""
    import h3

    h3_resolution = h3_resolution or twin_config.H3_RESOLUTION
    cell = h3.latlng_to_cell(lat, lon, h3_resolution)
    # h3-py v4's grid_disk returns a list, not a set (confirmed against
    # h3-py 4.5.0 -- older docs/memory suggest set, so this is worth
    # pinning down explicitly rather than assuming).
    neighbours = set(h3.grid_disk(cell, 1)) - {cell}
    return cell, neighbours
