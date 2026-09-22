"""Official external alerts: resolve, persist, expire, serve.

``twin/ingest/sachet.py`` knows the shape of a CAP document; this module knows
what the twin does with one. The split matters because persistence is where
the two rules that keep this feature honest are enforced:

1. **Re-polling must be idempotent.** SACHET republishes the same alert every
   five minutes for its whole lifetime. ``UNIQUE(source, source_uid)`` plus an
   upsert means a feed read updates rows; without it, a warning that lives for
   three hours becomes 36 duplicate "incidents" and the incident sub-score
   climbs purely because time passed.

2. **An update supersedes; it does not add.** ``cap:msgType=Update`` names the
   alert it replaces in ``cap:references``. Ignoring that double-counts every
   revised warning -- and warnings are revised constantly, because that is how
   IMD narrows a forecast as it firms up.

Expiry is treated as authoritative: past ``cap:expires`` an alert stops
contributing to any score. An expired warning left in place is worse than no
warning, because it looks current.
"""

import json
import logging
import re

import h3

from . import config
from . import models as m
from .ingest.sachet import (
    SachetCapAdapter, hazard_type_for, parse_cap_datetime,
)

log = logging.getLogger("twin.alerts")



def ingest_city_alerts(db, city, adapter=None, states=None):
    """Poll the feeds relevant to `city`, persist, and return a summary.

    Never raises: the adapter degrades to cache or to an empty alert list, and
    a malformed individual alert is skipped with a log line. A disaster feed
    that goes down must cost one layer, never the twin (C1).
    """
    adapter = adapter or SachetCapAdapter()
    states = states or _states_for_city(city)

    created = updated = superseded = skipped = 0
    statuses = []

    for state in states:
        data, snapshot = adapter.run(db, city_id=city.id, state=state)
        statuses.append(snapshot.status if snapshot else "failed")

        for raw in (data or {}).get("alerts") or []:
            try:
                outcome = _upsert_alert(db, city, raw, source="sachet")
            except Exception:  # noqa: BLE001 - one bad alert must not stop the rest
                log.exception("alert upsert failed for %s", raw.get("source_uid"))
                skipped += 1
                continue
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            else:
                skipped += 1

    superseded = _apply_supersessions(db, city)
    db.session.commit()

    return {
        "city": city.slug,
        "states": list(states),
        "created": created,
        "updated": updated,
        "superseded": superseded,
        "skipped": skipped,
        "status": "ok" if all(s == "ok" for s in statuses) else "degraded",
        "active": active_alert_count(db, city),
    }


def _states_for_city(city):
    state = (city.state or "").strip().lower()
    if state and state in config.SACHET_STATES:
        return (state,)
    return (state,) if state else config.SACHET_STATES


