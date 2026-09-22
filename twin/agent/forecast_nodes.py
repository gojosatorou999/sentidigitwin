"""The forecast agent: where the weather is going, and who to warn first.

``nodes.py`` triages what is happening **now** -- alerts in force, cells
already hot, buses already stopped. This graph answers the other half, and
the half that actually saves anyone: *what is coming, where will it land, and
how long have we got*.

The DAG, with the same conditional edge as the triage graph::

    sample -> advect -> detect -> threshold
                                      |
                        nothing projected -> END
                                      |
                               retrieve -> draft_brief -> persist

Three properties are worth stating because they are what make the output
usable rather than impressive:

**No model invents a number.** Wind vectors, arrival times, rain rates and
apparent temperatures all come from ``twin/forecast.py``, which is arithmetic
on Open-Meteo's hourly series. The LLM is given those numbers and asked to
write the paragraph an analyst reads. It never decides *whether* to flag,
*where*, or *when* -- so a hallucination can make a brief read badly, but
cannot move a storm or invent a warning.

**Lead time is the product.** Every flag carries the hour the system arrives
and the confidence at that lead. A projection nine hours out is shown as a
nine-hour projection, not as a fact.

**The human gate is unchanged.** Flags land as ``pending`` in the same
``twin_flag`` table the triage agent uses, so they appear in the same console
queue with the same dispatch buttons. An analyst sends the alert; the agent
never does.
"""

import logging

import h3

from .. import config
from .. import forecast as fx
from .. import models as m
from . import flagstore, llm, rag

log = logging.getLogger("twin.agent.forecast")

#: Lattice spacing for the wind/rain sample, in degrees. Roughly 5-6 km at
#: these latitudes: fine enough that a storm crossing the city is seen at
#: several points, coarse enough that one city is a single API request.
LATTICE_STEP_DEG = 0.05

#: Projected arrivals within this many km are the same system arriving, not
#: two systems. Sized to the lattice so neighbouring sources merge.
CLUSTER_RADIUS_KM = 6.0

#: A cluster needs this many independent projected arrivals before it is
#: worth an analyst's time. One lattice point projecting one hour ahead is
#: noise; four points agreeing is a system.
MIN_ARRIVALS = 3

#: Confidence floor. Below this the projection is too far out or the wind too
#: light to stand behind, and the flag is not raised at all.
MIN_CONFIDENCE = 0.25

#: H3 rings searched around each projected point when matching it to the
#: city's cells. Two rings at resolution 8 is roughly 1.5 km -- inside the
#: projection's own error bar, so this widens the match to the accuracy the
#: method actually has rather than inventing reach it does not.
SNAP_RINGS = 2


# --------------------------------------------------------------------------
# 1. sample -- the lattice, and the hourly fields on it
# --------------------------------------------------------------------------

def sample(state):
    """Fetch hourly wind/rain/cloud/heat for a lattice over the city."""
    from ..ingest.windfield import WindFieldAdapter

    db, city = state["db"], state["city"]
    points = _lattice(city)

    data, snapshot = WindFieldAdapter().run(db, points=points)
    status = snapshot.status if snapshot is not None else "unknown"
    if status not in ("ok", "cached"):
        log.warning("forecast: windfield %s for %s", status, city.slug)

    return {"points": data or [], "field_status": status, "lattice_size": len(points)}


def _lattice(city):
    """Sample points across the city's bbox, inclusive of the far edge.

    A range that stops short of the maximum leaves the downwind edge of the
    city unsampled, which is precisely the edge a system arrives at.
    """
    min_lon, min_lat = city.bbox_min_lon, city.bbox_min_lat
    max_lon, max_lat = city.bbox_max_lon, city.bbox_max_lat

    points, lat = [], min_lat
    while lat <= max_lat + 1e-9:
        lon = min_lon
        while lon <= max_lon + 1e-9:
            points.append((round(lat, 4), round(lon, 4)))
            lon += LATTICE_STEP_DEG
        lat += LATTICE_STEP_DEG
    return points


# --------------------------------------------------------------------------
# 2. advect -- move the fields with the wind
# --------------------------------------------------------------------------

def advect(state):
    """Project rain arrivals, and read heat crossings in place."""
    points = state.get("points") or []

    arrivals = fx.project_rain(points, max_lead_hours=fx.MAX_LEAD_HOURS)

    heat = []
    for point in points:
        for window in fx.heat_windows(point):
            entry = dict(window)
            entry["lat"], entry["lon"] = point.get("lat"), point.get("lon")
            heat.append(entry)

    log.info("forecast %s: %d rain arrivals, %d heat crossings from %d points",
             state["city"].slug, len(arrivals), len(heat), len(points))
    return {"arrivals": arrivals, "heat": heat}


