"""compute_state orchestrator (section 5, Phase 3).

This is the one place that turns "the latest ingested numbers" into
"TwinCellState rows for four horizons, for every cell in a city". Everything
mathematical lives in twin/scoring.py (pure, unit-tested in isolation);
everything here is plumbing: fan out to the ingest adapters, IDW-interpolate
the weather lattice onto cells, bucket approved reports into cells and
k-ring-1 neighbours, and bulk-upsert the results.

Two performance rules from section 5.4 / 15, enforced structurally rather
than just documented:

- **One weather/AQI call per city, not per cell.** :func:`_fetch_weather` and
  :func:`_fetch_airquality` call their adapters exactly once per city per
  compute run, against a lattice built by ``twin.geo.build_lattice``.
- **Bulk upsert, not row-at-a-time.** :func:`_write_states` does one SELECT
  of existing `(cell_id, horizon_hours) -> id` and one
  `bulk_insert_mappings` / `bulk_update_mappings` pair, because ~2,000 cells
  x 4 horizons = ~8,000 rows every 5 minutes will not fit the <12s budget
  any other way on SQLite (section 15).
"""

import logging
import time

from . import anomaly
from . import config
from . import geo
from . import models as m
from . import scoring
from .ingest.internal_reports import InternalReportsAdapter, cells_for_report
from .ingest.open_meteo import (
    OpenMeteoAirQualityAdapter, OpenMeteoFloodAdapter, OpenMeteoForecastAdapter,
)

log = logging.getLogger("twin.engine")

_FORECAST_WINDOW_KEY = {0: "rain_forecast_mm_3h", 3: "rain_forecast_mm_3h",
                        6: "rain_forecast_mm_6h", 24: "rain_forecast_mm_24h"}

#: Which baseline an anomaly at each horizon is judged against. "Now" is
#: compared to the 1-hour distribution and the forecast windows to their own,
#: because 40 mm in an hour and 40 mm over a day are not the same event.
_ANOMALY_METRIC_KEY = {0: "rain_1h", 3: "rain_3h", 6: "rain_3h", 24: "rain_24h"}


def compute_state(db, city, horizons=config.HORIZONS, lattice_spacing_km=5.0):
    """Compute and upsert TwinCellState for every cell of `city`, all horizons.

    Returns a summary dict (cell/row counts, per-source snapshot statuses,
    elapsed seconds, and the set of h3 indexes whose status band changed --
    the SSE `changed_cells` payload, section 7/8.6).
    """
    t0 = time.monotonic()
    cells = db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
    if not cells:
        log.warning("compute_state(%s): no cells -- run scripts/seed_twin.py first", city.slug)
        return {"city": city.slug, "cells": 0, "elapsed_s": 0.0}

    bbox = (city.bbox_min_lon, city.bbox_min_lat, city.bbox_max_lon, city.bbox_max_lat)
    lattice = geo.build_lattice(bbox, spacing_km=lattice_spacing_km)

    weather_by_point, weather_status = _fetch_weather(db, city, lattice)
    aq_by_point, aq_status = _fetch_airquality(db, city, lattice)
    discharge, flood_status = _fetch_flood(db, city)
    reports_by_cell, incident_status = _fetch_incidents(db, city, cells)

    # The live-data extension. Each of these is read from what has already
    # been persisted rather than fetched here, so the score and the map always
    # describe the same instant -- a score citing vehicles the operator cannot
    # see on screen is unauditable.
    alerts_by_cell = _fetch_alert_cells(db, city)
    disruption_by_cell = _fetch_transit_disruption(db, city)
    baselines_by_cell = _load_baselines(db, cells)
    station_aqi_by_cell = _fetch_station_aqi(db, city, cells)

    degraded_sources = {
        key for key, status in (
            ("open_meteo_forecast", weather_status),
            ("open_meteo_airquality", aq_status),
            ("open_meteo_flood", flood_status),
            ("internal_reports", incident_status),
        )
        if status != "ok"
    }

    elevation_population = [c.elevation_m for c in cells if c.elevation_m is not None]

    previous_scores = _load_previous_scores(db, cells, horizons)
    rows_by_key = {}  # (cell_id, horizon) -> dict of column values

    for cell in cells:
        weather = _idw_for_cell(cell, lattice, weather_by_point)
        air = _idw_for_cell(cell, lattice, aq_by_point)
        reports = reports_by_cell.get(cell.h3_index, [])

        for horizon in horizons:
            row = _score_one_cell_horizon(
                cell, horizon, weather, air, discharge, reports,
                elevation_population, degraded_sources,
                alerts=alerts_by_cell.get(cell.h3_index) or [],
                disruption_stats=disruption_by_cell.get(cell.h3_index),
                baselines=baselines_by_cell.get(cell.id) or {},
                station_aqi=station_aqi_by_cell.get(cell.h3_index),
            )
            rows_by_key[(cell.id, horizon)] = row

    changed_cells = _write_states(db, rows_by_key, previous_scores)

    elapsed = time.monotonic() - t0
    log.info("compute_state(%s): %d cells x %d horizons in %.2fs",
             city.slug, len(cells), len(horizons), elapsed)

    return {
        "city": city.slug,
        "cells": len(cells),
        "horizons": list(horizons),
        "elapsed_s": round(elapsed, 2),
        "degraded_sources": sorted(degraded_sources),
        "changed_cells": changed_cells,
    }