def _upsert_alert(db, city, raw, source="sachet"):
    """Insert or update one alert row. Returns 'created'|'updated'|'skipped'."""
    source_uid = raw.get("source_uid") or raw.get("guid") or raw.get("identifier")
    if not source_uid:
        return "skipped"

    geometry = raw.get("geometry")
    geometry_kind = raw.get("geometry_kind")

    # An alert with a polygon that misses the city entirely is not this city's
    # problem and is discarded rather than stored -- the feeds are state-wide,
    # so most items in them are about somewhere else.
    cells = cells_for_geometry(db, city, geometry) if geometry else set()
    area_confidence = "polygon"
    if geometry and not cells:
        return "skipped"
    if not geometry:
        area_confidence = targets_city(raw, city)
        if area_confidence is None:
            return "skipped"

    row = (db.session.query(m.TwinExternalAlert)
           .filter_by(source=source, source_uid=str(source_uid))
           .one_or_none())
    outcome = "updated"
    if row is None:
        row = m.TwinExternalAlert(source=source, source_uid=str(source_uid))
        db.session.add(row)
        outcome = "created"

    row.city_id = city.id
    row.cap_identifier = raw.get("identifier")
    row.sender = raw.get("sender_name") or raw.get("sender") or raw.get("author")
    row.event = raw.get("event") or raw.get("rss_title") or raw.get("title")
    row.category = raw.get("category")
    row.severity = raw.get("severity")
    row.certainty = raw.get("certainty")
    row.urgency = raw.get("urgency")
    row.msg_type = raw.get("msg_type")
    row.headline = raw.get("headline")
    row.description = raw.get("description")
    row.instruction = raw.get("instruction")
    row.area_desc = raw.get("area_desc")
    row.geometry_kind = geometry_kind
    row.geometry_geojson = json.dumps(geometry) if geometry else None
    row.effective_at = parse_cap_datetime(raw.get("effective") or raw.get("sent")
                                          or raw.get("pub_date"))
    row.onset_at = parse_cap_datetime(raw.get("onset"))
    row.expires_at = parse_cap_datetime(raw.get("expires"))
    row.sent_at = parse_cap_datetime(raw.get("sent") or raw.get("pub_date"))
    row.references_uid = raw.get("references")
    row.raw_url = raw.get("raw_url") or raw.get("link")
    row.raw = {k: v for k, v in raw.items() if k not in ("geometry", "polygon_points")}
    # How the alert was tied to this city, so the card can say "state-wide
    # advisory, geography inferred" rather than implying a surveyed footprint.
    row.raw["area_confidence"] = area_confidence
    row.fetched_at = m.utcnow()

    db.session.flush()
    _replace_alert_cells(db, city, row, cells)
    return outcome


def _replace_alert_cells(db, city, row, cells):
    """Make twin_alert_cell match `cells` exactly for this alert.

    Rewritten rather than appended because an Update can *shrink* a warning's
    footprint, and a cell that silently keeps an alert it is no longer in is
    the kind of error nobody notices until someone is evacuated needlessly.
    """
    existing = {c.h3_index: c for c in
                db.session.query(m.TwinAlertCell).filter_by(alert_id=row.id).all()}

    for h3_index in cells:
        if h3_index not in existing:
            db.session.add(m.TwinAlertCell(
                alert_id=row.id, city_id=city.id, h3_index=h3_index))
    for h3_index, link in existing.items():
        if h3_index not in cells:
            db.session.delete(link)


def _apply_supersessions(db, city):
    """Mark every alert named in a live alert's cap:references as superseded."""
    live = (db.session.query(m.TwinExternalAlert)
            .filter(m.TwinExternalAlert.city_id == city.id,
                    m.TwinExternalAlert.references_uid.isnot(None),
                    m.TwinExternalAlert.superseded_at.is_(None))
            .all())

    referenced = set()
    for row in live:
        for token in _reference_uids(row.references_uid):
            referenced.add(token)
    if not referenced:
        return 0

    count = 0
    for row in (db.session.query(m.TwinExternalAlert)
                .filter(m.TwinExternalAlert.city_id == city.id,
                        m.TwinExternalAlert.superseded_at.is_(None))
                .all()):
        if row.cap_identifier in referenced or row.source_uid in referenced:
            row.superseded_at = m.utcnow()
            count += 1
    return count


def _reference_uids(references):
    """``sender,identifier,sent`` triples, comma- and space-separated.

    CAP allows several references in one element, each itself comma-separated,
    which means the identifier is the *middle* field of each triple -- not the
    whole token. Splitting on commas alone and comparing the result to an
    identifier matches nothing.
    """
    uids = set()
    for triple in str(references or "").split():
        parts = triple.split(",")
        if len(parts) >= 2:
            uids.add(parts[1].strip())
        elif parts and parts[0].strip():
            uids.add(parts[0].strip())
    return uids