# --------------------------------------------------------------------------
# 3. detect -- cluster arrivals into warnable areas
# --------------------------------------------------------------------------

def detect(state):
    """Group projections into candidate events, each with cells and a lead time."""
    city = state["city"]
    candidates = []

    for cluster in _cluster(state.get("arrivals") or [], key="to"):
        if len(cluster) < MIN_ARRIVALS:
            continue
        # The soonest arrival is the one that matters: it is the deadline.
        first = min(cluster, key=lambda a: a["at_hour"])
        peak = max(cluster, key=lambda a: a["precip_mm"])
        confidence = max(a["confidence"] for a in cluster)
        severity = fx.rain_severity(peak["precip_mm"], confidence)
        if severity is None:
            continue

        centre = _centroid([a["to"] for a in cluster])
        candidates.append({
            "kind": "rain_arrival",
            "hazard_type": "flood" if severity == "critical" else "rain",
            "severity": severity,
            "confidence": confidence,
            "at_hour": first["at_hour"],
            "lead_hours": first["lead_hours"],
            "centre": centre,
            "cells": _cells_for(city, [a["to"] for a in cluster]),
            "peak_mm_h": peak["precip_mm"],
            "wind_speed_kmh": first.get("wind_speed_kmh"),
            "wind_dir_deg": first.get("wind_dir_deg"),
            "source_count": len({a["from"] for a in cluster}),
            "arrival_count": len(cluster),
        })

    for cluster in _cluster(state.get("heat") or [], key=None):
        if not cluster:
            continue
        worst = max(cluster, key=lambda h: h["apparent_c"])
        first = min(cluster, key=lambda h: h["hour"])
        centre = _centroid([(h["lat"], h["lon"]) for h in cluster])
        candidates.append({
            "kind": "heat",
            "hazard_type": "heat",
            "severity": worst["severity"],
            # Heat is read straight from the forecast series rather than
            # projected along a vector, so it does not inherit advection's
            # decay -- but it is still a forecast, and long leads are still
            # less certain.
            "confidence": fx.confidence_for(first["hour"], 999),
            "at_hour": first["hour"],
            "lead_hours": first["hour"],
            "centre": centre,
            "cells": _cells_for(city, [(h["lat"], h["lon"]) for h in cluster]),
            "apparent_c": worst["apparent_c"],
            "humidity_pct": worst.get("humidity_pct"),
            "arrival_count": len(cluster),
            "source_count": len(cluster),
        })

    return {"candidates": candidates}


def _cluster(items, key):
    """Greedy spatial grouping of projections within CLUSTER_RADIUS_KM.

    Greedy rather than proper clustering on purpose: the lattice is regular
    and small, so the result is the same, and an analyst can follow "these
    points are within 6 km of each other" in a way they cannot follow a
    fitted model's cluster assignment.
    """
    def position(item):
        return item[key] if key else (item["lat"], item["lon"])

    clusters = []
    for item in items:
        lat, lon = position(item)
        for cluster in clusters:
            centre = _centroid([position(other) for other in cluster])
            if fx._km_between(centre[0], centre[1], lat, lon) <= CLUSTER_RADIUS_KM:
                cluster.append(item)
                break
        else:
            clusters.append([item])
    return clusters


def _centroid(positions):
    lats = [p[0] for p in positions]
    lons = [p[1] for p in positions]
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _cells_for(city, positions):
    """The H3 cells a set of projected positions lands on.

    Filtered to cells the city actually has, so a projection that lands just
    outside the modelled grid does not create a flag pointing at a cell the
    console cannot draw or drill into.
    """
    wanted = set()
    for lat, lon in positions:
        try:
            centre = h3.latlng_to_cell(lat, lon, config.H3_RESOLUTION)
        except Exception:  # noqa: BLE001 - a bad coordinate must not kill the pass
            continue
        # Snap to the neighbourhood, not the single hex the point lands in.
        # An advection projection is accurate to kilometres at best, and the
        # lattice is coarser than the grid, so demanding exact containment
        # threw away every Hyderabad projection: its clip polygon is GHMC
        # (805 cells, ~600 km2) inside a bbox several times that area, so a
        # system heading for the city usually lands a hex or two off.
        wanted.update(h3.grid_disk(centre, SNAP_RINGS))
    if not wanted:
        return []

    # Intersect with the cells the city actually has. A projection that lands
    # just outside the modelled grid would otherwise create a flag pointing
    # at a cell the console cannot draw or drill into.
    from .. import models as models_module

    known = {row[0] for row in
             db_session_of(city).query(models_module.TwinCell.h3_index)
             .filter(models_module.TwinCell.city_id == city.id)
             .filter(models_module.TwinCell.h3_index.in_(sorted(wanted)))
             .all()}
    return sorted(known)


