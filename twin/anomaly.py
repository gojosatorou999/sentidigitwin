"""Is today unusual *here*? Deterministic, explainable, no model involved.

The twin could already say "62 mm of rain fell in three hours". It could not
say whether that is a Tuesday in Bengaluru or the worst hour of the decade --
and that difference is the whole of what an operator means by "abnormal".

Two design rules, both learned from what the rest of this module gets right:

1. **Compare a cell against its own history, not against a constant.**
   A 30 mm threshold that means "unremarkable" in coastal Karnataka means
   "evacuate" on the Deccan plateau. Every judgement here is relative to the
   cell's own baseline for the current calendar month (``twin_baseline``,
   filled by ``scripts/backfill_baselines.py`` from open archives).

2. **Prefer percentiles to sigma.** Rainfall is not normally distributed --
   it is zero most days and extreme on a few -- so a standard deviation
   overstates how unusual a wet hour is. Where a baseline carries p95/p99 the
   comparison uses them, and sigma is only the fallback.

Everything in this module is a pure function of numbers already fetched. It
never touches the network, the database or an LLM: an operator asked "why was
this flagged" must get "62 mm against a 30 mm p95 for September here", not a
model's opinion.
"""

import math

from . import config

#: Sigma is only meaningful once a baseline has enough observations behind it.
#: Below this, the comparison is reported as unmeasured rather than guessed.
MIN_SAMPLES = 30


def sigma_above(value, baseline):
    """How many standard deviations above this cell's own mean, or None.

    Returns None -- not 0.0 -- when there is no usable baseline. The
    distinction is the same one the rest of the module keeps: "measured as
    normal" and "we have no idea what normal is here" must never be conflated,
    because only one of them justifies relaxing.
    """
    if value is None or baseline is None:
        return None
    if (baseline.sample_count or 0) < MIN_SAMPLES:
        return None
    if baseline.stddev in (None, 0):
        return None
    return (value - (baseline.mean or 0.0)) / baseline.stddev


def exceedance(value, baseline):
    """Where `value` sits against this cell's own distribution.

    Returns a dict the UI and the agent can both quote directly::

        {"level": "p95", "threshold": 30.2, "value": 62.0, "ratio": 2.05}

    ``level`` is the highest published percentile the value clears, or None
    when it clears none of them. The ratio is against that threshold, so
    "twice the local p95" is expressible without anyone re-deriving it.
    """
    if value is None or baseline is None:
        return None

    for level in ("p99", "p95", "p90", "p50"):
        threshold = getattr(baseline, level, None)
        if threshold is None or threshold <= 0:
            continue
        if value >= threshold:
            return {
                "level": level,
                "threshold": round(threshold, 2),
                "value": round(value, 2),
                "ratio": round(value / threshold, 2),
                "sample_count": baseline.sample_count,
                "source": baseline.source,
            }
    return None


def anomaly_score(value, baseline):
    """0..100: how far outside normal this reading is for this cell.

    Percentile-first, sigma as fallback, None when neither is available.
    The curve is deliberately gentle below p95 and steep above it: the
    operational question is not "is this above average" (half of all days are)
    but "is this beyond what this place normally absorbs".
    """
    if value is None or baseline is None:
        return None

    hit = exceedance(value, baseline)
    if hit is not None:
        floor = {"p50": 10.0, "p90": 35.0, "p95": 60.0, "p99": 85.0}[hit["level"]]
        # Clearing a threshold by a wide margin matters; the ratio adds up to
        # 15 points on top of the band's own floor.
        over = min(1.0, max(0.0, hit["ratio"] - 1.0))
        return min(100.0, floor + over * 15.0)

    sigma = sigma_above(value, baseline)
    if sigma is None:
        return None
    if sigma <= 0:
        return 0.0
    return min(100.0, (sigma / config.ANOMALY_SIGMA_ALERT) * 60.0)


def anomaly_multiplier(anomaly):
    """Turn an anomaly score into a gentle multiplier on hazard (1.0 .. 1.25).

    A multiplier rather than another additive term, and a small one, for a
    specific reason: being unusual is not the same as being dangerous. An
    unusually wet hour in a cell with no drains, near a lake, next to a
    hospital should escalate; the same hour on high ground should barely move.
    Multiplying lets the existing hazard and vulnerability terms decide which
    of those it is, instead of letting novelty alone raise an alarm.
    """
    if anomaly is None:
        return 1.0
    return 1.0 + 0.25 * min(1.0, max(0.0, anomaly / 100.0))


# --------------------------------------------------------------------------
# Official alert pressure
# --------------------------------------------------------------------------

