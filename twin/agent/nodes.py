"""The triage pipeline's nodes: gather, extract, correlate, score, brief, persist.

Each node takes the shared state dict and returns the keys it changed, which
is exactly LangGraph's contract -- so the same functions run inside a compiled
``StateGraph`` when langgraph is installed and in a plain sequence when it is
not (see ``graph.py``). Keeping them free of graph machinery is what makes
that possible, and what makes each one testable on its own.

**The rule that governs this whole package: the LLM never computes risk.**

``score`` calls ``twin/scoring.py`` -- the same pure functions the map uses --
and it is a plain Python node, not a tool the model may reason about. When an
official acts on a flag and is asked "why was this area flagged", the answer
has to be "62 mm against a 30 mm p95 for September here, plus a Severe IMD
warning", not "the model said so". Scores must be reproducible, and a model at
temperature 0 is still not reproducible across a provider's silent version
bump.

What the LLM does instead is language: reading prose headlines into structured
fields, deciding that four sources are describing one event, and writing the
brief a human reads. Those are judgement calls that regexes genuinely lose.
"""

import hashlib
import logging

from .. import alerts as alerts_module
from .. import anomaly
from .. import config
from .. import live as live_module
from .. import models as m
from ..ingest.sachet import hazard_type_for
from . import flagstore, llm, rag

log = logging.getLogger("twin.agent.nodes")

#: Cells at least this close to the flag threshold are gathered, so a cluster
#: can form from several near-threshold cells rather than needing one that
#: already breaches on its own.
GATHER_MARGIN = 12.0

#: An official alert this severe is always worth an analyst's eye, whatever
#: the computed score says. The twin models rain and terrain well; it does not
#: model everything IMD warns about, and refusing to surface an Extreme
#: warning because the hexagons look calm would be the worst kind of silence.
ALWAYS_FLAG_SEVERITIES = ("extreme",)


# --------------------------------------------------------------------------
# 1. gather -- everything currently true about this city
# --------------------------------------------------------------------------

def gather(state):
    """Pull the live picture: alerts in force, hot cells, disruption, stations."""
    db, city = state["db"], state["city"]

    active = alerts_module.active_alerts(db, city)
    alert_cells = alerts_module.alert_cells_by_h3(db, city)

    threshold = max(0.0, config.FLAG_THRESHOLD - GATHER_MARGIN)
    hot_cells = _hot_cells(db, city, threshold)
    disruption = live_module.transit_disruption(db, city)

    signals = []
    for alert in active:
        signals.append({
            "kind": "official_alert",
            "id": "alert:%s" % alert.id,
            "hazard_type": hazard_type_for({
                "event": alert.event, "headline": alert.headline,
                "category": alert.category}),
            "severity": alert.severity,
            "certainty": alert.certainty,
            "sender": alert.sender,
            "event": alert.event,
            "headline": alert.headline,
            "area_desc": alert.area_desc,
            "instruction": alert.instruction,
            "url": alert.raw_url,
            "expires_at": alert.expires_at.isoformat() if alert.expires_at else None,
            "cells": [h3 for h3, rows in alert_cells.items()
                      if any(row.id == alert.id for row in rows)],
            "geometry_kind": alert.geometry_kind,
        })

    for cell in hot_cells:
        signals.append({
            "kind": "risk_cell",
            "id": "cell:%s" % cell["h3"],
            "hazard_type": _hazard_from_inputs(cell),
            "risk_score": cell["risk_score"],
            "status": cell["status"],
            "cells": [cell["h3"]],
            "raw_inputs": cell["raw_inputs"],
        })

    for h3_index, stats in disruption.items():
        score = anomaly.transit_disruption_score(stats)
        if score is None or score < 40:
            continue
        signals.append({
            "kind": "transit_disruption",
            "id": "transit:%s" % h3_index,
            "hazard_type": "road_blockage",
            "cells": [h3_index],
            "stats": stats,
            "disruption_score": score,
        })

    return {
        "signals": signals,
        "alerts": active,
        "hot_cells": hot_cells,
        "disruption": disruption,
    }