def db_session_of(city):
    """The session the city row is attached to.

    Threading ``db`` down to this helper would mean changing four signatures
    to carry something SQLAlchemy can already tell us from the instance.
    """
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(city).session


# --------------------------------------------------------------------------
# 4. threshold -- the conditional edge
# --------------------------------------------------------------------------

def threshold(state):
    """Keep only what is worth waking an analyst for.

    Both LLM nodes sit after this, so a calm forecast costs no tokens -- the
    same economics as the triage graph, for the same reason.
    """
    kept = []
    for candidate in state.get("candidates") or []:
        if candidate["confidence"] < MIN_CONFIDENCE:
            continue
        if not candidate["cells"]:
            continue
        if candidate["severity"] not in ("watch", "warning", "critical"):
            continue
        candidate["key"] = _cluster_key(candidate)
        kept.append(candidate)

    kept.sort(key=lambda c: (c["at_hour"], -c["confidence"]))
    return {"flagged": kept, "flag_count": len(kept)}


#: H3 resolution the cluster key is bucketed at. Resolution 5 is ~250 km2 --
#: a district-sized bucket, coarse enough that a storm drifting a few
#: kilometres between passes stays in it.
KEY_RESOLUTION = 5


def _cluster_key(candidate):
    """Stable across polls, so the same storm updates one flag row.

    Bucketed on a **coarse** cell containing the cluster centre, not on the
    hazard's own cells. Two earlier versions of this were wrong in the same
    way: keying on the arrival hour minted a new flag every pass because the
    hour counts down, and keying on ``cells[0]`` minted one whenever the
    projection drifted far enough to reorder the sorted cell list. Both
    produce a queue of near-duplicate flags for one storm, which is how an
    analyst learns to stop reading the queue.

    The hour is deliberately absent, and so is anything else that moves
    between passes. What identifies an event here is *roughly where* and
    *what kind*, which is also how a human would say it.
    """
    lat, lon = candidate["centre"]
    try:
        bucket = h3.latlng_to_cell(lat, lon, KEY_RESOLUTION)
    except Exception:  # noqa: BLE001 - a key is still needed for a bad centre
        bucket = "nocell"
    return "fx:%s:%s" % (candidate["hazard_type"], bucket)


# --------------------------------------------------------------------------
# 5. retrieve -- SOPs for the hazard being projected
# --------------------------------------------------------------------------

def retrieve(state):
    flagged = state.get("flagged") or []
    if not flagged:
        return {"context": {}}

    context = {}
    for candidate in flagged:
        query = "%s response procedure %s" % (
            candidate["hazard_type"], state["city"].display_name)
        try:
            context[candidate["key"]] = rag.search(query, top_k=config.RAG_TOP_K)
        except Exception:  # noqa: BLE001 - retrieval enhances, never gates
            log.exception("forecast RAG lookup failed for %s", candidate["key"])
            context[candidate["key"]] = []
    return {"context": context}


# --------------------------------------------------------------------------
# 6. draft_brief -- the card, written from the numbers
# --------------------------------------------------------------------------

def draft_brief(state):
    """Deterministic brief first; the model only makes it readable."""
    flagged = state.get("flagged") or []
    if not flagged:
        # Return before touching anything else in the state. The graph's
        # conditional edge already skips this node on a calm pass, but a node
        # that only works when it is called in the right order is a trap for
        # the sequential fallback path and for any future caller.
        return {"briefs": []}

    city = state["city"]
    context = state.get("context") or {}
    briefs = []

    for candidate in flagged:
        facts = _fact_lines(candidate, city)
        title = _title_for(candidate, city)
        brief = "\n".join("- " + line for line in facts)
        mode = "rules"

        if llm.available():
            try:
                polished = llm.complete(
                    system=(
                        "You brief a disaster-response analyst. You are given "
                        "the complete set of facts from a kinematic weather "
                        "projection. Write 2-4 short sentences they can act "
                        "on. Use only the numbers given. Do not add causes, "
                        "consequences, place names or advice that is not in "
                        "the facts. Lead with when it arrives. Do not "
                        "reconcile or comment on the projection mechanics; "
                        "the arrival time is the actionable number."),
                    user="City: %s\nProjection facts:\n%s" % (
                        city.display_name, brief),
                )
                if polished and polished.strip():
                    brief = polished.strip() + "\n\n" + brief
                    mode = "llm"
            except Exception:  # noqa: BLE001 - the deterministic brief still ships
                log.exception("forecast brief LLM failed for %s", candidate["key"])

        briefs.append({
            "key": candidate["key"],
            "hazard_type": candidate["hazard_type"],
            "title": title,
            "headline_h3": candidate["cells"][0] if candidate["cells"] else None,
            "cells": candidate["cells"],
            "severity": candidate["severity"],
            "confidence": candidate["confidence"],
            "brief_md": brief,
            "citations": _citations_for(context.get(candidate["key"]) or []),
            "evidence": _evidence_for(candidate),
            "agent_mode": mode,
        })

    return {"briefs": briefs}


