"""The forecast physics, and the agent that turns it into flags.

Two things are worth testing hard here and one is not.

Worth testing: **the vector convention**, because getting it backwards puts
every projected storm on the opposite side of the city and still looks
entirely plausible on a map; and **the guard rails** -- confidence decay,
thresholds, the conditional edge -- because they are what stop the agent
filling an analyst's queue with nine-hour guesses.

Not worth testing: whether the projection is meteorologically *right*. It is
a first-order kinematic advection and it is documented as one. The tests pin
what the code claims, not what a numerical weather model would say.

Nothing here touches the network or an LLM.
"""

import math

import pytest

from twin import forecast as fx
from twin.ingest.windfield import wind_vector


# --------------------------------------------------------------------------
# The vector convention -- the error that would be invisible on a map
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bearing,expected", [
    # Meteorological bearing is where the wind comes FROM.
    (0.0, (0.0, -1.0)),     # northerly  -> air moves south
    (90.0, (-1.0, 0.0)),    # easterly   -> air moves west
    (180.0, (0.0, 1.0)),    # southerly  -> air moves north
    (270.0, (1.0, 0.0)),    # westerly   -> air moves east
])
def test_wind_vector_points_where_the_air_goes_not_where_it_came_from(bearing, expected):
    u, v = wind_vector(1.0, bearing)
    assert u == pytest.approx(expected[0], abs=1e-9)
    assert v == pytest.approx(expected[1], abs=1e-9)


def test_wind_vector_is_none_without_both_components():
    assert wind_vector(None, 90.0) is None
    assert wind_vector(10.0, None) is None


def test_a_westerly_moves_a_parcel_east():
    """The end-to-end version of the convention test, in coordinates."""
    u, v = wind_vector(36.0, 270.0)           # 36 km/h westerly
    lat, lon = fx.displace(12.97, 77.59, u, v, 1.0)

    assert lon > 77.59, "a westerly must carry the parcel eastward"
    assert lat == pytest.approx(12.97, abs=1e-6)


def test_displacement_distance_matches_the_wind_speed():
    u, v = wind_vector(30.0, 180.0)           # southerly, due north at 30 km/h
    lat, lon = fx.displace(17.385, 78.4867, u, v, 2.0)

    assert fx._km_between(17.385, 78.4867, lat, lon) == pytest.approx(60.0, rel=0.02)


def test_displace_is_none_without_a_vector():
    assert fx.displace(12.97, 77.59, None, 1.0, 1.0) is None


def test_displace_survives_the_pole():
    """cos(lat) -> 0 would otherwise turn a bad input into a coordinate off the map."""
    result = fx.displace(89.9999, 10.0, 50.0, 0.0, 6.0)
    assert result is not None
    assert -180.0 <= result[1] <= 180.0 or result[1] == 10.0


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def test_confidence_falls_with_lead_time():
    strong = 25.0
    values = [fx.confidence_for(hour, strong) for hour in (1, 3, 6, 12)]
    assert values == sorted(values, reverse=True)
    assert values[0] > values[-1]


def test_confidence_is_penalised_when_the_wind_is_too_light_to_have_a_bearing():
    """At 1 km/h the direction is noise, and a projection along it is too."""
    assert fx.confidence_for(2, 1.0) < fx.confidence_for(2, 20.0)


def test_confidence_without_a_wind_speed_is_halved_not_assumed():
    assert fx.confidence_for(3, None) < fx.confidence_for(3, 20.0)


def test_confidence_now_is_certain():
    assert fx.confidence_for(0, 10.0) == 1.0


# --------------------------------------------------------------------------
# Sources and severity
# --------------------------------------------------------------------------

def _point(lat=12.97, lon=77.59, hours=None):
    return {"lat": lat, "lon": lon, "hours": hours or []}


