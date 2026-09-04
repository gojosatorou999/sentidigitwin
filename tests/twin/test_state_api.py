"""Phase 4 API integration tests: /state, /cell, /summary, /compare,
/incidents, /infrastructure, /seed, /refresh.

No network calls: cells and states are inserted directly, and anything that
would otherwise reach an external API (engine.compute_state,
grid.generate_cells_for_city) is monkeypatched to a stub. See
test_ingest_fallbacks.py for adapter-level degradation coverage and
test_grid.py for real (small, synthetic) H3 generation.
"""

import json

import pytest

from twin import models as m


@pytest.fixture()
def hyderabad(db):
    return db.session.query(m.TwinCity).filter_by(slug="hyderabad").one()


@pytest.fixture()
def khairatabad_zone(db, hyderabad):
    return db.session.query(m.TwinZone).filter_by(city_id=hyderabad.id, slug="khairatabad").one()


def _make_cell(db, city, zone_id=None, h3_index="8860a2b1c3fffff", lat=17.41, lon=78.46):
    cell = m.TwinCell(
        h3_index=h3_index, city_id=city.id, zone_id=zone_id,
        center_latitude=lat, center_longitude=lon,
        boundary_geojson=json.dumps([[lon, lat], [lon + 0.001, lat],
                                     [lon + 0.001, lat + 0.001], [lon, lat + 0.001]]),
        area_sqkm=0.74, elevation_m=520.0, elevation_source="open_meteo",
        dist_to_water_m=300.0, drain_length_m=500.0,
        infra_criticality_cached=1.0, terrain_score_cached=45.0,
    )
    db.session.add(cell)
    db.session.commit()
    return cell


def _make_state(db, cell, horizon=0, risk_score=78.0, status="critical"):
    state = m.TwinCellState(
        cell_id=cell.id, horizon_hours=horizon, risk_score=risk_score, status=status,
        hazard_score=60.0, vulnerability_multiplier=1.3,
        hydro_score=81.0, incident_score=66.0, terrain_score=72.0,
        infra_score=48.0, env_score=39.0,
        raw_inputs={"rain_now_mm_1h": 34.0, "dist_to_water_m": 180.0},
        degraded_inputs=[], incident_count=2, computed_at=m.utcnow(),
    )
    db.session.add(state)
    db.session.commit()
    return state


class TestStateEndpoint:
    def test_compact_is_the_default(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)

        response = official.get("/api/twin/hyderabad/state")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)
        assert body[0]["h3"] == cell.h3_index
        assert body[0]["risk_score"] == 78.0
        assert body[0]["status"] == "critical"
        assert "colour" in body[0] and "height_m" in body[0]

    def test_geometry_true_returns_feature_collection(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)

        response = official.get("/api/twin/hyderabad/state?geometry=true")
        body = response.get_json()
        assert body["type"] == "FeatureCollection"
        assert body["features"][0]["properties"]["h3"] == cell.h3_index
        assert body["features"][0]["geometry"]["type"] == "Polygon"

    def test_cell_with_no_state_row_defaults_to_normal(self, official, db, hyderabad):
        _make_cell(db, hyderabad)  # no _make_state call

        response = official.get("/api/twin/hyderabad/state")
        body = response.get_json()
        assert body[0]["status"] == "normal"
        assert body[0]["risk_score"] == 0.0

    def test_zone_filter_narrows_to_matching_cells(self, official, db, hyderabad, khairatabad_zone):
        in_zone = _make_cell(db, hyderabad, zone_id=khairatabad_zone.id, h3_index="8860a2b1c3fffff")
        out_of_zone = _make_cell(db, hyderabad, zone_id=None, h3_index="8860a2b1c7fffff", lon=78.5)
        _make_state(db, in_zone)
        _make_state(db, out_of_zone)

        response = official.get("/api/twin/hyderabad/state?zone=khairatabad")
        body = response.get_json()
        assert {c["h3"] for c in body} == {in_zone.h3_index}

    def test_unknown_zone_is_404(self, official):
        response = official.get("/api/twin/hyderabad/state?zone=nonexistent")
        assert response.status_code == 404

    def test_invalid_horizon_is_400(self, official):
        response = official.get("/api/twin/hyderabad/state?horizon=5")
        assert response.status_code == 400

    def test_unknown_city_is_404(self, official):
        response = official.get("/api/twin/atlantis/state")
        assert response.status_code == 404

    def test_fields_param_narrows_response(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)

        response = official.get("/api/twin/hyderabad/state?fields=risk_score,status")
        body = response.get_json()[0]
        assert set(body) == {"h3", "risk_score", "status"}

    def test_etag_supports_304(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)

        first = official.get("/api/twin/hyderabad/state")
        etag = first.headers["ETag"]

        second = official.get("/api/twin/hyderabad/state", headers={"If-None-Match": etag})
        assert second.status_code == 304


