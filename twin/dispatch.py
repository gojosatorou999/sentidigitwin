"""The one path from the twin to real people's phones.

Everything else in this module is analysis. This is the part that makes a
stranger's phone buzz at 2 a.m., so it is deliberately the most conservative
code here:

* **An analyst must press the button.** The agent raises flags; it never
  dispatches. There is no code path from a scheduler to this module.
* **The count comes before the send.** ``preview`` answers "how many people,
  how far" so nobody discovers the blast radius afterwards.
* **Cooldowns are enforced server-side.** Alert fatigue is what destroys a
  warning system: people who were pinged three times about the same storm stop
  reading the fourth, which is the one that mattered.
* **Every send is audited.** ``twin_dispatch`` records who, when, to how many,
  with what words, over which cells.

The twin does not own users, notifications or WhatsApp -- the host app does.
Rather than importing them (which would break C6 and the module's test
isolation), the host registers three callables at startup, the same pattern
``twin/ingest/internal_reports.py`` uses for the Report model. With nothing
registered, dispatch reports itself unavailable instead of half-working.
"""

import logging
from datetime import timedelta

from . import config
from . import geo
from . import models as m

log = logging.getLogger("twin.dispatch")

_channel = {}


def register_alert_channel(recipients_near, notify=None, send_whatsapp=None):
    """Tell the twin how to reach people.

    ``recipients_near(lat, lon, radius_km)`` must return an iterable of dicts::

        {"id": 12, "username": "asha", "distance_km": 1.8,
         "whatsapp_number": "+9198..."}   # whatsapp_number may be None

    ``notify(user_id, message)`` creates an in-app notification.
    ``send_whatsapp(number, body)`` sends one message and may raise.

    Called once from the host app beside ``create_twin_blueprint``. Idempotent.
    """
    _channel.clear()
    _channel.update({
        "recipients_near": recipients_near,
        "notify": notify,
        "send_whatsapp": send_whatsapp,
    })
    log.info("twin: public alert channel registered")


def is_registered():
    return "recipients_near" in _channel


def describe():
    return {
        "available": is_registered(),
        "whatsapp": bool(_channel.get("send_whatsapp")),
        "in_app": bool(_channel.get("notify")),
        "cooldown_minutes": config.DISPATCH_COOLDOWN_MIN,
        "max_radius_km": config.DISPATCH_MAX_RADIUS_KM,
    }


# --------------------------------------------------------------------------
# Geometry: a flag's cells -> a centre and a radius
# --------------------------------------------------------------------------

def flag_area(db, flag):
    """(centre_lat, centre_lon, radius_km) covering every cell in a flag.

    A circle rather than the exact hexagon union, because the question this
    answers is "who do we warn", and the honest answer errs outward: a person
    300 m past the edge of a flooded cell is in the same street as one inside
    it. ``DISPATCH_BUFFER_KM`` is that deliberate margin.
    """
    import h3

    cells = [row.h3_index for row in
             db.session.query(m.TwinFlagCell).filter_by(flag_id=flag.id).all()]
    if not cells:
        # A city-wide advisory (IMD's "23 districts of Telangana") has no
        # published footprint. Refusing to dispatch it would mean the twin can
        # relay every warning except the broadest ones. The city itself is the
        # area, and the analyst sees the resulting recipient count before
        # committing to it.
        return _city_area(db, flag)

    points = []
    for h3_index in cells:
        try:
            points.append(h3.cell_to_latlng(h3_index))
        except Exception:  # noqa: BLE001 - a malformed index must not block an alert
            continue
    if not points:
        return None

    centre_lat = sum(p[0] for p in points) / len(points)
    centre_lon = sum(p[1] for p in points) / len(points)

    furthest_m = max(
        (geo.haversine_m(centre_lat, centre_lon, lat, lon) for lat, lon in points),
        default=0.0)
    radius_km = furthest_m / 1000.0 + config.DISPATCH_BUFFER_KM
    return centre_lat, centre_lon, min(radius_km, config.DISPATCH_MAX_RADIUS_KM)


def _city_area(db, flag):
    """(lat, lon, radius_km) covering the whole city, for a footprint-less flag."""
    city = db.session.query(m.TwinCity).get(flag.city_id)
    if city is None:
        return None

    corner_m = geo.haversine_m(city.center_latitude, city.center_longitude,
                               city.bbox_max_lat, city.bbox_max_lon)
    radius_km = min(corner_m / 1000.0, config.DISPATCH_MAX_RADIUS_KM)
    return city.center_latitude, city.center_longitude, radius_km


def cooldown_state(db, flag, now=None):
    """(active, minutes_since_last) for this flag's anti-fatigue cooldown."""
    now = now or m.utcnow()
    last = (db.session.query(m.TwinDispatch)
            .filter_by(flag_id=flag.id)
            .order_by(m.TwinDispatch.sent_at.desc())
            .first())
    if last is None:
        return False, None

    minutes = (now - last.sent_at).total_seconds() / 60.0
    return minutes < config.DISPATCH_COOLDOWN_MIN, round(minutes)


# --------------------------------------------------------------------------
# Preview and send
# --------------------------------------------------------------------------

