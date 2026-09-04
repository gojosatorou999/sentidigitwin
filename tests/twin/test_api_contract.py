"""Phase 0 API contract: shape, role gating, and cache headers (sections 7, C4)."""

from twin import config


def test_health_shape(official):
    body = official.get("/api/twin/health").get_json()

    assert set(body) >= {"sources", "compute", "enabled", "server_time"}
    # Every declared source appears, even before it has ever run.
    assert set(body["sources"]) == set(config.ALL_SOURCES)
    for key, entry in body["sources"].items():
        assert entry["status"] == "unknown"
        assert entry["tier"] in (1, 2)
    assert body["server_time"].endswith("Z")


def test_health_reports_compute_staleness_before_first_run(official):
    compute = official.get("/api/twin/health").get_json()["compute"]

    # No compute has happened, so the twin must say so rather than imply
    # freshness (section 15, "Stale compute mistaken for live data").
    assert compute["last_computed_at"] is None
    assert compute["stale"] is True
    assert compute["stale_after_seconds"] == (
        config.COMPUTE_INTERVAL_MIN * 60 * config.STALE_COMPUTE_MULTIPLIER)


def test_cities_returns_both_cities_in_configured_order(official):
    body = official.get("/api/twin/cities").get_json()

    assert [c["slug"] for c in body] == list(config.CITY_ORDER)


def test_city_payload_shape(official):
    hyd = official.get("/api/twin/cities").get_json()[0]

    assert set(hyd) >= {
        "slug", "display_name", "center", "bbox", "camera", "zones",
        "last_updated", "health", "zone_scheme", "h3_resolution", "cell_count",
    }
    assert hyd["bbox"] == list(config.CITY_DEFS["hyderabad"]["bbox"])
    assert hyd["camera"] == {"zoom": 10.2, "pitch": 55.0, "bearing": -12.5}
    assert hyd["zone_scheme"] == "GHMC-6"
    assert hyd["h3_resolution"] == config.H3_RESOLUTION
    assert hyd["cell_count"] == 0        # Phase 1 fills the grid


def test_whole_city_is_first_zone_for_every_city(official):
    for city in official.get("/api/twin/cities").get_json():
        assert city["zones"][0]["slug"] == config.ALL_ZONES
        assert city["zones"][0]["display_name"] == "Whole City"


def test_all_configured_zones_are_present(official):
    body = {c["slug"]: c for c in official.get("/api/twin/cities").get_json()}

    for city_slug, zone_defs in config.ZONE_DEFS.items():
        served = {z["slug"] for z in body[city_slug]["zones"]}
        assert served == {config.ALL_ZONES} | {z["slug"] for z in zone_defs}


def test_zones_are_badged_approximate_until_phase_1(official):
    """Officials must be able to see when a boundary is a guess (section 2.3)."""
    hyd = official.get("/api/twin/cities").get_json()[0]

    real_zones = [z for z in hyd["zones"] if z["zone_type"] != "synthetic"]
    assert real_zones
    for zone in real_zones:
        assert zone["boundary_source"] == "approximate"
        assert zone["has_boundary"] is False


def test_cities_sets_cache_headers(official):
    response = official.get("/api/twin/cities")

    assert response.headers["ETag"].startswith('W/"')
    assert "max-age=60" in response.headers["Cache-Control"]


def test_etag_is_stable_across_identical_requests(official):
    first = official.get("/api/twin/cities").headers["ETag"]
    second = official.get("/api/twin/cities").headers["ETag"]

    assert first == second


class TestRoleGating:
    """C4: twin state routes are official/analyst only."""

    def test_anonymous_is_401(self, anon):
        assert anon.get("/api/twin/cities").status_code == 401
        assert anon.get("/api/twin/health").status_code == 401

    def test_wrong_role_is_403_not_404(self, citizen):
        response = citizen.get("/api/twin/cities")

        assert response.status_code == 403
        assert "official" in response.get_json()["error"]

    def test_analyst_is_allowed(self, analyst):
        assert analyst.get("/api/twin/cities").status_code == 200

    def test_health_is_login_gated_but_not_role_gated(self, citizen):
        # An admin without the analyst role still needs ingestion visibility.
        assert citizen.get("/api/twin/health").status_code == 200


class TestRouteAuthEnvelope:
    """Every route in the section 7 contract enforces C4 the same way, even
    with an empty database -- 404 for a bad city/cell must never leak past
    a 401/403 for the wrong caller."""

    ROUTES = (
        "/api/twin/hyderabad/zones",
        "/api/twin/hyderabad/state",
        "/api/twin/hyderabad/cell/8860a2b1c3fffff",
        "/api/twin/hyderabad/incidents",
        "/api/twin/hyderabad/infrastructure",
        "/api/twin/hyderabad/summary",
        "/api/twin/hyderabad/timeline",
        "/api/twin/compare",
    )

    def test_anonymous_rejected_before_any_lookup(self, anon):
        for route in self.ROUTES:
            assert anon.get(route).status_code == 401, route

    def test_official_only_routes_reject_analyst(self, analyst):
        for route in ("/api/twin/refresh", "/api/twin/seed"):
            assert analyst.post(route, json={}).status_code == 403, route

    def test_official_only_routes_accept_official(self, official, monkeypatch):
        # These routes do real ingest/seed work when they succeed (covered
        # by test_state_api.py's monkeypatched versions and by
        # scripts/seed_twin.py's real run); here we only care that an
        # official is let PAST the role gate, so the underlying work is
        # stubbed to keep this a fast, network-free auth test.
        monkeypatch.setattr(
            "twin.routes.engine.compute_state",
            lambda db_, city: {"city": city.slug, "cells": 0})
        monkeypatch.setattr(
            "twin.routes.grid.generate_cells_for_city", lambda db_, city, **kw: 0)
        monkeypatch.setattr("twin.routes.grid.seed_elevation", lambda db_, city, **kw: 0)
        monkeypatch.setattr(
            "twin.routes.grid.seed_infrastructure_and_terrain",
            lambda db_, city, **kw: {"assets": 0, "water_features": 0, "drain_features": 0})
        monkeypatch.setattr("twin.routes.grid.cache_terrain_scores", lambda db_, city: 0)

        for route in ("/api/twin/refresh", "/api/twin/seed"):
            response = official.post(route, json={"city": "hyderabad"})
            assert response.status_code == 200, route