def targets_city(raw, city):
    """Is a polygon-less alert about this city? Returns a confidence label or None.

    Three ways to tell, in descending order of certainty, because IMD writes
    the area three different ways and only the first is unambiguous:

    * ``district_code`` -- a ``cap:geocode`` LGD district code matches one of
      the city's own. Authoritative when the code list is populated.
    * ``named`` -- the areaDesc or headline names the city or one of its
      districts ("Bengaluru Rural,Bengaluru Urban districts of Karnataka").
    * ``state_wide`` -- the areaDesc is the unhelpful but extremely common
      "23 districts of Telangana", naming nobody. A warning covering most of
      the state almost certainly covers its capital, so it is kept -- but
      labelled, so an operator reading the card knows the geography was
      inferred rather than stated.

    Deliberately string and code matching rather than a model call: this
    decides whether an alert is stored at all, it runs on every item of every
    poll, and a false positive silently attributes another district's warning
    to this city.
    """
    codes = set()
    for name, values in (raw.get("geocodes") or {}).items():
        if "district" in str(name).lower() or "lgd" in str(name).lower():
            codes.update(str(v).strip() for v in _as_list(values))
    city_codes = {str(c) for c in config.CITY_LGD_DISTRICT_CODES.get(city.slug, ())}
    if codes and city_codes and (codes & city_codes):
        return "district_code"

    haystack = " ".join(str(v or "").lower() for v in (
        raw.get("area_desc"), raw.get("headline"), raw.get("event"),
        raw.get("rss_title"), raw.get("title"), raw.get("description")))

    needles = [city.slug.lower(), (city.display_name or "").lower()]
    needles.extend(config.CITY_ALERT_ALIASES.get(city.slug, ()))
    if any(needle and needle in haystack for needle in needles):
        return "named"

    # "N districts of <State>" with no names at all.
    state = (city.state or "").strip().lower()
    area_desc = (raw.get("area_desc") or "").strip().lower()
    match = re.match(r"^(\d+)\s+districts?\s+of\s+(.+)$", area_desc)
    if match and state and state in match.group(2):
        return "state_wide"

    # A code list that names districts we cannot resolve yet. Storing it as an
    # unresolved advisory is more honest than dropping it: the alert is real,
    # and scripts/learn_lgd_codes.py fills the mapping in from the feed itself.
    if codes and not city_codes and state and state in area_desc:
        return "state_wide"

    return None


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, (list, tuple, set)) else [value]


# --------------------------------------------------------------------------
# Geometry -> cells
# --------------------------------------------------------------------------

def cells_for_geometry(db, city, geometry):
    """H3 indexes of this city's cells covered by a GeoJSON polygon.

    Tests the city's own cell centres for containment rather than filling the
    polygon with ``h3.polygon_to_cells``. The fill approach is the obvious one
    and it is wrong here: ``polygon_to_cells`` keeps a cell only when its
    *centre* falls inside the polygon, so an alert footprint smaller than one
    cell -- or a long thin one along a river, which is exactly the shape a
    flood warning has -- fills to nothing and the alert silently covers
    nowhere. Walking the city's cells is also bounded work (~1,700 rows) and
    needs no resolution juggling.

    Only cells that exist in ``twin_cell`` are returned: a footprint outside
    the modelled grid is meaningless to the twin, and storing cells the map
    cannot draw produces "affected areas" nobody can click.
    """
    if not geometry:
        return set()

    try:
        from shapely.geometry import Point, shape
        from shapely.prepared import prep

        polygon = shape(geometry)
    except Exception as exc:  # noqa: BLE001 - malformed geometry from a feed
        log.warning("alert polygon is unusable (%s)", exc)
        return set()

    if polygon.is_empty:
        return set()
    if not polygon.is_valid:
        polygon = polygon.buffer(0)

    min_lon, min_lat, max_lon, max_lat = polygon.bounds
    rows = (db.session.query(m.TwinCell.h3_index,
                             m.TwinCell.center_latitude, m.TwinCell.center_longitude)
            .filter(m.TwinCell.city_id == city.id,
                    m.TwinCell.center_latitude >= min_lat,
                    m.TwinCell.center_latitude <= max_lat,
                    m.TwinCell.center_longitude >= min_lon,
                    m.TwinCell.center_longitude <= max_lon)
            .all())

    prepared = prep(polygon)
    cells = {h3_index for h3_index, lat, lon in rows
             if prepared.contains(Point(lon, lat))}

    # A footprint narrower than one cell contains no centre at all. Falling
    # back to the cells the polygon *touches* keeps such an alert on the map
    # instead of dropping it, which matters most for the small, precise
    # warnings -- an underpass, one lake -- that deserve the attention.
    if not cells:
        cells = {
            h3_index for h3_index, lat, lon in rows
            if polygon.intersects(_cell_polygon(h3_index))
        }
    return cells