class TestCellDrilldown:
    def test_full_payload_shape(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)

        response = official.get("/api/twin/hyderabad/cell/%s" % cell.h3_index)
        assert response.status_code == 200
        body = response.get_json()
        assert body["h3"] == cell.h3_index
        assert "explanation" in body and isinstance(body["explanation"], str)
        assert body["state"]["risk_score"] == 78.0
        assert body["raw_inputs"]["rain_now_mm_1h"] == 34.0
        assert body["assets"] == []
        assert body["reports"] == []

    def test_explanation_names_the_dominant_drivers(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)

        body = official.get("/api/twin/hyderabad/cell/%s" % cell.h3_index).get_json()
        assert "34 mm/h" in body["explanation"]

    def test_unknown_cell_is_404(self, official):
        response = official.get("/api/twin/hyderabad/cell/8860a2b1c3fffff")
        assert response.status_code == 404

    def test_includes_infrastructure_in_the_cell(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)
        db.session.add(m.TwinInfrastructure(
            city_id=hyderabad.id, cell_id=cell.id, osm_id="node/1",
            asset_type="hospital", name="Osmania General Hospital", criticality=1.0,
            latitude=cell.center_latitude, longitude=cell.center_longitude,
        ))
        db.session.commit()

        body = official.get("/api/twin/hyderabad/cell/%s" % cell.h3_index).get_json()
        assert body["assets"][0]["name"] == "Osmania General Hospital"
        assert "Osmania General Hospital" in body["explanation"]