# --------------------------------------------------------------------------
# Per-cell, per-horizon scoring
# --------------------------------------------------------------------------

def _score_one_cell_horizon(cell, horizon, weather, air, discharge, reports,
                            elevation_population, degraded_sources,
                            alerts=(), disruption_stats=None, baselines=None,
                            station_aqi=None):
    weather = weather or {}
    air = air or {}
    baselines = baselines or {}

    rain_now = weather.get("rain_now_mm_1h")
    rain_forecast = weather.get(_FORECAST_WINDOW_KEY[horizon])
    discharge_now, discharge_2yr = discharge

    hydro, hydro_dropped = scoring.hydro_score(
        rain_now, rain_forecast, discharge_now, discharge_2yr, horizon_hours=horizon)

    incident, incident_count, top_report_id = scoring.incident_score(reports)

    terrain = cell.terrain_score_cached
    infra = scoring.infra_score(cell.infra_criticality_cached)

    # A station's measured AQI beats a 5 km reanalysis grid cell when one is
    # in range: the thing that spikes an Indian city's air is local, and a
    # model that smooths over 5 km cannot see it. The modelled value stays as
    # the fallback so a city with no nearby station still scores.
    measured_aqi = (station_aqi or {}).get("aqi")
    env, env_dropped = scoring.env_score(
        measured_aqi if measured_aqi is not None else air.get("us_aqi"),
        weather.get("apparent_temp_c"))

    alert_pressure = anomaly.alert_pressure(alerts) if alerts else None
    disruption = anomaly.transit_disruption_score(disruption_stats)

    # Anomaly is judged on the rain window this horizon actually cares about,
    # against the matching baseline metric.
    anomaly_metric = _ANOMALY_METRIC_KEY[horizon]
    anomaly_score = anomaly.anomaly_score(
        rain_forecast if horizon else rain_now, baselines.get(anomaly_metric))
    rain_exceedance = anomaly.exceedance(
        rain_forecast if horizon else rain_now, baselines.get(anomaly_metric))

    result = scoring.composite(hydro=hydro, incident=incident, env=env,
                               terrain=terrain, infra=infra,
                               alert=alert_pressure, disruption=disruption,
                               anomaly=anomaly_score)

    # Merge structural drops (renormalisation happened) with source-level
    # degradation (the adapter itself failed this run) -- an official needs
    # to know both "this number was interpolated from a stale forecast" and
    # "this term was dropped entirely", and they are not the same thing.
    degraded = set(result["degraded_inputs"])
    if "open_meteo_forecast" in degraded_sources:
        degraded.add("hydro")
        degraded.add("env")
    if "open_meteo_flood" in degraded_sources:
        degraded.add("hydro")
    if "open_meteo_airquality" in degraded_sources:
        degraded.add("env")
    if "internal_reports" in degraded_sources:
        degraded.add("incident")

    raw_inputs = {
        "rain_now_mm_1h": rain_now,
        "rain_forecast_mm": rain_forecast,
        "river_discharge_m3s": discharge_now,
        "river_discharge_2yr_return_m3s": discharge_2yr,
        "us_aqi": air.get("us_aqi"),
        "station_aqi": measured_aqi,
        "station_name": (station_aqi or {}).get("name"),
        "apparent_temp_c": weather.get("apparent_temp_c"),
        "elevation_m": cell.elevation_m,
        "dist_to_water_m": cell.dist_to_water_m,
        "drain_length_m": cell.drain_length_m,
        "infra_criticality_cached": cell.infra_criticality_cached,
        "incident_count_raw": incident_count,
        # The live-data extension's own working, kept beside the rest so C3
        # ("show your working") still holds for the new terms.
        "official_alert_count": len(alerts or ()),
        "official_alert_pressure": alert_pressure,
        "transit_stall_rate": (disruption_stats or {}).get("stall_rate"),
        "transit_vehicles": (disruption_stats or {}).get("vehicles"),
        "anomaly_score": anomaly_score,
        "anomaly_multiplier": result.get("anomaly_multiplier"),
        "rain_exceedance": rain_exceedance,
    }

    return {
        "cell_id": cell.id,
        "horizon_hours": horizon,
        "risk_score": result["risk_score"],
        "status": result["status"],
        "hazard_score": result["hazard_score"],
        "vulnerability_multiplier": result["vulnerability_multiplier"],
        "hydro_score": hydro,
        "incident_score": incident,
        "terrain_score": terrain,
        "infra_score": infra,
        "env_score": env,
        "raw_inputs": raw_inputs,
        "degraded_inputs": sorted(degraded),
        "incident_count": incident_count,
        "top_incident_report_id": top_report_id,
        "computed_at": m.utcnow(),
    }