def _hot_cells(db, city, threshold):
    """Cells at or near the flag threshold, at the 'now' horizon."""
    rows = (db.session.query(m.TwinCellState, m.TwinCell)
            .join(m.TwinCell, m.TwinCellState.cell_id == m.TwinCell.id)
            .filter(m.TwinCell.city_id == city.id,
                    m.TwinCellState.horizon_hours == 0,
                    m.TwinCellState.risk_score >= threshold)
            .order_by(m.TwinCellState.risk_score.desc())
            .limit(200)
            .all())

    return [{
        "h3": cell.h3_index,
        "cell_id": cell.id,
        "risk_score": state.risk_score,
        "status": state.status,
        "lat": cell.center_latitude,
        "lon": cell.center_longitude,
        "raw_inputs": state.raw_inputs or {},
        "sub_scores": {
            "hydro": state.hydro_score, "incident": state.incident_score,
            "env": state.env_score, "terrain": state.terrain_score,
            "infra": state.infra_score,
        },
    } for state, cell in rows]


def _hazard_from_inputs(cell):
    """Name the dominant hazard from the numbers that produced the score."""
    raw = cell.get("raw_inputs") or {}
    if (raw.get("rain_now_mm_1h") or 0) > 5 or (raw.get("rain_forecast_mm") or 0) > 10:
        return "flood" if (raw.get("dist_to_water_m") or 9999) < 600 else "rain"
    if (raw.get("transit_stall_rate") or 0) > 0.5:
        return "road_blockage"
    if (raw.get("station_aqi") or raw.get("us_aqi") or 0) > 200:
        return "air_quality"
    if (raw.get("incident_count_raw") or 0) > 0:
        return "incident"
    return "other"


# --------------------------------------------------------------------------
# 2. extract -- prose into structure (LLM job 1)
# --------------------------------------------------------------------------

def extract(state):
    """Normalise every signal into one shape.

    Deterministically first, because CAP alerts are already structured and
    parsing a machine-readable field with a language model would be daft. The
    LLM is then offered only the free-text headlines, and only to fill fields
    the document did not state -- and its answer is constrained to vocabularies
    defined here, so it can never invent a hazard type or a severity.
    """
    signals = state.get("signals") or []
    if not signals:
        return {"extracted": []}

    extracted = [dict(signal) for signal in signals]

    prose_signals = [s for s in extracted
                     if s["kind"] == "official_alert" and (s.get("headline") or s.get("event"))]
    if not prose_signals or not llm.available():
        return {"extracted": extracted}

    payload = [{
        "id": signal["id"],
        "text": " ".join(filter(None, [signal.get("event"), signal.get("headline"),
                                       signal.get("area_desc")]))[:600],
    } for signal in prose_signals[:20]]

    result = llm.complete_json(
        system=(
            "You read Indian disaster management alerts (IMD, NDMA, SDMA) and "
            "return structured fields. You never invent locations, numbers or "
            "severities. If a field is not stated in the text, use null."),
        user=(
            "For each item return an object with: id, hazard_type (one of: "
            "flood, rain, heat, earthquake, fire, air_quality, road_blockage, "
            "other), urgency_hint (immediate, soon, later, null), and "
            "plain_summary (one sentence, under 20 words, no adjectives that "
            "are not in the source).\n\n"
            "Return {\"items\": [...]}\n\n" + _dumps(payload)),
        fallback=None)

    if not result:
        return {"extracted": extracted}

    allowed = {"flood", "rain", "heat", "earthquake", "fire", "air_quality",
               "road_blockage", "other"}
    by_id = {signal["id"]: signal for signal in extracted}
    for item in (result.get("items") or []):
        signal = by_id.get(item.get("id"))
        if signal is None:
            continue
        hazard = (item.get("hazard_type") or "").strip().lower()
        if hazard in allowed:
            signal["hazard_type"] = hazard
        if item.get("plain_summary"):
            signal["plain_summary"] = str(item["plain_summary"])[:200]

    return {"extracted": extracted}


# --------------------------------------------------------------------------
# 3. correlate -- many signals, one event (LLM job 2)
# --------------------------------------------------------------------------

def correlate(state):
    """Group signals that are describing the same thing.

    Spatially first and always: two signals sharing a cell, or sitting in
    adjacent cells, and naming a related hazard are one event. That is done
    deterministically because it is geometry, not judgement.

    The LLM is then asked only to *merge* clusters the geometry kept apart --
    a warning whose polygon stops at a ward boundary and a set of hot cells
    just outside it, say -- and it may only reference cluster ids that already
    exist. It cannot create a cluster, move a cell, or split one.
    """
    extracted = state.get("extracted") or []
    if not extracted:
        return {"clusters": []}

    clusters = _spatial_clusters(extracted)
    if len(clusters) > 1 and llm.available():
        clusters = _llm_merge(clusters)

    return {"clusters": clusters}