def _cell_polygon(h3_index):
    from shapely.geometry import Polygon

    ring = h3.cell_to_boundary(h3_index)  # ((lat, lng), ...) in h3 v4
    return Polygon([(lon, lat) for lat, lon in ring])


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

def active_alerts(db, city=None, now=None, include_district=True):
    """Every alert that is currently in force.

    "In force" means: not expired, not superseded, and either already
    effective or effective within the hour (a warning issued for 20:00 is
    operationally relevant at 19:45). ``cap:expires`` is authoritative -- see
    the module docstring.
    """
    now = now or m.utcnow()
    query = db.session.query(m.TwinExternalAlert).filter(
        m.TwinExternalAlert.superseded_at.is_(None))
    if city is not None:
        query = query.filter(m.TwinExternalAlert.city_id == city.id)

    rows = []
    for row in query.order_by(m.TwinExternalAlert.effective_at.desc()).all():
        if row.expires_at is not None and row.expires_at <= now:
            continue
        if not include_district and row.geometry_kind != "polygon":
            continue
        rows.append(row)
    return rows


def active_alert_count(db, city=None, now=None):
    return len(active_alerts(db, city=city, now=now))


def alert_cells_by_h3(db, city, now=None):
    """{h3_index: [alert_row, ...]} for alerts in force with a real footprint."""
    rows = active_alerts(db, city=city, now=now, include_district=True)
    by_id = {row.id: row for row in rows}
    if not by_id:
        return {}

    links = (db.session.query(m.TwinAlertCell)
             .filter(m.TwinAlertCell.alert_id.in_(list(by_id)))
             .all())

    result = {}
    for link in links:
        alert = by_id.get(link.alert_id)
        if alert is not None:
            result.setdefault(link.h3_index, []).append(alert)
    return result


def alert_to_dict(row):
    """One alert as the API and the agent both see it."""
    return {
        "id": row.id,
        "source": row.source,
        "source_uid": row.source_uid,
        "identifier": row.cap_identifier,
        "sender": row.sender,
        "event": row.event,
        "hazard_type": hazard_type_for({
            "event": row.event, "headline": row.headline, "category": row.category}),
        "category": row.category,
        "severity": row.severity,
        "certainty": row.certainty,
        "urgency": row.urgency,
        "msg_type": row.msg_type,
        "headline": row.headline,
        "instruction": row.instruction,
        "area_desc": row.area_desc,
        "geometry_kind": row.geometry_kind,
        "area_confidence": (row.raw or {}).get("area_confidence"),
        "effective_at": row.effective_at.isoformat() if row.effective_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "url": row.raw_url,
        "attribution": "NDMA SACHET / CAP (Government of India)",
    }


def alerts_feature_collection(db, city, now=None):
    """GeoJSON for the map layer, plus the alerts that have no drawable shape.

    District-scoped alerts are returned in ``advisories`` rather than as
    geometry. They are real and an operator must see them, but drawing a
    made-up boundary for one would present an estimate as a survey -- the
    module's standing rule is that a guess must look like a guess.
    """
    features = []
    advisories = []

    for row in active_alerts(db, city=city, now=now):
        payload = alert_to_dict(row)
        if row.geometry_kind == "polygon" and row.geometry_geojson:
            try:
                geometry = json.loads(row.geometry_geojson)
            except (TypeError, ValueError):
                continue
            features.append({"type": "Feature", "geometry": geometry,
                             "properties": payload})
        else:
            advisories.append(payload)

    return {
        "type": "FeatureCollection",
        "features": features,
        "advisories": advisories,
        "attribution": "NDMA SACHET / CAP (Government of India)",
    }