def _hour(index, precip=0.0, cloud=0.0, speed=20.0, bearing=270.0, apparent=None):
    vector = wind_vector(speed, bearing) or (None, None)
    return {"hour": index, "precip_mm": precip, "cloud_pct": cloud,
            "wind_speed_kmh": speed, "wind_dir_deg": bearing,
            "u_kmh": vector[0], "v_kmh": vector[1],
            "temp_c": 30.0, "humidity_pct": 60, "apparent_c": apparent}


def test_drizzle_is_not_a_system_worth_projecting():
    point = _point(hours=[_hour(0, precip=fx.RAIN_SOURCE_MM_H - 0.1)])
    assert fx.rain_sources(point) == []


def test_real_rain_is_a_source():
    point = _point(hours=[_hour(0, precip=fx.RAIN_SOURCE_MM_H + 1.0)])
    assert len(fx.rain_sources(point)) == 1


def test_an_hour_with_no_wind_vector_cannot_be_a_source():
    """Nothing to advect along means no projection, not a projection of zero."""
    hour = _hour(0, precip=20.0)
    hour["u_kmh"] = hour["v_kmh"] = None
    assert fx.rain_sources(_point(hours=[hour])) == []


def test_severity_is_discounted_by_confidence_rather_than_filtered():
    """A far-off severe projection should degrade to a watch, not disappear."""
    assert fx.rain_severity(20.0, 1.0) == "critical"
    assert fx.rain_severity(20.0, 0.3) in ("watch", "warning")
    assert fx.rain_severity(0.1, 1.0) is None


def test_heat_severity_bands():
    assert fx.heat_severity(fx.HEAT_SEVERE_C + 1) == "critical"
    assert fx.heat_severity(fx.HEAT_WATCH_C + 1) == "warning"
    assert fx.heat_severity(fx.HEAT_WATCH_C - 1) is None
    assert fx.heat_severity(None) is None


def test_heat_reports_crossings_not_every_hot_hour():
    """An analyst needs "it passes 42 at 14:00", not forty rows saying it is warm."""
    point = _point(hours=[
        _hour(0, apparent=30.0),
        _hour(1, apparent=39.0),   # crosses into warning
        _hour(2, apparent=39.5),   # still warning -- not a new crossing
        _hour(3, apparent=43.0),   # crosses into critical
        _hour(4, apparent=43.5),
    ])
    windows = fx.heat_windows(point)

    assert [w["hour"] for w in windows] == [1, 3]
    assert [w["severity"] for w in windows] == ["warning", "critical"]


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------

def test_projection_produces_arrivals_downwind_with_times_attached():
    point = _point(hours=[_hour(0, precip=10.0, speed=30.0, bearing=270.0)])
    arrivals = fx.project_rain([point], max_lead_hours=3)

    assert len(arrivals) == 3
    assert [a["lead_hours"] for a in arrivals] == [1, 2, 3]
    assert all(a["to"][1] > point["lon"] for a in arrivals), "westerly carries east"
    assert arrivals[0]["confidence"] > arrivals[-1]["confidence"]


def test_a_system_that_has_not_moved_has_not_arrived_anywhere():
    """Under a near-calm wind the parcel stays put; that is the source cell's
    problem, not a new arrival somewhere else."""
    point = _point(hours=[_hour(0, precip=10.0, speed=0.2, bearing=270.0)])
    assert fx.project_rain([point], max_lead_hours=2) == []


def test_arrival_hour_is_the_source_hour_plus_the_lead():
    point = _point(hours=[_hour(5, precip=10.0, speed=30.0)])
    arrivals = fx.project_rain([point], max_lead_hours=2)

    assert {a["at_hour"] for a in arrivals} == {6, 7}


def test_no_rain_anywhere_projects_nothing():
    assert fx.project_rain([_point(hours=[_hour(0), _hour(1)])]) == []


def test_cloud_decks_are_projected_only_when_coherent():
    thin = _point(hours=[_hour(0, cloud=fx.CLOUD_SOURCE_PCT - 10)])
    thick = _point(hours=[_hour(0, cloud=fx.CLOUD_SOURCE_PCT + 10)])

    assert fx.cloud_sources(thin) == []
    assert len(fx.cloud_sources(thick)) == 1