def _spatial_clusters(signals):
    """Union signals that share cells or neighbour each other."""
    import h3

    clusters = []
    citywide = {}

    for signal in signals:
        cells = set(signal.get("cells") or [])
        if not cells:
            # An official alert with no drawable footprint -- IMD's very common
            # "23 districts of Telangana" -- still has to reach an analyst.
            # Dropping it here (the obvious reading of "no cells, no cluster")
            # silently discarded every state-wide warning, including Extreme
            # ones, which is the exact opposite of what a warning system is
            # for. It becomes a city-wide cluster instead: no polygon, because
            # nobody published one, but a real entry in the queue.
            if signal["kind"] != "official_alert":
                continue
            hazard = signal.get("hazard_type") or "other"
            cluster = citywide.setdefault(hazard, {
                "signals": [], "cells": set(), "neighbourhood": set(),
                "hazard_type": hazard, "citywide": True,
            })
            cluster["signals"].append(signal)
            continue

        neighbourhood = set(cells)
        for cell in cells:
            try:
                neighbourhood.update(h3.grid_disk(cell, 1))
            except Exception:  # noqa: BLE001 - a malformed index must not stop triage
                continue

        merged_into = None
        for cluster in clusters:
            if not (cluster["neighbourhood"] & cells):
                continue
            if not _hazards_compatible(cluster["hazard_type"], signal.get("hazard_type")):
                continue
            cluster["signals"].append(signal)
            cluster["cells"].update(cells)
            cluster["neighbourhood"].update(neighbourhood)
            merged_into = cluster
            break

        if merged_into is None:
            clusters.append({
                "signals": [signal],
                "cells": set(cells),
                "neighbourhood": neighbourhood,
                "hazard_type": signal.get("hazard_type") or "other",
            })

    clusters.extend(citywide.values())

    for index, cluster in enumerate(clusters):
        cluster["key"] = _cluster_key(cluster)
        cluster["index"] = index
        cluster["hazard_type"] = _dominant_hazard(cluster)
    return clusters


#: Hazards that describe the same underlying weather event and should merge.
_HAZARD_FAMILY = {
    "flood": {"flood", "rain", "road_blockage"},
    "rain": {"rain", "flood", "road_blockage"},
    "road_blockage": {"road_blockage", "flood", "rain", "incident"},
    "incident": {"incident", "flood", "rain", "road_blockage", "other"},
}


def _hazards_compatible(existing, candidate):
    if not existing or not candidate or existing == candidate:
        return True
    return candidate in _HAZARD_FAMILY.get(existing, {existing})


def _dominant_hazard(cluster):
    """Prefer the hazard an official alert named over one the twin inferred."""
    for signal in cluster["signals"]:
        if signal["kind"] == "official_alert" and signal.get("hazard_type"):
            return signal["hazard_type"]
    for signal in cluster["signals"]:
        if signal.get("hazard_type") and signal["hazard_type"] != "other":
            return signal["hazard_type"]
    return "other"


def _cluster_key(cluster):
    """A stable id for this event, so re-running triage updates its flag.

    Keyed on hazard family plus the sorted cell set: the same storm over the
    same cells produces the same key ten minutes later, and the existing flag
    is updated rather than a duplicate raised every agent tick.

    A city-wide cluster has no cells, so it is keyed on the source alerts
    instead -- otherwise every state-wide advisory of the same hazard type
    would collapse into one permanently-updating flag.
    """
    if cluster.get("citywide"):
        source_ids = ",".join(sorted(s["id"] for s in cluster["signals"]))
        raw = "citywide|%s|%s" % (cluster.get("hazard_type") or "other", source_ids)
    else:
        raw = "%s|%s" % (cluster.get("hazard_type") or "other",
                         ",".join(sorted(cluster["cells"])))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _llm_merge(clusters):
    """Let the model merge clusters, within strict limits."""
    payload = [{
        "id": cluster["index"],
        "hazard": cluster["hazard_type"],
        "cell_count": len(cluster["cells"]),
        "sources": [s.get("event") or s.get("kind") for s in cluster["signals"]][:4],
    } for cluster in clusters[:15]]

    result = llm.complete_json(
        system=("You group civic hazard signals that describe the SAME real-world "
                "event. Be conservative: when unsure, keep them separate. You may "
                "only use the ids given."),
        user=("Return {\"groups\": [[id, id, ...], ...]} listing ids that belong to "
              "one event. Every id must appear exactly once.\n\n" + _dumps(payload)),
        fallback=None)

    groups = (result or {}).get("groups")
    if not groups:
        return clusters

    by_index = {cluster["index"]: cluster for cluster in clusters}
    merged, claimed = [], set()
    for group in groups:
        members = [by_index[i] for i in group
                   if isinstance(i, int) and i in by_index and i not in claimed]
        if not members:
            continue
        claimed.update(member["index"] for member in members)

        head = members[0]
        for other in members[1:]:
            head["signals"].extend(other["signals"])
            head["cells"].update(other["cells"])
            head["neighbourhood"].update(other["neighbourhood"])
        head["hazard_type"] = _dominant_hazard(head)
        head["key"] = _cluster_key(head)
        merged.append(head)

    # Anything the model forgot stays as its own cluster: a dropped group must
    # never silently delete a flagged area.
    merged.extend(cluster for cluster in clusters if cluster["index"] not in claimed)
    return merged


