"""The five sub-scores + composite (DIGITAL_TWIN_README.md section 5.1).

Pure functions only: every function here takes already-resolved numbers (or
None for "genuinely unmeasured, no neutral fallback exists") and returns a
number (or None). Nothing in this module touches the network, the database,
or a Flask request -- that is deliberate, so every boundary value from
section 5.1 can be pinned with a plain unit test (Phase 3 checkpoint) with no
ingest machinery involved.

Two things every caller must get right, because they are the two mistakes
the original spec review caught:

1. ``None`` means *unmeasured*. ``0.0`` means *measured as nothing*. A cell
   with zero approved reports passes ``0.0`` to :func:`incident_score`'s
   caller-side aggregation, never ``None``. Only a source with no data at
   all (elevation seed never ran, no 2-year discharge baseline yet) may pass
   ``None`` into these functions.
2. ``risk_score = hazard * vulnerability``, not a five-term weighted sum. See
   :func:`composite`.
"""

import math
from datetime import datetime, timezone

from . import config
from . import geo


def _weighted_renormalize(terms):
    """`terms`: {name: (value_or_None, weight)}.

    Drops any None-valued term and renormalises the rest to sum to 1.0.
    Returns (score_or_None, [names_dropped]). Score is None only when every
    term is None -- callers treat that as "this whole sub-score is
    unmeasured", not zero.
    """
    present = {k: (v, w) for k, (v, w) in terms.items() if v is not None}
    dropped = [k for k in terms if k not in present]
    if not present:
        return None, dropped
    weight_sum = sum(w for _, w in present.values())
    if weight_sum <= 0:
        return None, dropped
    score = sum(v * w for v, w in present.values()) / weight_sum
    return score, dropped


# --------------------------------------------------------------------------
# 1. HYDRO PRESSURE
# --------------------------------------------------------------------------

def hydro_score(rain_now_mm_1h, rain_forecast_mm, discharge_m3s,
                 discharge_2yr_return_m3s, horizon_hours=0):
    """`rain_forecast_mm` is the sum over the window matching `horizon_hours`
    (next 3h at horizon 0/3, next 6h at horizon 6, next 24h at horizon 24 --
    engine.py picks the right window before calling this).

    `discharge_2yr_return_m3s` is a seed-time-derived p95-of-archive proxy
    (see twin.ingest.open_meteo.OpenMeteoFloodReturnPeriodAdapter), not a
    true hydrological return period -- Open-Meteo exposes no such endpoint.
    """
    rain_now = None if rain_now_mm_1h is None else min(100.0, rain_now_mm_1h * 4.0)
    rain_forecast = None if rain_forecast_mm is None else min(100.0, rain_forecast_mm * 2.0)

    discharge = None
    if discharge_m3s is not None and discharge_2yr_return_m3s:
        discharge = min(100.0, (discharge_m3s / discharge_2yr_return_m3s) * 60.0)

    w_now, w_forecast, w_discharge = config.HYDRO_WEIGHTS[horizon_hours]
    score, dropped = _weighted_renormalize({
        "rain_now": (rain_now, w_now),
        "rain_forecast": (rain_forecast, w_forecast),
        "discharge": (discharge, w_discharge),
    })
    return score, dropped


# --------------------------------------------------------------------------
# 2. INCIDENT PRESSURE
# --------------------------------------------------------------------------

def report_contribution(priority, confidence, timestamp, in_this_cell, now=None):
    """The weighted contribution of one approved report to one cell's score."""
    now = now or datetime.now(timezone.utc)
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    if timestamp is None:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    sev = config.INCIDENT_SEVERITY.get(priority, config.INCIDENT_SEVERITY["low"])
    hours_since = max(0.0, (now - timestamp).total_seconds() / 3600.0)
    rec = math.exp(-hours_since / config.INCIDENT_DECAY_TAU_HOURS)
    conf = config.INCIDENT_DEFAULT_CONFIDENCE if confidence is None else confidence
    w = 1.0 if in_this_cell else config.INCIDENT_NEIGHBOUR_WEIGHT
    return sev * rec * conf * w


def incident_score(reports, now=None):
    """`reports`: iterable of (report_dict, in_this_cell: bool).

    report_dict needs `priority`, `confidence`, `timestamp`, `id`. Always
    returns a float (0.0 for an empty/failed-sync-free cell), never None --
    see the module docstring's rule 1. Also returns (count, top_report_id).
    """
    now = now or datetime.now(timezone.utc)
    total = 0.0
    count = 0
    top_id, top_contribution = None, -1.0

    for report, in_cell in reports:
        contribution = report_contribution(
            report.get("priority"), report.get("confidence"),
            report.get("timestamp"), in_cell, now=now,
        )
        total += contribution
        count += 1
        if contribution > top_contribution:
            top_contribution, top_id = contribution, report.get("id")

    return min(100.0, total), count, top_id