# --------------------------------------------------------------------------
# The agent's guard rails
# --------------------------------------------------------------------------

def test_threshold_drops_low_confidence_and_cell_less_candidates():
    from twin.agent import forecast_nodes as fn

    state = {"candidates": [
        {"confidence": 0.9, "cells": ["a"], "severity": "warning",
         "hazard_type": "rain", "at_hour": 2, "centre": (12.97, 77.59)},
        {"confidence": 0.05, "cells": ["b"], "severity": "critical",
         "hazard_type": "rain", "at_hour": 1,           # too uncertain
         "centre": (12.97, 77.59)},
        {"confidence": 0.9, "cells": [], "severity": "critical",
         "hazard_type": "rain", "at_hour": 1,           # lands off the grid
         "centre": (12.97, 77.59)},
        {"confidence": 0.9, "cells": ["c"], "severity": None,
         "hazard_type": "rain", "at_hour": 1,           # below every band
         "centre": (12.97, 77.59)}
    ]}
    result = fn.threshold(state)

    assert result["flag_count"] == 1
    assert result["flagged"][0]["cells"] == ["a"]


def test_threshold_orders_by_soonest_arrival():
    from twin.agent import forecast_nodes as fn

    state = {"candidates": [
        {"confidence": 0.9, "cells": ["a"], "severity": "watch",
         "hazard_type": "rain", "at_hour": 9, "centre": (12.97, 77.59)},
        {"confidence": 0.9, "cells": ["b"], "severity": "watch",
         "hazard_type": "rain", "at_hour": 2, "centre": (17.38, 78.48)},
    ]}
    flagged = fn.threshold(state)["flagged"]

    assert [c["at_hour"] for c in flagged] == [2, 9]


def test_cluster_key_survives_the_arrival_hour_changing():
    """The same storm re-projected must update one flag, not create a new one.

    Keying on the arrival hour would mint a fresh flag on every pass, which
    is how a queue becomes unusable.
    """
    from twin.agent import forecast_nodes as fn

    early = {"hazard_type": "rain", "centre": (12.97, 77.59), "at_hour": 3}
    later = {"hazard_type": "rain", "centre": (12.97, 77.59), "at_hour": 7}

    assert fn._cluster_key(early) == fn._cluster_key(later)


def test_cluster_key_survives_the_projection_drifting():
    """The second version of this bug: keying on the cluster's own cells.

    A projection that moves a kilometre between passes reorders the sorted
    cell list, changes cells[0], and mints a duplicate flag for one storm.
    Observed live -- a re-run created 7 new flags instead of updating 7.
    """
    from twin.agent import forecast_nodes as fn

    before = {"hazard_type": "rain", "centre": (12.970, 77.590), "at_hour": 3}
    after = {"hazard_type": "rain", "centre": (12.978, 77.601), "at_hour": 3}

    assert fn._cluster_key(before) == fn._cluster_key(after)


def test_cluster_key_separates_distant_systems_and_different_hazards():
    """Stability must not become "everything is one flag"."""
    from twin.agent import forecast_nodes as fn

    here = {"hazard_type": "rain", "centre": (12.97, 77.59), "at_hour": 3}
    far = {"hazard_type": "rain", "centre": (17.38, 78.48), "at_hour": 3}
    heat = {"hazard_type": "heat", "centre": (12.97, 77.59), "at_hour": 3}

    assert fn._cluster_key(here) != fn._cluster_key(far)
    assert fn._cluster_key(here) != fn._cluster_key(heat)


def test_cluster_key_tolerates_a_bad_centre():
    from twin.agent import forecast_nodes as fn

    key = fn._cluster_key({"hazard_type": "rain", "centre": (999.0, 999.0),
                           "at_hour": 1})
    assert key.startswith("fx:rain:")


def test_nothing_projected_means_no_llm_call():
    """The conditional edge: a calm forecast must cost zero tokens."""
    from twin.agent import forecast_nodes as fn

    assert fn.retrieve({"flagged": []}) == {"context": {}}
    assert fn.draft_brief({"flagged": [], "context": {}}) == {"briefs": []}