def _title_for(candidate, city):
    when = _when(candidate["at_hour"])
    if candidate["kind"] == "heat":
        return "Heat %s %s — %s, %.0f°C apparent" % (
            candidate["severity"], when, city.display_name,
            candidate.get("apparent_c") or 0.0)
    return "Rain arriving %s — %s, %.1f mm/h" % (
        when, city.display_name, candidate.get("peak_mm_h") or 0.0)


def _when(hour):
    if hour <= 1:
        return "within the hour"
    return "in ~%dh" % hour


def _fact_lines(candidate, city):
    # "at_hour" is when it lands, counted from now -- the number an analyst
    # acts on. "lead_hours" is how far downwind the field was carried to get
    # there, which is what the confidence applies to. Labelling both as a
    # "lead" made the two indistinguishable and the model wrote itself in
    # circles trying to reconcile "hour +12" with "lead 1h".
    lines = ["City: %s" % city.display_name,
             "Arrives: %s from now (forecast hour +%d)" % (
                 _when(candidate["at_hour"]), candidate["at_hour"]),
             "Projection: carried %dh downwind of rain already falling at "
             "forecast hour +%d" % (
                 candidate["lead_hours"],
                 candidate["at_hour"] - candidate["lead_hours"]),
             "Confidence in that %dh projection step: %.2f" % (
                 candidate["lead_hours"], candidate["confidence"]),
             "Cells affected: %d" % len(candidate["cells"]),
             "Severity: %s" % candidate["severity"]]

    if candidate["kind"] == "heat":
        lines.append("Peak apparent temperature: %.1f C" % candidate["apparent_c"])
        if candidate.get("humidity_pct") is not None:
            lines.append("Relative humidity: %s%%" % candidate["humidity_pct"])
        lines.append("Heat is read from the forecast series in place, not "
                     "advected, so no projection step applies.")
    else:
        lines.append("Peak rain rate at source: %.1f mm/h" % candidate["peak_mm_h"])
        if candidate.get("wind_speed_kmh") is not None:
            lines.append("Carried by wind at %.1f km/h from %s deg" % (
                candidate["wind_speed_kmh"],
                candidate.get("wind_dir_deg")))
        lines.append("Independent source points agreeing: %d" % candidate["source_count"])
        lines.append("First-order advection projection; the field is assumed "
                     "carried without growth or turning.")
    return lines


def _citations_for(passages):
    return [{"source": p.get("source"), "title": p.get("title"),
             "url": p.get("url"), "score": p.get("score")}
            for p in passages]


def _evidence_for(candidate):
    """The raw numbers that justified the flag (C3): auditable, not prose."""
    evidence = {
        "method": "advection" if candidate["kind"] != "heat" else "threshold_crossing",
        "signal_kinds": [candidate["kind"]],
        "at_hour": candidate["at_hour"],
        "lead_hours": candidate["lead_hours"],
        "confidence": candidate["confidence"],
        "arrival_count": candidate["arrival_count"],
        "source_count": candidate["source_count"],
        "centre": list(candidate["centre"]),
        "official_alerts": [],
    }
    for field in ("peak_mm_h", "apparent_c", "humidity_pct",
                  "wind_speed_kmh", "wind_dir_deg"):
        if candidate.get(field) is not None:
            evidence[field] = candidate[field]
    return evidence


# --------------------------------------------------------------------------
# 7. persist -- same table, same pending gate as the triage agent
# --------------------------------------------------------------------------

def persist(state):
    """File the briefs. Same table, same pending gate as the triage agent."""
    return flagstore.upsert_flags(state["db"], state["city"],
                                  state.get("briefs") or [])