# --------------------------------------------------------------------------
# 4. score -- DETERMINISTIC. Never an LLM.
# --------------------------------------------------------------------------

def score(state):
    """Attach the twin's own risk numbers to each cluster.

    Reads ``twin_cell_state`` -- the rows the map is drawn from -- so a flag
    and the hexagons underneath it can never disagree. Nothing here is
    computed by, or shown to, a language model.
    """
    db, city = state["db"], state["city"]
    clusters = state.get("clusters") or []
    if not clusters:
        return {"scored": []}

    all_cells = sorted({cell for cluster in clusters for cell in cluster["cells"]})

    # A city-wide advisory has no cells at all, so an early return here would
    # drop exactly the alerts that cover the most people. Only the lookup is
    # skipped; the clusters are still scored, on the authority's severity.
    by_h3 = {}
    if all_cells:
        rows = (db.session.query(m.TwinCellState, m.TwinCell)
                .join(m.TwinCell, m.TwinCellState.cell_id == m.TwinCell.id)
                .filter(m.TwinCell.city_id == city.id,
                        m.TwinCell.h3_index.in_(all_cells),
                        m.TwinCellState.horizon_hours == 0)
                .all())
        by_h3 = {cell.h3_index: (state_row, cell) for state_row, cell in rows}

    scored = []
    for cluster in clusters:
        members = [by_h3[cell] for cell in cluster["cells"] if cell in by_h3]
        risks = [state_row.risk_score or 0.0 for state_row, _cell in members]

        peak_risk = max(risks) if risks else 0.0
        mean_risk = (sum(risks) / len(risks)) if risks else 0.0
        peak_h3 = None
        centre = None
        if members:
            peak_state, peak_cell = max(members, key=lambda pair: pair[0].risk_score or 0.0)
            peak_h3 = peak_cell.h3_index
            centre = {"lat": peak_cell.center_latitude, "lon": peak_cell.center_longitude}

        official = [s for s in cluster["signals"] if s["kind"] == "official_alert"]
        severities = {(s.get("severity") or "").strip().lower() for s in official}
        anomaly_scores = [
            (state_row.raw_inputs or {}).get("anomaly_score")
            for state_row, _cell in members]
        anomaly_scores = [value for value in anomaly_scores if value is not None]

        # A city-wide advisory has no cells and therefore no computed risk, so
        # banding it by score would label an Extreme IMD warning "normal". It
        # keeps the authority's own severity instead -- which is the honest
        # thing to show, since the authority is the only one making a claim
        # about it.
        severity = config.band_for(peak_risk)[0]
        if cluster.get("citywide"):
            severity = _severity_from_cap(severities)

        scored.append(dict(cluster, **{
            "peak_risk": round(peak_risk, 1),
            "mean_risk": round(mean_risk, 1),
            "severity": severity,
            "headline_h3": peak_h3,
            "centre": centre,
            "anomaly": max(anomaly_scores) if anomaly_scores else None,
            "official_severities": sorted(s for s in severities if s),
            "evidence": _evidence_for(cluster, members),
        }))

    return {"scored": scored}


def _severity_from_cap(severities):
    """CAP severity -> the twin's own band vocabulary, for display only."""
    if "extreme" in severities:
        return "critical"
    if "severe" in severities:
        return "warning"
    if "moderate" in severities:
        return "watch"
    return "normal"