def preview(db, flag, radius_km=None):
    """Who a dispatch would reach, without sending anything."""
    if not is_registered():
        return {"available": False,
                "error": "No alert channel is registered on this deployment."}

    area = flag_area(db, flag)
    if area is None:
        return {"available": False, "error": "This flag covers no mapped cells."}

    centre_lat, centre_lon, default_radius = area
    radius_km = _clamp_radius(radius_km if radius_km is not None else default_radius)

    recipients = list(_channel["recipients_near"](centre_lat, centre_lon, radius_km))
    cooldown_active, minutes_ago = cooldown_state(db, flag)

    return {
        "available": True,
        "flag_id": flag.id,
        "centre": {"lat": round(centre_lat, 5), "lon": round(centre_lon, 5)},
        "radius_km": round(radius_km, 1),
        "recipients": len(recipients),
        "whatsapp_reachable": sum(1 for r in recipients if r.get("whatsapp_number")),
        "nearest": [{"username": r.get("username"),
                     "distance_km": round(r.get("distance_km") or 0.0, 1)}
                    for r in recipients[:5]],
        "cooldown_active": cooldown_active,
        "cooldown_minutes_ago": minutes_ago,
        "message": build_message(flag),
    }


def send(db, flag, sent_by=None, sent_by_username=None, radius_km=None,
         message=None, force=False):
    """Dispatch the alert. Returns a result dict; never raises on a send failure.

    One person's WhatsApp failing must not abort the other four hundred, so
    per-recipient errors are counted and reported rather than propagated.
    """
    if not is_registered():
        return {"success": False,
                "error": "No alert channel is registered on this deployment."}

    area = flag_area(db, flag)
    if area is None:
        return {"success": False, "error": "This flag covers no mapped cells."}

    centre_lat, centre_lon, default_radius = area
    radius_km = _clamp_radius(radius_km if radius_km is not None else default_radius)

    cooldown_active, minutes_ago = cooldown_state(db, flag)
    if cooldown_active and not force:
        return {
            "success": False,
            "cooldown_active": True,
            "cooldown_minutes_ago": minutes_ago,
            "error": ("This area was alerted %s minutes ago. Sending again so soon "
                      "causes alert fatigue -- resend with force if it is warranted."
                      % minutes_ago),
        }

    body = (message or build_message(flag)).strip()
    recipients = list(_channel["recipients_near"](centre_lat, centre_lon, radius_km))

    notified = whatsapp_sent = whatsapp_failed = 0
    notify = _channel.get("notify")
    send_whatsapp = _channel.get("send_whatsapp")

    for recipient in recipients:
        if notify:
            try:
                notify(recipient["id"], _personalise(body, recipient))
                notified += 1
            except Exception:  # noqa: BLE001
                log.exception("twin dispatch: in-app notification failed for %s",
                              recipient.get("id"))

        number = recipient.get("whatsapp_number")
        if number and send_whatsapp:
            try:
                send_whatsapp(number, _personalise(body, recipient))
                whatsapp_sent += 1
            except Exception:  # noqa: BLE001
                whatsapp_failed += 1

    cells = [row.h3_index for row in
             db.session.query(m.TwinFlagCell).filter_by(flag_id=flag.id).all()]

    record = m.TwinDispatch(
        flag_id=flag.id, city_id=flag.city_id, sent_by=sent_by,
        sent_by_username=sent_by_username, message=body,
        radius_km=radius_km, cells=cells, recipients=notified or len(recipients),
        whatsapp_sent=whatsapp_sent, whatsapp_failed=whatsapp_failed,
    )
    db.session.add(record)

    # Dispatched, not merely approved: the flag has had its effect, and the
    # agent must not re-raise the same cluster next tick.
    flag.status = "dispatched"
    flag.reviewed_by = sent_by
    flag.reviewed_at = m.utcnow()
    db.session.commit()

    log.info("twin dispatch: flag %s -> %d recipients (%d whatsapp) by %s",
             flag.id, record.recipients, whatsapp_sent, sent_by_username)

    return {
        "success": True,
        "flag_id": flag.id,
        "dispatch_id": record.id,
        "recipients": record.recipients,
        "whatsapp_sent": whatsapp_sent,
        "whatsapp_failed": whatsapp_failed,
        "radius_km": round(radius_km, 1),
        "message": body,
    }


def _clamp_radius(radius_km):
    try:
        radius_km = float(radius_km)
    except (TypeError, ValueError):
        radius_km = config.DISPATCH_BUFFER_KM
    return max(0.5, min(radius_km, config.DISPATCH_MAX_RADIUS_KM))


def _personalise(body, recipient):
    distance = recipient.get("distance_km")
    if distance is None:
        return body
    return "%s\n\nYou are about %.1f km from the affected area." % (body, distance)


def build_message(flag):
    """The public-facing text.

    Written from the flag's own fields rather than by a model, and quoting the
    issuing authority where there is one. Someone receiving this at night needs
    to know what, where, who says so, and what to do -- in that order, in about
    four lines.
    """
    hazard = (flag.hazard_type or "hazard").replace("_", " ")
    severity = (flag.severity or "watch").upper()

    official = ((flag.evidence or {}).get("official_alerts") or [])
    authority_line = ""
    if official:
        first = official[0]
        authority_line = "\nSource: %s -- %s." % (
            first.get("sender") or "Official alert", first.get("event") or "warning")

    instruction = ""
    for alert in official:
        if alert.get("instruction"):
            instruction = "\n\n" + alert["instruction"].strip()
            break
    if not instruction:
        instruction = ("\n\nAvoid low-lying roads and underpasses, and do not attempt "
                       "to cross moving water.") if hazard in ("flood", "rain") else (
            "\n\nFollow local authority guidance and avoid the affected area.")

    return ("SENTINEL ALERT -- %s (%s)\n%s%s%s" % (
        hazard.upper(), severity, (flag.title or "").strip(), authority_line, instruction
    )).strip()
