"""Boundary-value tests for every sub-score (Phase 3 checkpoint).

Pure functions, no DB, no network -- see the twin.scoring module docstring
for why that separation exists.
"""

from datetime import datetime, timedelta, timezone

import pytest

from twin import config, scoring


class TestHydroScore:
    def test_all_zero_is_zero(self):
        score, dropped = scoring.hydro_score(0, 0, 0, 100, horizon_hours=0)
        assert score == 0.0
        assert dropped == []

    def test_rain_now_saturates_at_25mm(self):
        score, _ = scoring.hydro_score(25, 0, None, None, horizon_hours=0)
        # rain_now term alone = min(100, 25*4) = 100; with discharge dropped,
        # renormalised over (rain_now, rain_forecast) weights 0.40/0.40.
        assert score == pytest.approx(100.0 * 0.5)

    def test_rain_forecast_saturates_at_50mm_per_3h(self):
        score, _ = scoring.hydro_score(0, 50, None, None, horizon_hours=0)
        assert score == pytest.approx(100.0 * 0.5)

    def test_discharge_at_return_period_is_60(self):
        score, dropped = scoring.hydro_score(0, 0, 100, 100, horizon_hours=0)
        # discharge = min(100, (100/100)*60) = 60; weight 0.20 of the total.
        assert dropped == []
        assert score == pytest.approx(0.40 * 0 + 0.40 * 0 + 0.20 * 60)

    def test_missing_discharge_renormalises_not_zeroes(self):
        with_discharge, _ = scoring.hydro_score(10, 10, 50, 100, horizon_hours=0)
        without_discharge, dropped = scoring.hydro_score(10, 10, None, None, horizon_hours=0)
        assert dropped == ["discharge"]
        # Same rain inputs must not silently score lower just because the
        # flood API is degraded -- that would look like "less hazard" when
        # it is really "less information" (section 5.1 renorm rule).
        assert without_discharge == pytest.approx((0.40 * 40 + 0.40 * 20) / 0.80)

    def test_all_missing_is_none(self):
        score, dropped = scoring.hydro_score(None, None, None, None, horizon_hours=0)
        assert score is None
        assert set(dropped) == {"rain_now", "rain_forecast", "discharge"}

    def test_horizon_24_upweights_discharge(self):
        w_now, w_forecast, w_discharge = config.HYDRO_WEIGHTS[24]
        assert w_discharge > config.HYDRO_WEIGHTS[0][2]
        score, _ = scoring.hydro_score(0, 0, 100, 100, horizon_hours=24)
        assert score == pytest.approx(w_discharge * 60)


class TestIncidentScore:
    def _report(self, priority="high", confidence=0.8, hours_ago=0, report_id=1):
        return {
            "id": report_id,
            "priority": priority,
            "confidence": confidence,
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
        }

    def test_no_reports_is_zero_not_none(self):
        score, count, top_id = scoring.incident_score([])
        assert score == 0.0  # NOT None -- see module docstring rule 1
        assert count == 0
        assert top_id is None

    def test_fresh_critical_report_in_cell(self):
        report = self._report(priority="critical", confidence=1.0, hours_ago=0)
        score, count, top_id = scoring.incident_score([(report, True)])
        assert score == pytest.approx(100.0, abs=0.5)
        assert count == 1
        assert top_id == 1

    def test_neighbour_spillover_is_weighted_down(self):
        report = self._report(priority="critical", confidence=1.0, hours_ago=0)
        in_cell, _, _ = scoring.incident_score([(report, True)])
        neighbour, _, _ = scoring.incident_score([(report, False)])
        assert neighbour == pytest.approx(in_cell * config.INCIDENT_NEIGHBOUR_WEIGHT)

    def test_decay_halves_by_8_3_hours(self):
        report = self._report(priority="critical", confidence=1.0, hours_ago=8.3178)
        score, _, _ = scoring.incident_score([(report, True)])
        assert score == pytest.approx(50.0, rel=0.02)

    def test_null_confidence_uses_default_not_zero(self):
        """The `or 0.5` bug this project fixed: a genuine 0.0 confidence must
        NOT be promoted to 0.5, but a missing (None) confidence must be."""
        missing = self._report(confidence=None, hours_ago=0)
        zero = self._report(confidence=0.0, hours_ago=0)
        score_missing, _, _ = scoring.incident_score([(missing, True)])
        score_zero, _, _ = scoring.incident_score([(zero, True)])
        assert score_missing == pytest.approx(75.0 * config.INCIDENT_DEFAULT_CONFIDENCE, abs=0.5)
        assert score_zero == pytest.approx(0.0, abs=1e-6)
        assert score_zero < score_missing

    def test_caps_at_100(self):
        reports = [(self._report(priority="critical", confidence=1.0, hours_ago=0, report_id=i), True)
                   for i in range(5)]
        score, count, _ = scoring.incident_score(reports)
        assert score == 100.0
        assert count == 5

    def test_top_report_is_the_highest_contributor(self):
        weak = self._report(priority="low", confidence=0.3, hours_ago=10, report_id=1)
        strong = self._report(priority="critical", confidence=1.0, hours_ago=0, report_id=2)
        _, _, top_id = scoring.incident_score([(weak, True), (strong, True)])
        assert top_id == 2


