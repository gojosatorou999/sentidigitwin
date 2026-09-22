"""Where a weather system will be, and where heat will bite, hour by hour.

``twin/scoring.py`` answers "how bad is this cell **now**". This module
answers the question an analyst actually acts on: *what is coming, where, and
in how long* -- early enough to warn people before it lands.

Two mechanisms, deliberately kept separate because they fail differently:

**Advection.** A rain or cloud field is carried by the wind. Given a source
point with rain, a wind vector and a lead time, the field's centre moves
``vector * hours``. Project it, find the cells it lands on, and that is an
arrival estimate with a time attached. This is a first-order kinematic
projection, not a numerical weather model: it assumes the system is carried
without growing, decaying or turning. That assumption is good for a couple of
hours and degrades steadily after, which is why :data:`MAX_LEAD_HOURS` is
short and why every projection carries a decaying confidence rather than
presenting hour 9 as though it were hour 1.

**Heat.** Heat does not advect -- it builds in place. It is read directly
from the hourly apparent-temperature series, which already folds in humidity
and wind, and flagged on threshold crossings with the hour they occur.

Everything here is a pure function of numbers in and numbers out, so the
whole forecast is testable without the network, a database or a model key.
No LLM is involved in any quantity on this page: a model writes the brief
that explains a projection, never the projection itself.
"""

import math

#: Beyond this, a single wind vector per point stops describing the field:
#: real systems turn, grow and decay. Projections are still computed for the
#: full window but confidence falls away, and the agent's threshold rejects
#: what it cannot stand behind.
MAX_LEAD_HOURS = 12

#: mm/h. Below this an hour is drizzle, not a system worth moving across a
#: map and warning a district about.
RAIN_SOURCE_MM_H = 2.0

#: mm/h at which an arriving system is worth an analyst's attention, and the
#: higher bar at which it is worth waking someone.
RAIN_ARRIVAL_MM_H = 4.0
RAIN_SEVERE_MM_H = 12.0

#: Apparent temperature (C). The lower bound is where India's IMD heat
#: advisories generally begin for plains cities; the upper is where they
#: escalate. Apparent temperature is used rather than dry-bulb because
#: humidity is what makes Hyderabad and Bengaluru dangerous, not the
#: thermometer alone.
HEAT_WATCH_C = 38.0
HEAT_SEVERE_C = 42.0

#: Cloud cover percentage that counts as a coherent deck worth projecting.
CLOUD_SOURCE_PCT = 70.0

#: km per degree of latitude. Longitude is scaled by cos(lat) at use.
_KM_PER_DEG_LAT = 111.32


def displace(lat, lon, u_kmh, v_kmh, hours):
    """The point a parcel at (lat, lon) reaches after ``hours`` at (u, v).

    ``u`` is eastward and ``v`` northward, both km/h -- the convention
    :func:`twin.ingest.windfield.wind_vector` emits, which is already the
    direction of travel rather than the direction the wind is named for.
    """
    if u_kmh is None or v_kmh is None:
        return None

    north_km = v_kmh * hours
    east_km = u_kmh * hours

    new_lat = lat + north_km / _KM_PER_DEG_LAT
    # Guard the pole: cos(lat) -> 0 makes the longitude step explode. No
    # modelled city is anywhere near it, but an unguarded division here
    # would turn a bad input into a coordinate off the map.
    scale = math.cos(math.radians(max(-89.0, min(89.0, lat))))
    if abs(scale) < 1e-6:
        return (new_lat, lon)
    new_lon = lon + east_km / (_KM_PER_DEG_LAT * scale)
    return (new_lat, new_lon)


def confidence_for(hours, wind_speed_kmh):
    """How much to trust a projection ``hours`` ahead, in 0..1.

    Falls with lead time, because a kinematic projection decays. Also falls
    when the wind is very light: a 2 km/h wind barely moves the field, so its
    *bearing* is noise and the projected direction means little. Fast wind is
    not penalised -- it is the case this method handles best.
    """
    if hours <= 0:
        return 1.0
    lead = max(0.0, 1.0 - (float(hours) / (MAX_LEAD_HOURS + 4.0)))
    if wind_speed_kmh is None:
        return lead * 0.5
    # Below ~5 km/h the bearing is not meaningful; ramp in linearly.
    steadiness = min(1.0, float(wind_speed_kmh) / 5.0)
    return round(lead * (0.4 + 0.6 * steadiness), 3)