def _evidence_for(cluster, members):
    """Every number that justified this cluster, kept for the flag row (C3)."""
    rain_values, aqi_values, stall_rates = [], [], []
    for state_row, _cell in members:
        raw = state_row.raw_inputs or {}
        for key in ("rain_now_mm_1h", "rain_forecast_mm"):
            if raw.get(key) is not None:
                rain_values.append(raw[key])
        if raw.get("station_aqi") is not None:
            aqi_values.append(raw["station_aqi"])
        elif raw.get("us_aqi") is not None:
            aqi_values.append(raw["us_aqi"])
        if raw.get("transit_stall_rate") is not None:
            stall_rates.append(raw["transit_stall_rate"])

    exceedance = None
    for state_row, _cell in members:
        candidate = (state_row.raw_inputs or {}).get("rain_exceedance")
        if candidate:
            exceedance = candidate
            break

    return {
        "cell_count": len(cluster["cells"]),
        "max_rain_mm": max(rain_values) if rain_values else None,
        "max_aqi": max(aqi_values) if aqi_values else None,
        "max_stall_rate": max(stall_rates) if stall_rates else None,
        "rain_exceedance": exceedance,
        "official_alerts": [{
            "sender": s.get("sender"), "event": s.get("event"),
            "severity": s.get("severity"), "certainty": s.get("certainty"),
            "url": s.get("url"), "instruction": s.get("instruction"),
        } for s in cluster["signals"] if s["kind"] == "official_alert"],
        "signal_kinds": sorted({s["kind"] for s in cluster["signals"]}),
    }


# --------------------------------------------------------------------------
# 5. threshold -- what is worth a human's attention
# --------------------------------------------------------------------------

def threshold(state):
    """Keep only clusters worth interrupting someone for.

    A quiet city must reach the end of this node with nothing, and must do so
    **before** any LLM node runs: most polls find nothing, and burning tokens
    to write briefs nobody needs is how an agent becomes too expensive to keep
    switched on.
    """
    scored = state.get("scored") or []
    flagged = []

    for cluster in scored:
        if cluster["peak_risk"] >= config.FLAG_THRESHOLD:
            cluster["flag_reason"] = "risk_threshold"
            flagged.append(cluster)
            continue
        if any(sev in ALWAYS_FLAG_SEVERITIES for sev in cluster["official_severities"]):
            cluster["flag_reason"] = "official_severity"
            flagged.append(cluster)

    flagged.sort(key=lambda cluster: cluster["peak_risk"], reverse=True)
    return {"flagged": flagged}


# --------------------------------------------------------------------------
# 6. retrieve -- precedent and procedure (LLM job 3, via RAG)
# --------------------------------------------------------------------------

def retrieve(state):
    """Attach relevant SOP/procedure passages to each flagged cluster."""
    flagged = state.get("flagged") or []
    if not flagged:
        return {"context": {}}

    context = {}
    for cluster in flagged:
        query = "%s response procedure %s" % (
            cluster.get("hazard_type") or "hazard", state["city"].display_name)
        try:
            context[cluster["key"]] = rag.search(query, top_k=config.RAG_TOP_K)
        except Exception:  # noqa: BLE001 - retrieval is an enhancement, never a gate
            log.exception("RAG lookup failed for %s", cluster["key"])
            context[cluster["key"]] = []
    return {"context": context}


# --------------------------------------------------------------------------
# 7. draft_brief -- the card an analyst reads (LLM job 4)
# --------------------------------------------------------------------------

def draft_brief(state):
    """Write each flag's brief, with citations.

    The deterministic brief is written first, from the evidence, and is what
    ships if the model is absent or fails. When a model is available it is
    given those same sentences and asked to make them readable -- it is never
    given free rein to characterise the situation, and every number in the
    output originates in the evidence dict.
    """
    flagged = state.get("flagged") or []
    context = state.get("context") or {}
    briefs = []

    for cluster in flagged:
        citations = _citations_for(cluster, context.get(cluster["key"]) or [])
        facts = _fact_lines(cluster, state["city"])
        title = _title_for(cluster, state["city"])
        brief = "\n".join("- " + line for line in facts)

        if llm.available():
            polished = llm.complete(
                system=("You write short situation briefs for a city emergency "
                        "analyst. Use ONLY the facts given. Never add numbers, "
                        "place names, causes or reassurance that are not in them. "
                        "Three sentences maximum. Plain English."),
                user=("Facts:\n%s\n\nWrite the brief. End with what the analyst "
                      "should consider doing, phrased as a suggestion." % brief),
                fallback=None)
            if polished:
                brief = polished.strip()

        briefs.append(dict(cluster, **{
            "title": title,
            "brief_md": brief,
            "citations": citations,
            "agent_mode": llm.mode(),
        }))

    return {"briefs": briefs}


