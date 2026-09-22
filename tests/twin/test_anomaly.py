"""Anomaly detection and the extended risk formula.

Two things are pinned hardest here.

**Backwards compatibility.** The live-data terms were added to a formula that
was already scoring a running city. If `alert`, `disruption` and `anomaly` are
absent, every score must be bit-identical to what the three-term formula
produced -- otherwise adding a transit feed to one city would silently
re-rank the other.

**"Unmeasured" is not "zero".** A cell with no baseline, no station and no
buses must report None for those terms rather than a comfortable 0.0. The
whole module rests on that distinction and it is easy to erode.
"""

import pytest

from twin import anomaly, config, scoring


class _Baseline:
    """Stand-in for a TwinBaseline row -- these are pure functions, so the
    real model (and a database) would add nothing but setup cost."""

    def __init__(self, mean=None, stddev=None, p50=None, p90=None, p95=None,
                 p99=None, sample_count=500, source="open_meteo_archive"):
        self.mean = mean
        self.stddev = stddev
        self.p50 = p50
        self.p90 = p90
        self.p95 = p95
        self.p99 = p99
        self.sample_count = sample_count
        self.source = source


MONSOON = _Baseline(mean=4.0, stddev=8.0, p50=1.0, p90=14.0, p95=22.0, p99=48.0)


class TestExceedance:
    def test_reports_the_highest_percentile_cleared(self):
        hit = anomaly.exceedance(50.0, MONSOON)
        assert hit["level"] == "p99"
        assert hit["ratio"] == pytest.approx(50.0 / 48.0, abs=0.01)

    def test_a_middling_value_clears_only_the_median(self):
        assert anomaly.exceedance(3.0, MONSOON)["level"] == "p50"

    def test_a_value_below_everything_is_not_an_exceedance(self):
        assert anomaly.exceedance(0.2, MONSOON) is None

    def test_no_baseline_means_no_judgement(self):
        # Not "normal" -- unknown. A cell the twin has no history for must not
        # be reported as unremarkable.
        assert anomaly.exceedance(500.0, None) is None


class TestAnomalyScore:
    def test_percentiles_are_preferred_over_sigma(self):
        # Rain is zero most hours and extreme in a few, so sigma understates
        # the tail badly: 50 mm here is 5.75 sigma, which would saturate any
        # sigma-based score, while the percentile view keeps room above it.
        score = anomaly.anomaly_score(50.0, MONSOON)
        assert 85.0 <= score <= 100.0

    def test_falls_back_to_sigma_when_no_percentiles_exist(self):
        sigma_only = _Baseline(mean=30.0, stddev=2.0)
        score = anomaly.anomaly_score(35.0, sigma_only)
        assert score is not None and score > 0

    def test_a_thin_baseline_is_refused_rather_than_trusted(self):
        thin = _Baseline(mean=1.0, stddev=1.0, sample_count=5)
        assert anomaly.sigma_above(40.0, thin) is None

    def test_unmeasured_stays_none(self):
        assert anomaly.anomaly_score(None, MONSOON) is None
        assert anomaly.anomaly_score(10.0, None) is None

    def test_a_calm_reading_scores_zero_not_none(self):
        calm = _Baseline(mean=10.0, stddev=5.0)
        assert anomaly.anomaly_score(4.0, calm) == 0.0


class TestAnomalyMultiplier:
    def test_absent_anomaly_is_neutral(self):
        assert anomaly.anomaly_multiplier(None) == 1.0

    def test_multiplier_is_bounded(self):
        # Being unusual is not the same as being dangerous: the cap stops
        # novelty alone from driving a cell into a higher band.
        assert anomaly.anomaly_multiplier(100.0) == pytest.approx(1.25)
        assert anomaly.anomaly_multiplier(0.0) == 1.0


class TestAlertPressure:
    class _Alert:
        def __init__(self, severity, certainty="Observed", urgency="Immediate"):
            self.severity = severity
            self.certainty = certainty
            self.urgency = urgency

    def test_severity_drives_the_level(self):
        extreme = anomaly.alert_pressure([self._Alert("Extreme")])
        minor = anomaly.alert_pressure([self._Alert("Minor")])
        assert extreme > minor

    def test_certainty_scales_it_down(self):
        observed = anomaly.alert_pressure([self._Alert("Severe", "Observed")])
        possible = anomaly.alert_pressure([self._Alert("Severe", "Possible")])
        assert possible < observed

    def test_overlapping_alerts_combine_sub_additively(self):
        one = anomaly.alert_pressure([self._Alert("Severe")])
        three = anomaly.alert_pressure([self._Alert("Severe")] * 3)
        # Two warnings about the same storm are not twice the storm.
        assert one < three < one * 3

    def test_no_alerts_is_zero_not_none(self):
        assert anomaly.alert_pressure([]) == 0.0