def test_the_graph_skips_the_llm_nodes_when_nothing_cleared():
    from twin.agent import forecast_graph

    assert forecast_graph._AFTER_THRESHOLD == {"retrieve", "draft_brief", "persist"}
    names = [name for name, _fn in forecast_graph.SEQUENCE]
    assert names.index("threshold") < names.index("draft_brief")


def test_evidence_records_the_numbers_not_the_prose():
    """"Why was this flagged" must have a numeric answer (C3)."""
    from twin.agent import forecast_nodes as fn

    evidence = fn._evidence_for({
        "kind": "rain_arrival", "at_hour": 4, "lead_hours": 2,
        "confidence": 0.7, "arrival_count": 6, "source_count": 3,
        "centre": (12.9, 77.6), "peak_mm_h": 8.1,
        "wind_speed_kmh": 20.0, "wind_dir_deg": 270,
    })

    assert evidence["method"] == "advection"
    assert evidence["peak_mm_h"] == 8.1
    assert evidence["wind_dir_deg"] == 270
    assert evidence["at_hour"] == 4


def test_heat_evidence_says_it_was_not_advected():
    from twin.agent import forecast_nodes as fn

    evidence = fn._evidence_for({
        "kind": "heat", "at_hour": 6, "lead_hours": 6, "confidence": 0.6,
        "arrival_count": 4, "source_count": 4, "centre": (17.4, 78.5),
        "apparent_c": 43.2, "humidity_pct": 55,
    })

    assert evidence["method"] == "threshold_crossing"
    assert evidence["apparent_c"] == 43.2


# --------------------------------------------------------------------------
# What the analyst reads
# --------------------------------------------------------------------------

class _City(object):
    display_name = "Bengaluru"
    slug = "bengaluru"


def test_the_title_leads_with_when_it_arrives():
    from twin.agent import forecast_nodes as fn

    title = fn._title_for({
        "kind": "rain_arrival", "at_hour": 3, "severity": "warning",
        "peak_mm_h": 7.4,
    }, _City())

    assert "in ~3h" in title
    assert "in in" not in title, "the 'moving in in ~3h' duplication"


def test_the_brief_separates_arrival_time_from_projection_distance():
    """Labelling both as a "lead" made them indistinguishable, and the model
    wrote itself in circles reconciling "hour +12" with "lead 1h"."""
    from twin.agent import forecast_nodes as fn

    lines = fn._fact_lines({
        "kind": "rain_arrival", "at_hour": 12, "lead_hours": 1,
        "confidence": 0.94, "cells": ["a", "b"], "severity": "warning",
        "peak_mm_h": 7.3, "wind_speed_kmh": 21.0, "wind_dir_deg": 244,
        "source_count": 7,
    }, _City())
    text = "\n".join(lines)

    assert "Arrives: in ~12h from now" in text
    assert "carried 1h downwind" in text
    assert "forecast hour +11" in text, "the source hour, so the two are distinguishable"


def test_the_brief_states_the_method_is_first_order():
    from twin.agent import forecast_nodes as fn

    text = "\n".join(fn._fact_lines({
        "kind": "rain_arrival", "at_hour": 4, "lead_hours": 2,
        "confidence": 0.8, "cells": ["a"], "severity": "watch",
        "peak_mm_h": 3.0, "wind_speed_kmh": 12.0, "wind_dir_deg": 200,
        "source_count": 3,
    }, _City()))

    assert "first-order" in text.lower()


def test_heat_briefs_do_not_claim_a_projection_step():
    from twin.agent import forecast_nodes as fn

    text = "\n".join(fn._fact_lines({
        "kind": "heat", "at_hour": 5, "lead_hours": 5, "confidence": 0.7,
        "cells": ["a"], "severity": "critical", "apparent_c": 43.0,
        "humidity_pct": 58,
    }, _City()))

    assert "not" in text.lower() and "advected" in text.lower()