def _title_for(cluster, city):
    hazard = (cluster.get("hazard_type") or "hazard").replace("_", " ")
    official = cluster["evidence"].get("official_alerts") or []
    if official and official[0].get("event"):
        return official[0]["event"][:120]
    if cluster.get("citywide"):
        return "%s advisory covering %s" % (hazard.capitalize(), city.display_name)
    return "%s risk across %d cells in %s" % (
        hazard.capitalize(), len(cluster["cells"]), city.display_name)


def _fact_lines(cluster, city):
    """The checkable sentences a brief is built from."""
    evidence = cluster["evidence"]
    if cluster.get("citywide"):
        lines = [
            "Authority-issued advisory covering %s. The issuing authority "
            "published no map polygon for it, so no specific area can be shown "
            "-- the geography below is theirs, not the twin's."
            % city.display_name,
        ]
    else:
        lines = [
            "%s-level risk: peak score %.0f, average %.0f across %d cells in %s."
            % (cluster["severity"], cluster["peak_risk"], cluster["mean_risk"],
               len(cluster["cells"]), city.display_name),
        ]

    lines.extend(anomaly.explain({
        "rain": {"value": evidence.get("max_rain_mm"),
                 "exceedance": evidence.get("rain_exceedance"),
                 "baseline_missing": not evidence.get("rain_exceedance")},
        "alerts": [{
            "sender": alert.get("sender"), "severity": alert.get("severity"),
            "event": alert.get("event"),
        } for alert in evidence.get("official_alerts") or []],
        "transit": ({"score": 1, "stalled": 0, "vehicles": 0,
                     "stall_rate": evidence["max_stall_rate"]}
                    if evidence.get("max_stall_rate") else None),
        "air": {"aqi": evidence.get("max_aqi")} if evidence.get("max_aqi") else None,
    }))

    if cluster.get("flag_reason") == "official_severity":
        lines.append("Raised because an authority issued an Extreme-severity alert, "
                     "even though the computed score is below the flag threshold.")
    return lines


def _citations_for(cluster, retrieved):
    """Sources an analyst can open. A brief that cites nothing is uncheckable."""
    citations = []
    for alert in cluster["evidence"].get("official_alerts") or []:
        citations.append({
            "source": alert.get("sender") or "Official alert",
            "title": alert.get("event") or "CAP alert",
            "url": alert.get("url"),
            "kind": "official_alert",
        })
    for passage in retrieved:
        citations.append({
            "source": passage.get("source") or "Document",
            "title": passage.get("title") or passage.get("source"),
            "url": passage.get("url"),
            "excerpt": (passage.get("text") or "")[:240],
            "kind": "document",
        })
    return citations


# --------------------------------------------------------------------------
# 8. persist -- write flags for the analyst queue
# --------------------------------------------------------------------------

def persist(state):
    """Upsert flags and their cells. Everything lands as `pending`.

    The human gate is this status, not a code path: nothing the agent
    produces reaches the public until an analyst acts on it, which mirrors
    the host app's existing rule that a citizen report is invisible until
    approved. The write itself lives in flagstore, shared with the forecast
    agent so the gate cannot drift between them.
    """
    briefs = state.get("briefs") or []
    for brief in briefs:
        brief["confidence"] = _confidence_for(brief)
    return flagstore.upsert_flags(state["db"], state["city"], briefs)


def _confidence_for(brief):
    """How much corroboration this flag has, 0..1.

    Independent sources agreeing is the only confidence signal worth
    reporting: one hot cell is a reading, while a hot cell plus an IMD warning
    plus a stalled bus fleet is an event.
    """
    kinds = set(brief["evidence"].get("signal_kinds") or [])
    base = 0.35 + 0.2 * len(kinds)
    if brief["evidence"].get("official_alerts"):
        base += 0.15
    return round(min(1.0, base), 2)


def _dumps(payload):
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)