class TestTransitDisruption:
    def test_a_stalled_fleet_scores_high(self):
        score = anomaly.transit_disruption_score(
            {"vehicles": 20, "stalled": 19, "stall_rate": 0.95})
        assert score > 80

    def test_ordinary_traffic_barely_registers(self):
        score = anomaly.transit_disruption_score(
            {"vehicles": 20, "stalled": 4, "stall_rate": 0.2})
        assert score == 0.0

    def test_too_few_vehicles_is_unmeasured_not_calm(self):
        # "No buses are stuck here" and "no buses come here" are different
        # statements about a road, and only one is reassuring.
        assert anomaly.transit_disruption_score(
            {"vehicles": 1, "stalled": 1, "stall_rate": 1.0}) is None
        assert anomaly.transit_disruption_score(None) is None


class TestCompositeBackwardsCompatibility:
    """The extension must not move a single score where it has nothing to add."""

    CASES = [
        (60.0, 20.0, 30.0, 50.0, 40.0),
        (0.0, 0.0, 0.0, 0.0, 0.0),
        (100.0, 100.0, 100.0, 100.0, 100.0),
        (None, 12.0, None, None, 0.0),
    ]

    @pytest.mark.parametrize("hydro,incident,env,terrain,infra", CASES)
    def test_absent_live_terms_reproduce_the_original_formula(
            self, hydro, incident, env, terrain, infra):
        result = scoring.composite(hydro, incident, env, terrain, infra)

        expected_hazard, _ = scoring._weighted_renormalize({
            "hydro": (hydro, config.HAZARD_WEIGHTS["hydro"]),
            "incident": (incident, config.HAZARD_WEIGHTS["incident"]),
            "env": (env, config.HAZARD_WEIGHTS["env"]),
        })
        assert result["hazard_score"] == (
            None if expected_hazard is None else pytest.approx(expected_hazard))
        assert result["anomaly_multiplier"] == 1.0

    def test_an_absent_feed_is_not_reported_as_degraded(self):
        # Reporting "no transit feed" as a degraded input on all 1,747 cells
        # would drown the one signal degraded_inputs exists to carry.
        result = scoring.composite(50.0, 0.0, 10.0, 40.0, 20.0)
        assert "disruption" not in result["degraded_inputs"]
        assert "alert" not in result["degraded_inputs"]


class TestCompositeWithLiveTerms:
    def test_an_official_alert_raises_the_score(self):
        without = scoring.composite(30.0, 0.0, 10.0, 50.0, 20.0)
        with_alert = scoring.composite(30.0, 0.0, 10.0, 50.0, 20.0, alert=90.0)
        assert with_alert["risk_score"] > without["risk_score"]

    def test_an_anomaly_amplifies_rather_than_replaces(self):
        calm_cell = scoring.composite(10.0, 0.0, 5.0, 10.0, 0.0, anomaly=100.0)
        exposed_cell = scoring.composite(10.0, 0.0, 5.0, 95.0, 90.0, anomaly=100.0)
        # The same "unusual" reading escalates far more where there is
        # something at stake -- which is the point of multiplying.
        assert exposed_cell["risk_score"] > calm_cell["risk_score"]

    def test_anomaly_cannot_manufacture_risk_from_nothing(self):
        nothing_happening = scoring.composite(0.0, 0.0, 0.0, 90.0, 90.0, anomaly=100.0)
        assert nothing_happening["risk_score"] == 0.0

    def test_transit_disruption_nudges_but_does_not_dominate(self):
        alert_only = scoring.composite(20.0, 0.0, 10.0, 50.0, 20.0, alert=80.0)
        transit_only = scoring.composite(20.0, 0.0, 10.0, 50.0, 20.0, disruption=80.0)
        # Buses also stop for protests, VIP movement and roadworks.
        assert transit_only["risk_score"] < alert_only["risk_score"]

    def test_scores_stay_inside_the_band_range(self):
        maxed = scoring.composite(100.0, 100.0, 100.0, 100.0, 100.0,
                                  alert=100.0, disruption=100.0, anomaly=100.0)
        assert 0.0 <= maxed["risk_score"] <= 100.0
        assert maxed["status"] == "critical"


class TestExplain:
    def test_produces_checkable_sentences(self):
        lines = anomaly.explain({
            "rain": {"value": 62.0,
                     "exceedance": {"level": "p95", "threshold": 30.0, "value": 62.0,
                                    "ratio": 2.07, "sample_count": 4380,
                                    "source": "open_meteo_archive"}},
            "alerts": [{"sender": "IMD Bengaluru", "severity": "Severe",
                        "event": "Heavy rainfall warning"}],
            "transit": {"score": 70.0, "stalled": 14, "vehicles": 18, "stall_rate": 0.78},
        })
        joined = " ".join(lines)
        # The brief must quote numbers an operator can re-derive, not adjectives.
        assert "62.0 mm" in joined and "p95" in joined
        assert "IMD Bengaluru" in joined
        assert "14 of 18" in joined