class TestTerrainScore:
    def test_missing_elevation_is_none(self):
        assert scoring.terrain_score(None, [10, 20, 30], 100, 0) is None

    def test_lowest_cell_in_city_scores_highest_low_lying(self):
        population = [10, 20, 30, 40, 50]
        score = scoring.terrain_score(10, population, dist_to_water_m=2000, drain_length_m=2000)
        # elev_pct = 1/5 = 0.2 -> low_lying = 80; water/drain both benign.
        assert score == pytest.approx(0.45 * 80 + 0.35 * 0 + 0.20 * 0)

    def test_adjacent_to_water_scores_100_proximity(self):
        score = scoring.terrain_score(50, [50], dist_to_water_m=100, drain_length_m=2000)
        assert score == pytest.approx(0.45 * 0 + 0.35 * 100 + 0.20 * 0)

    def test_no_drains_scores_full_drain_gap(self):
        score = scoring.terrain_score(50, [50], dist_to_water_m=2000, drain_length_m=0)
        assert score == pytest.approx(0.45 * 0 + 0.35 * 0 + 0.20 * 100)

    def test_2000m_of_drain_saturates_gap_to_zero(self):
        score = scoring.terrain_score(50, [50], dist_to_water_m=2000, drain_length_m=2000)
        assert score == pytest.approx(0.0)

    def test_water_proximity_bands(self):
        assert scoring.terrain_score(50, [50], 199, 2000) == pytest.approx(0.35 * 100)
        assert scoring.terrain_score(50, [50], 499, 2000) == pytest.approx(0.35 * 60)
        assert scoring.terrain_score(50, [50], 999, 2000) == pytest.approx(0.35 * 25)
        assert scoring.terrain_score(50, [50], 1000, 2000) == pytest.approx(0.0)


class TestInfraScore:
    def test_no_assets_is_zero_not_none(self):
        assert scoring.infra_score(0.0) == 0.0

    def test_missing_cache_is_none(self):
        assert scoring.infra_score(None) is None

    def test_saturates_at_100(self):
        # gain=12; a hospital(1.0)+2 substations(0.9 each)=2.8 -> 33.6, still below 100
        assert scoring.infra_score(2.8) == pytest.approx(2.8 * config.INFRA_CRITICALITY_GAIN)
        assert scoring.infra_score(10.0) == 100.0


class TestEnvScore:
    def test_calm_weather_is_zero(self):
        score, dropped = scoring.env_score(us_aqi=0, apparent_temp_c=25)
        assert score == 0.0
        assert dropped == []

    def test_aqi_300_saturates(self):
        score, _ = scoring.env_score(us_aqi=300, apparent_temp_c=25)
        assert score == pytest.approx(0.60 * 100)

    def test_heat_50c_saturates(self):
        score, _ = scoring.env_score(us_aqi=0, apparent_temp_c=50)
        assert score == pytest.approx(0.40 * 100)

    def test_missing_aqi_renormalises_onto_heat(self):
        score, dropped = scoring.env_score(us_aqi=None, apparent_temp_c=40)
        assert dropped == ["aqi"]
        assert score == pytest.approx(50.0)  # heat term alone, full weight

    def test_both_missing_is_none(self):
        score, dropped = scoring.env_score(None, None)
        assert score is None
        assert set(dropped) == {"aqi", "heat"}


class TestComposite:
    def test_calm_cell_scores_zero_even_with_high_vulnerability(self):
        """The bug this project's composite rewrite fixes: a low-lying,
        hospital-dense cell must NOT sit at a permanent `watch` when nothing
        is happening."""
        result = scoring.composite(hydro=0.0, incident=0.0, env=0.0, terrain=100.0, infra=100.0)
        assert result["risk_score"] == 0.0
        assert result["status"] == "normal"
        assert result["vulnerability_multiplier"] == pytest.approx(1.6)

    def test_high_hazard_amplified_by_vulnerability(self):
        # Kept well below saturation (hazard=40, not 80) so the 1.6x
        # amplification is visible in the assertion rather than clamped away.
        calm_vuln = scoring.composite(hydro=40, incident=40, env=40, terrain=0, infra=0)
        exposed_vuln = scoring.composite(hydro=40, incident=40, env=40, terrain=100, infra=100)
        assert exposed_vuln["risk_score"] > calm_vuln["risk_score"]
        assert exposed_vuln["risk_score"] == pytest.approx(calm_vuln["risk_score"] * 1.6, rel=1e-6)

    def test_risk_score_clamped_to_100(self):
        result = scoring.composite(hydro=100, incident=100, env=100, terrain=100, infra=100)
        assert result["risk_score"] == 100.0

    def test_status_band_boundaries(self):
        for hydro, expected_status in ((0, "normal"), (46, "watch"), (91, "warning"), (137, "critical")):
            # hazard = 0.55*hydro (incident/env=0) so risk = hazard*1.0 = 0.55*hydro
            result = scoring.composite(hydro=hydro, incident=0, env=0, terrain=0, infra=0)
            assert result["status"] == expected_status, (hydro, result["risk_score"])

    def test_degraded_inputs_merges_both_halves(self):
        result = scoring.composite(hydro=None, incident=0.0, env=50, terrain=None, infra=0)
        assert "hydro" in result["degraded_inputs"]
        assert "terrain" in result["degraded_inputs"]
        assert "incident" not in result["degraded_inputs"]

    def test_incident_alone_never_makes_hazard_none(self):
        """incident_score() always returns 0.0, never None (rule 1), so
        hazard should never be fully unmeasured in practice."""
        result = scoring.composite(hydro=None, incident=0.0, env=None, terrain=None, infra=None)
        assert result["hazard_score"] is not None
        assert result["hazard_score"] == 0.0