# --------------------------------------------------------------------------
# Ingest fan-out (one call per city, never per cell -- section 5.4/15)
# --------------------------------------------------------------------------

def _fetch_weather(db, city, lattice):
    adapter = OpenMeteoForecastAdapter()
    data, snapshot = adapter.run(db, city_id=city.id, points=lattice)
    by_point = {(entry["lat"], entry["lon"]): entry for entry in (data or [])}
    return by_point, (snapshot.status if snapshot else "failed")


def _fetch_airquality(db, city, lattice):
    adapter = OpenMeteoAirQualityAdapter()
    data, snapshot = adapter.run(db, city_id=city.id, points=lattice)
    by_point = {(entry["lat"], entry["lon"]): entry for entry in (data or [])}
    return by_point, (snapshot.status if snapshot else "failed")


def _fetch_flood(db, city):
    if city.basin_outlet_lat is None or city.basin_outlet_lon is None:
        return (None, city.discharge_2yr_return), "unknown"
    adapter = OpenMeteoFloodAdapter()
    data, snapshot = adapter.run(
        db, city_id=city.id, lat=city.basin_outlet_lat, lon=city.basin_outlet_lon)
    discharge_now = (data or {}).get("river_discharge_now")
    return (discharge_now, city.discharge_2yr_return), (snapshot.status if snapshot else "failed")


def _fetch_incidents(db, city, cells):
    """{cell.h3_index: [(report_dict, in_this_cell_bool), ...]} for every
    approved report in range, exploded across its home cell and k-ring-1
    neighbours (section 5.1 incident pressure spillover).
    """
    adapter = InternalReportsAdapter()
    data, snapshot = adapter.run(db, city_id=city.id, since_hours=72)
    status = snapshot.status if snapshot else "failed"

    by_cell = {}
    if not data:
        return by_cell, status

    valid_h3 = {c.h3_index for c in cells}
    for report in data:
        lat, lon = report.get("lat"), report.get("lon")
        if lat is None or lon is None:
            continue
        home_cell, neighbours = cells_for_report(lat, lon)
        if home_cell in valid_h3:
            by_cell.setdefault(home_cell, []).append((report, True))
        for neighbour in neighbours:
            if neighbour in valid_h3:
                by_cell.setdefault(neighbour, []).append((report, False))

    return by_cell, status


def _fetch_alert_cells(db, city):
    """{h3_index: [alert_row, ...]} for alerts in force with a real footprint.

    District-scoped advisories deliberately do not appear here. They are real
    and an operator sees them in the alert panel, but attributing a state-wide
    IMD advisory to all 940 cells would push the whole city up a band on a
    routine monsoon afternoon -- and a map that is amber every day is a map
    nobody looks at.
    """
    try:
        from . import alerts as alerts_module

        return alerts_module.alert_cells_by_h3(db, city)
    except Exception:  # noqa: BLE001 - C1: a feed must never break compute
        log.exception("alert lookup failed for %s; scoring without alerts", city.slug)
        return {}


def _fetch_transit_disruption(db, city):
    try:
        from . import live as live_module

        return live_module.transit_disruption(db, city)
    except Exception:  # noqa: BLE001
        log.exception("transit lookup failed for %s; scoring without it", city.slug)
        return {}


def _fetch_station_aqi(db, city, cells):
    """{h3_index: {aqi, name, ...}} from measured stations, spread to neighbours.

    A city has tens of stations and ~900 cells, so a station is attributed to
    its own cell and its immediate ring rather than interpolated across the
    whole city: an instrument describes its street, and claiming it describes
    somewhere 4 km away is exactly the overreach the modelled layer already
    makes. Cells with no station in range keep the modelled value.
    """
    try:
        import h3

        from . import models as models_module

        rows = (db.session.query(models_module.TwinObservation)
                .filter(models_module.TwinObservation.city_id == city.id,
                        models_module.TwinObservation.kind == "air_quality")
                .all())
    except Exception:  # noqa: BLE001
        log.exception("station lookup failed for %s", city.slug)
        return {}

    by_cell = {}
    for row in rows:
        if row.value is None or not row.h3_index:
            continue
        # A station that stopped reporting must not keep scoring the city.
        if row.status == "stale":
            continue

        entry = {"aqi": row.value, "name": row.name, "source": row.source_key,
                 "unit": row.unit, "distance_rings": 0}
        by_cell.setdefault(row.h3_index, entry)
        try:
            for neighbour in h3.grid_disk(row.h3_index, 1):
                if neighbour == row.h3_index:
                    continue
                by_cell.setdefault(neighbour, dict(entry, distance_rings=1))
        except Exception:  # noqa: BLE001 - malformed index
            continue

    valid = {cell.h3_index for cell in cells}
    return {k: v for k, v in by_cell.items() if k in valid}