def rain_sources(point):
    """Hours at which this lattice point is itself raining hard enough to move.

    Returns ``[{"hour", "precip_mm", "u_kmh", "v_kmh", "wind_speed_kmh"}]``.
    """
    sources = []
    for entry in point.get("hours") or []:
        precip = entry.get("precip_mm")
        if precip is None or precip < RAIN_SOURCE_MM_H:
            continue
        if entry.get("u_kmh") is None or entry.get("v_kmh") is None:
            continue
        sources.append({
            "hour": entry["hour"],
            "precip_mm": precip,
            "cloud_pct": entry.get("cloud_pct"),
            "u_kmh": entry["u_kmh"],
            "v_kmh": entry["v_kmh"],
            "wind_speed_kmh": entry.get("wind_speed_kmh"),
            "wind_dir_deg": entry.get("wind_dir_deg"),
        })
    return sources


def project_rain(points, max_lead_hours=MAX_LEAD_HOURS):
    """Every projected rain arrival from every raining lattice point.

    One entry per (source point, source hour, lead time)::

        {"from": (lat, lon), "to": (lat, lon), "at_hour", "lead_hours",
         "precip_mm", "confidence", "wind_speed_kmh", "wind_dir_deg"}

    Arrivals are *candidates*. Clustering them into a warnable area, and
    deciding which cells they touch, is the agent's job -- this function
    stays a pure projection so it can be tested against hand-worked vectors.
    """
    arrivals = []
    for point in points or []:
        lat, lon = point.get("lat"), point.get("lon")
        if lat is None or lon is None:
            continue
        for source in rain_sources(point):
            for lead in range(1, max_lead_hours + 1):
                destination = displace(lat, lon, source["u_kmh"], source["v_kmh"], lead)
                if destination is None:
                    continue
                # A system that has not left its own doorstep has not
                # "arrived" anywhere; it is still the source cell's problem.
                if _km_between(lat, lon, destination[0], destination[1]) < 1.0:
                    continue
                arrivals.append({
                    "from": (lat, lon),
                    "to": destination,
                    "at_hour": source["hour"] + lead,
                    "lead_hours": lead,
                    "precip_mm": source["precip_mm"],
                    "cloud_pct": source.get("cloud_pct"),
                    "wind_speed_kmh": source.get("wind_speed_kmh"),
                    "wind_dir_deg": source.get("wind_dir_deg"),
                    "confidence": confidence_for(lead, source.get("wind_speed_kmh")),
                })
    return arrivals


def cloud_sources(point):
    """Hours with a coherent cloud deck over this point.

    Cloud is projected as well as rain because a deck arriving ahead of the
    rain is the earliest visible sign an analyst has, and it is what the
    satellite layer in the console actually shows them.
    """
    decks = []
    for entry in point.get("hours") or []:
        cover = entry.get("cloud_pct")
        if cover is None or cover < CLOUD_SOURCE_PCT:
            continue
        if entry.get("u_kmh") is None or entry.get("v_kmh") is None:
            continue
        decks.append({
            "hour": entry["hour"],
            "cloud_pct": cover,
            "u_kmh": entry["u_kmh"],
            "v_kmh": entry["v_kmh"],
            "wind_speed_kmh": entry.get("wind_speed_kmh"),
        })
    return decks


def heat_windows(point):
    """Hours at which apparent temperature crosses the advisory thresholds.

    Returns ``[{"hour", "apparent_c", "temp_c", "humidity_pct", "severity"}]``
    for the *crossings*, not every hot hour: an analyst needs "it passes 42 at
    14:00", not forty rows saying it is warm.
    """
    windows = []
    previous = None
    for entry in point.get("hours") or []:
        apparent = entry.get("apparent_c")
        if apparent is None:
            continue
        severity = heat_severity(apparent)
        if severity and severity != previous:
            windows.append({
                "hour": entry["hour"],
                "apparent_c": apparent,
                "temp_c": entry.get("temp_c"),
                "humidity_pct": entry.get("humidity_pct"),
                "severity": severity,
            })
        previous = severity
    return windows


def heat_severity(apparent_c):
    """``"critical"``, ``"warning"`` or None for an apparent temperature."""
    if apparent_c is None:
        return None
    if apparent_c >= HEAT_SEVERE_C:
        return "critical"
    if apparent_c >= HEAT_WATCH_C:
        return "warning"
    return None


def rain_severity(precip_mm_h, confidence=1.0):
    """Severity for a projected arrival, discounted by how far ahead it is.

    Confidence *lowers* severity rather than filtering the row out, so a
    low-confidence severe projection still surfaces -- as a watch, which is
    what it honestly is -- instead of vanishing.
    """
    if precip_mm_h is None:
        return None
    effective = float(precip_mm_h) * max(0.0, min(1.0, confidence))
    if effective >= RAIN_SEVERE_MM_H:
        return "critical"
    if effective >= RAIN_ARRIVAL_MM_H:
        return "warning"
    if effective >= RAIN_SOURCE_MM_H:
        return "watch"
    return None


def _km_between(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    north = (lat2 - lat1) * _KM_PER_DEG_LAT
    east = (lon2 - lon1) * _KM_PER_DEG_LAT * math.cos(mean_lat)
    return math.hypot(north, east)