# --------------------------------------------------------------------------
# 3. TERRAIN EXPOSURE (horizon-invariant, cached on TwinCell)
# --------------------------------------------------------------------------

def terrain_score(elevation_m, city_elevation_population, dist_to_water_m, drain_length_m):
    """None when elevation is unknown -- there is no neutral elevation."""
    if elevation_m is None:
        return None

    elev_pct = geo.percentile_rank(elevation_m, city_elevation_population)
    low_lying = (1.0 - elev_pct) * 100.0

    if dist_to_water_m is None:
        water_prox = 0.0  # unknown distance: not flagged as obviously close
    elif dist_to_water_m < 200:
        water_prox = 100.0
    elif dist_to_water_m < 500:
        water_prox = 60.0
    elif dist_to_water_m < 1000:
        water_prox = 25.0
    else:
        water_prox = 0.0

    drain_divisor = config.DRAIN_SATURATION_M / 100.0  # 2000m saturates -> /20
    drain_gap = 100.0 - min(100.0, (drain_length_m or 0.0) / drain_divisor)

    weights = config.TERRAIN_WEIGHTS
    return (weights["low_lying"] * low_lying
            + weights["water_prox"] * water_prox
            + weights["drain_gap"] * drain_gap)


# --------------------------------------------------------------------------
# 4. INFRA CRITICALITY (horizon-invariant, cached on TwinCell)
# --------------------------------------------------------------------------

def infra_score(criticality_sum):
    """`criticality_sum` is TwinCell.infra_criticality_cached; defaults to
    0.0 (a real "no critical assets here" measurement), never None once the
    cell exists -- so this practically always returns a number.
    """
    if criticality_sum is None:
        return None
    return min(100.0, criticality_sum * config.INFRA_CRITICALITY_GAIN)


# --------------------------------------------------------------------------
# 5. ENV STRESS
# --------------------------------------------------------------------------

def env_score(us_aqi, apparent_temp_c):
    aqi_s = None if us_aqi is None else min(100.0, us_aqi / 3.0)
    heat_s = None if apparent_temp_c is None else max(0.0, min(100.0, (apparent_temp_c - 30.0) * 5.0))

    score, dropped = _weighted_renormalize({
        "aqi": (aqi_s, config.ENV_WEIGHTS["aqi"]),
        "heat": (heat_s, config.ENV_WEIGHTS["heat"]),
    })
    return score, dropped


# --------------------------------------------------------------------------
# COMPOSITE: hazard * vulnerability (corrected section 5.1)
# --------------------------------------------------------------------------

def composite(hydro, incident, env, terrain, infra):
    """Returns a dict with everything TwinCellState needs to store.

    hazard   = renormalised(0.55*hydro + 0.30*incident + 0.15*env) -- what is
               HAPPENING; 0 when hydro/incident/env are all calm.
    vulnerability = 1 + 0.6*(0.6*terrain + 0.4*infra)/100 -- what is AT
               STAKE; ranges 1.0..1.6 and never reduces risk.
    risk_score = clamp(0, 100, hazard * vulnerability)

    `degraded_inputs` merges drops from both halves so C3 stays intact: every
    number that fed the score, and every one that didn't, is visible.
    """
    hazard, hazard_dropped = _weighted_renormalize({
        "hydro": (hydro, config.HAZARD_WEIGHTS["hydro"]),
        "incident": (incident, config.HAZARD_WEIGHTS["incident"]),
        "env": (env, config.HAZARD_WEIGHTS["env"]),
    })

    # Vulnerability has no "all missing" failure mode worth propagating: a
    # missing terrain/infra term contributes its neutral midpoint rather than
    # being dropped, because there is nothing else for the multiplier's
    # weight to renormalise onto (see the README's corrected renorm rule).
    terrain_component = 50.0 if terrain is None else terrain
    infra_component = 0.0 if infra is None else infra
    vulnerability_dropped = []
    if terrain is None:
        vulnerability_dropped.append("terrain")
    if infra is None:
        vulnerability_dropped.append("infra")

    vw = config.VULNERABILITY_WEIGHTS
    vulnerability_raw = (vw["terrain"] * terrain_component + vw["infra"] * infra_component) / 100.0
    vulnerability = 1.0 + config.VULNERABILITY_GAIN * vulnerability_raw

    if hazard is None:
        risk_score = 0.0
    else:
        risk_score = max(0.0, min(100.0, hazard * vulnerability))

    status, colour, opacity, height_factor = config.band_for(risk_score)

    return {
        "risk_score": risk_score,
        "status": status,
        "colour": colour,
        "opacity": opacity,
        "height_m": risk_score * height_factor,
        "hazard_score": hazard,
        "vulnerability_multiplier": vulnerability,
        "degraded_inputs": sorted(set(hazard_dropped) | set(vulnerability_dropped)),
    }