class TestSummaryAndCompare:
    def test_incident_count_24h_excludes_reports_outside_the_city(
            self, official, db, hyderabad, monkeypatch):
        """Regression: found live -- Bengaluru's summary counted Hyderabad's
        demo reports because incident_count_24h never scoped by city."""
        far_away_report = {
            "id": 1, "lat": 12.9716, "lon": 77.5946,  # Bengaluru
            "hazard_type": "flood", "priority": "high", "confidence": 0.9,
            "timestamp": m.utcnow().isoformat(),
        }
        monkeypatch.setattr(
            "twin.routes.InternalReportsAdapter.run",
            lambda self, db_, city_id=None, **kw: ([far_away_report], None))

        body = official.get("/api/twin/hyderabad/summary").get_json()
        assert body["incident_count_24h"] == 0

    def test_summary_aggregates_status_bands(self, official, db, hyderabad):
        c1 = _make_cell(db, hyderabad, h3_index="8860a2b1c3fffff")
        c2 = _make_cell(db, hyderabad, h3_index="8860a2b1c7fffff", lon=78.5)
        _make_state(db, c1, risk_score=10.0, status="normal")
        _make_state(db, c2, risk_score=90.0, status="critical")

        body = official.get("/api/twin/hyderabad/summary").get_json()
        assert body["avg_risk"] == 50.0
        assert body["max_risk"] == 90.0
        assert body["cells_by_status"]["normal"] == 1
        assert body["cells_by_status"]["critical"] == 1
        assert len(body["top_5_cells"]) == 2

    def test_summary_with_no_cells_does_not_crash(self, official):
        body = official.get("/api/twin/hyderabad/summary").get_json()
        assert body["avg_risk"] == 0.0
        assert body["cells_by_status"]["normal"] == 0

    def test_compare_returns_both_cities(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        _make_state(db, cell)

        body = official.get("/api/twin/compare").get_json()
        assert set(body) == {"hyderabad", "bengaluru"}
        assert body["hyderabad"]["avg_risk"] == 78.0


class TestSeedAndRefreshLocking:
    def test_seed_lock_blocks_concurrent_run(self, official, db, hyderabad):
        from twin.routes import _acquire_seed_lock

        assert _acquire_seed_lock(db, "hyderabad") is True
        assert _acquire_seed_lock(db, "hyderabad") is False  # already running

    def test_seed_reports_lock_conflict_through_the_api(self, official, db, hyderabad):
        from twin.routes import _acquire_seed_lock

        _acquire_seed_lock(db, "hyderabad")
        response = official.post("/api/twin/seed", json={"city": "hyderabad"})
        body = response.get_json()
        assert "already in progress" in body["hyderabad"]["error"]

    def test_refresh_calls_engine_and_publishes_on_change(self, official, db, hyderabad, monkeypatch):
        published = []
        monkeypatch.setattr(
            "twin.routes.twin_stream.publish",
            lambda event_type, **kw: published.append((event_type, kw)))
        monkeypatch.setattr(
            "twin.routes.engine.compute_state",
            lambda db_, city: {"city": city.slug, "cells": 1, "changed_cells": ["8860a2b1c3fffff"]})

        response = official.post("/api/twin/refresh", json={"city": "hyderabad"})
        assert response.status_code == 200
        assert response.get_json()["hyderabad"]["changed_cells"] == ["8860a2b1c3fffff"]
        assert published[0][0] == "state_update"

    def test_refresh_unknown_city_reports_error_not_500(self, official):
        response = official.post("/api/twin/refresh", json={"city": "atlantis"})
        assert response.status_code == 200
        assert "error" in response.get_json()["atlantis"]


class TestTrafficRoute:
    """Section 4.2/C5: TomTom is optional and keyed -- absent a key, the
    layer must report unavailable rather than error or claim a fake URL."""

    def test_unavailable_without_a_key(self, official, monkeypatch):
        monkeypatch.setattr("twin.config.TOMTOM_API_KEY", None)
        body = official.get("/api/twin/traffic").get_json()
        assert body == {"available": False, "tile_url_template": None}

    def test_available_with_a_key_returns_a_real_template(self, official, monkeypatch):
        monkeypatch.setattr("twin.config.TOMTOM_API_KEY", "test-key-123")
        body = official.get("/api/twin/traffic").get_json()
        assert body["available"] is True
        assert "{z}" in body["tile_url_template"]
        assert "{x}" in body["tile_url_template"]
        assert "{y}" in body["tile_url_template"]
        assert "test-key-123" in body["tile_url_template"]

    def test_requires_auth(self, anon):
        assert anon.get("/api/twin/traffic").status_code == 401


class TestIncidentsAndInfrastructure:
    def test_incidents_filters_by_hazard_type(self, official, db, hyderabad, monkeypatch):
        import h3

        # Incidents are only counted within cells the city actually has
        # (see twin.routes._reports_within_city) -- a Report carries only
        # lat/lon, so scoping to "this city" means "falls in one of this
        # city's seeded cells", exactly like engine.py's compute_state does.
        reports = [
            {"id": 1, "lat": 17.41, "lon": 78.46, "hazard_type": "flood",
             "priority": "high", "confidence": 0.9, "timestamp": m.utcnow().isoformat()},
            {"id": 2, "lat": 17.42, "lon": 78.47, "hazard_type": "fire",
             "priority": "medium", "confidence": 0.7, "timestamp": m.utcnow().isoformat()},
        ]
        for report in reports:
            _make_cell(db, hyderabad, h3_index=h3.latlng_to_cell(report["lat"], report["lon"], 8),
                      lat=report["lat"], lon=report["lon"])

        monkeypatch.setattr(
            "twin.routes.InternalReportsAdapter.run",
            lambda self, db_, city_id=None, **kw: (reports, None))

        response = official.get("/api/twin/hyderabad/incidents?hazard_type=flood")
        body = response.get_json()
        assert len(body["features"]) == 1
        assert body["features"][0]["properties"]["hazard_type"] == "flood"

    def test_incidents_excludes_reports_outside_the_citys_cells(self, official, db, hyderabad, monkeypatch):
        """The bug this test guards against: a report near a DIFFERENT city
        (or simply outside this city's seeded footprint) must not appear in
        this city's incident feed -- confirmed live: Bengaluru's comparison
        strip showed Hyderabad's demo reports until _reports_within_city
        was added."""
        far_away_report = {
            "id": 99, "lat": 12.9716, "lon": 77.5946,  # Bengaluru, not Hyderabad
            "hazard_type": "flood", "priority": "high", "confidence": 0.9,
            "timestamp": m.utcnow().isoformat(),
        }
        monkeypatch.setattr(
            "twin.routes.InternalReportsAdapter.run",
            lambda self, db_, city_id=None, **kw: ([far_away_report], None))

        response = official.get("/api/twin/hyderabad/incidents")
        assert response.get_json()["features"] == []

    def test_infrastructure_filters_by_type(self, official, db, hyderabad):
        cell = _make_cell(db, hyderabad)
        db.session.add_all([
            m.TwinInfrastructure(city_id=hyderabad.id, cell_id=cell.id, osm_id="a",
                                 asset_type="hospital", criticality=1.0,
                                 latitude=17.4, longitude=78.4),
            m.TwinInfrastructure(city_id=hyderabad.id, cell_id=cell.id, osm_id="b",
                                 asset_type="school", criticality=0.6,
                                 latitude=17.4, longitude=78.4),
        ])
        db.session.commit()

        response = official.get("/api/twin/hyderabad/infrastructure?types=hospital")
        body = response.get_json()
        assert len(body["features"]) == 1
        assert body["features"][0]["properties"]["asset_type"] == "hospital"