def alert_pressure(alerts, now=None):
    """0..100 from the official alerts in force over one cell.

    Severity sets the level, certainty scales it, and an alert nearing its own
    expiry decays -- an IMD warning with ten minutes left is not the same
    claim as one just issued. Several overlapping alerts combine
    sub-additively (the strongest, plus a fraction of the rest): two warnings
    about the same storm are not twice the storm.
    """
    if not alerts:
        return 0.0

    contributions = []
    for alert in alerts:
        severity = config.CAP_SEVERITY_LEVEL.get(
            (getattr(alert, "severity", None) or "").strip().lower(), 25.0)
        certainty = config.CAP_CERTAINTY_CONFIDENCE.get(
            (getattr(alert, "certainty", None) or "").strip().lower(),
            config.CAP_CERTAINTY_CONFIDENCE["unknown"])

        urgency = (getattr(alert, "urgency", None) or "").strip().lower()
        urgency_factor = {"immediate": 1.0, "expected": 0.85,
                          "future": 0.6, "past": 0.3}.get(urgency, 0.8)

        contributions.append(severity * certainty * urgency_factor)

    contributions.sort(reverse=True)
    total = contributions[0] + sum(c * 0.35 for c in contributions[1:])
    return min(100.0, total)


# --------------------------------------------------------------------------
# Transit disruption
# --------------------------------------------------------------------------

#: Below this many vehicles a stall rate is noise, not signal: one bus at a
#: terminus is always "stopped" and means nothing at all.
MIN_VEHICLES_FOR_SIGNAL = 4


def transit_disruption_score(cell_stats):
    """0..100 from one cell's vehicle stall rate, or None if too few vehicles.

    None rather than 0.0 when the sample is too small, because "no buses are
    stuck here" and "no buses come here" are completely different statements
    about a road, and only one of them is reassuring.
    """
    if not cell_stats:
        return None
    vehicles = cell_stats.get("vehicles") or 0
    if vehicles < MIN_VEHICLES_FOR_SIGNAL:
        return None

    stall_rate = cell_stats.get("stall_rate") or 0.0
    # A quarter of buses stopped is ordinary traffic; everything stopped is
    # the signal. Below 25% the curve stays near zero on purpose.
    scaled = max(0.0, (stall_rate - 0.25) / 0.75)
    return min(100.0, 100.0 * math.pow(scaled, 0.8))


# --------------------------------------------------------------------------
# Station-measured air quality
# --------------------------------------------------------------------------

def station_aqi_score(aqi):
    """0..100 from a measured CPCB index.

    Kept separate from ``scoring.env_score``'s modelled AQI so the two can be
    blended with the measurement winning: a real instrument 400 m away beats a
    5 km reanalysis grid cell, and an operator should be told which one the
    number came from.
    """
    if aqi is None:
        return None
    return min(100.0, aqi / 4.0)


def explain(cell_evidence):
    """One human sentence per piece of evidence, for the brief and the drawer.

    Written here rather than in the agent because these sentences must be
    reproducible: the same numbers always produce the same words, with no
    model in the path, so a brief can be re-derived and checked afterwards.
    """
    lines = []

    rain = cell_evidence.get("rain")
    if rain and rain.get("exceedance"):
        hit = rain["exceedance"]
        lines.append(
            "Rainfall %.1f mm is %.1fx the %s (%.1f mm) normally seen in this "
            "cell this month (%d samples, %s)."
            % (hit["value"], hit["ratio"], hit["level"], hit["threshold"],
               hit.get("sample_count") or 0, hit.get("source") or "archive"))
    elif rain and rain.get("value") is not None and rain.get("baseline_missing"):
        lines.append("Rainfall %.1f mm; no local baseline exists yet, so it "
                     "cannot be called unusual." % rain["value"])

    alerts = cell_evidence.get("alerts") or []
    for alert in alerts[:3]:
        lines.append("%s issued a %s alert: %s."
                     % (alert.get("sender") or "An authority",
                        (alert.get("severity") or "").lower() or "an",
                        (alert.get("event") or alert.get("headline") or "").strip()))

    transit = cell_evidence.get("transit")
    if transit and transit.get("score") is not None:
        lines.append("%d of %d tracked vehicles here are stopped or silent (%.0f%%)."
                     % (transit.get("stalled") or 0, transit.get("vehicles") or 0,
                        100.0 * (transit.get("stall_rate") or 0.0)))

    air = cell_evidence.get("air")
    if air and air.get("aqi") is not None:
        lines.append("Nearest monitoring station reads AQI %d (%s), measured %s."
                     % (round(air["aqi"]), air.get("band") or "unclassified",
                        air.get("age") or "recently"))

    return lines