def _load_baselines(db, cells):
    """{cell_id: {metric: baseline_row}} for the current calendar month.

    Scoped to this month because that is what "normal" means operationally:
    60 mm in three hours is routine in a Bengaluru July and remarkable in
    February, and an all-year baseline would call the monsoon an anomaly every
    single day of it.
    """
    try:
        from . import models as models_module

        month = m.utcnow().month
        cell_ids = [cell.id for cell in cells]
        rows = (db.session.query(models_module.TwinBaseline)
                .filter(models_module.TwinBaseline.cell_id.in_(cell_ids),
                        models_module.TwinBaseline.month == month)
                .all())
    except Exception:  # noqa: BLE001
        log.exception("baseline lookup failed; scoring without anomaly detection")
        return {}

    by_cell = {}
    for row in rows:
        by_cell.setdefault(row.cell_id, {})[row.metric] = row
    return by_cell


def _idw_for_cell(cell, lattice, values_by_point):
    """One IDW-interpolated reading per numeric field in `values_by_point`."""
    if not values_by_point:
        return {}

    sample_entry = next(iter(values_by_point.values()))
    numeric_fields = [k for k, v in sample_entry.items() if isinstance(v, (int, float)) or v is None]

    result = {}
    for field in numeric_fields:
        samples = [
            (lat, lon, values_by_point[(lat, lon)].get(field))
            for (lat, lon) in lattice
            if (lat, lon) in values_by_point
        ]
        result[field] = geo.idw_interpolate(cell.center_latitude, cell.center_longitude, samples)
    return result


# --------------------------------------------------------------------------
# Bulk upsert (section 15: never row-at-a-time at this row count)
# --------------------------------------------------------------------------

def _load_previous_scores(db, cells, horizons):
    """{(cell_id, horizon): (status, risk_score)} before this run overwrites
    it -- the only source for the SSE `changed_cells` diff (section 15: "the
    in-place upsert destroys" the previous value if you don't grab it first).
    """
    cell_ids = [c.id for c in cells]
    rows = (
        db.session.query(m.TwinCellState.cell_id, m.TwinCellState.horizon_hours,
                         m.TwinCellState.status, m.TwinCellState.risk_score)
        .filter(m.TwinCellState.cell_id.in_(cell_ids), m.TwinCellState.horizon_hours.in_(horizons))
        .all()
    )
    return {(cell_id, horizon): (status, risk_score) for cell_id, horizon, status, risk_score in rows}


def _write_states(db, rows_by_key, previous_scores):
    """One bulk SELECT + one bulk_insert_mappings/bulk_update_mappings pair.

    Returns the list of h3_index values whose status band changed, for the
    SSE broadcaster.
    """
    cell_ids = {cell_id for cell_id, _horizon in rows_by_key}
    existing = (
        db.session.query(m.TwinCellState.id, m.TwinCellState.cell_id, m.TwinCellState.horizon_hours)
        .filter(m.TwinCellState.cell_id.in_(cell_ids))
        .all()
    )
    existing_ids = {(cell_id, horizon): state_id for state_id, cell_id, horizon in existing}

    to_insert, to_update = [], []
    changed_cell_ids = set()

    for (cell_id, horizon), row in rows_by_key.items():
        existing_id = existing_ids.get((cell_id, horizon))
        prev = previous_scores.get((cell_id, horizon))
        if prev is None or prev[0] != row["status"]:
            changed_cell_ids.add(cell_id)

        if existing_id is not None:
            row = dict(row)
            row["id"] = existing_id
            to_update.append(row)
        else:
            to_insert.append(row)

    if to_insert:
        db.session.bulk_insert_mappings(m.TwinCellState, to_insert)
    if to_update:
        db.session.bulk_update_mappings(m.TwinCellState, to_update)
    db.session.commit()

    if changed_cell_ids:
        changed_h3 = [
            h3_index for (h3_index,) in
            db.session.query(m.TwinCell.h3_index).filter(m.TwinCell.id.in_(changed_cell_ids)).all()
        ]
    else:
        changed_h3 = []
    return changed_h3
