"""OSINT surveillance-camera adapter and its two routes.

The interesting behaviour here is not "does Overpass answer" -- it is tag
interpretation. OSM's surveillance vocabulary is loose in practice: a
``direction`` may be a compass point, a sweep range or a list; a
``man_made=surveillance`` node may describe a human guard post rather than a
camera; and the great majority of mapped cameras carry nothing but a
position. Getting any of that wrong shows up as a wrong arrow on a map or a
guard post presented as a camera feed, so each case is pinned down here.

No test in this file touches the network; Overpass is stubbed.
"""

import json

import pytest

from twin.ingest import cctv


@pytest.fixture()
def adapter(db):
    """A CctvOsintAdapter. The disk cache is redirected into tmp_path by the
    shared `app` fixture, so nothing here can read or poison the running
    app's weekly camera cache."""
    return cctv.CctvOsintAdapter()


def _node(node_id=1, lat=12.97, lon=77.59, **tags):
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon,
            "tags": dict({"man_made": "surveillance"}, **tags)}


def _stub_overpass(monkeypatch, elements):
    monkeypatch.setattr(cctv, "run_overpass_query",
                        lambda session, query, timeout: {"elements": elements})


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------

class TestQuery:
    def test_bbox_query_uses_overpass_south_west_north_east_order(self):
        query = cctv.build_cctv_query(bbox=(77.44, 12.83, 77.78, 13.14))
        # Overpass wants (south, west, north, east); the twin stores
        # (min_lon, min_lat, max_lon, max_lat). Swapping these silently
        # returns cameras from the wrong place rather than failing.
        assert "12.830000,77.440000,13.140000,77.780000" in query

    def test_around_query_is_metres_lat_lon(self):
        query = cctv.build_cctv_query(around=(12.97, 77.59, 400))
        assert "around:400,12.970000,77.590000" in query

    def test_exactly_one_scope_is_required(self):
        with pytest.raises(ValueError):
            cctv.build_cctv_query()
        with pytest.raises(ValueError):
            cctv.build_cctv_query(bbox=(0, 0, 1, 1), around=(0, 0, 100))

    def test_speed_cameras_and_enforcement_are_queried_too(self):
        query = cctv.build_cctv_query(bbox=(0, 0, 1, 1))
        assert '"highway"="speed_camera"' in query
        assert '["enforcement"]' in query


# --------------------------------------------------------------------------
# Tag interpretation
# --------------------------------------------------------------------------

class TestDirection:
    @pytest.mark.parametrize("raw,expected", [
        ("135", 135.0),
        (270, 270.0),
        ("NW", 315.0),
        ("north", 0.0),
        ("90-180", 90.0),      # a panning camera's sweep -> where it starts
        ("N;E", 0.0),          # a list -> its first bearing
        ("450", 90.0),         # normalised into [0, 360)
        ("", None),
        ("sideways", None),
        (None, None),
    ])
    def test_parse_direction(self, raw, expected):
        assert cctv._parse_direction(raw) == expected


class TestClassification:
    def test_guard_posts_are_not_cameras(self):
        assert cctv._element_to_camera(
            _node(**{"surveillance:type": "guard"})) is None

    def test_alpr_is_a_camera(self):
        camera = cctv._element_to_camera(_node(**{"surveillance:type": "ALPR"}))
        assert camera is not None

    def test_speed_camera_without_man_made_tag_is_included(self):
        element = {"type": "node", "id": 7, "lat": 12.9, "lon": 77.6,
                   "tags": {"highway": "speed_camera"}}
        camera = cctv._element_to_camera(element)
        assert camera is not None
        assert camera["kind"] == "traffic"

    @pytest.mark.parametrize("tags,kind", [
        ({"surveillance": "public"}, "public"),
        ({"surveillance": "indoor"}, "indoor"),
        ({"surveillance:zone": "traffic"}, "traffic"),
        ({"surveillance:zone": "parking"}, "traffic"),
        ({"surveillance:zone": "street"}, "public"),
        ({"enforcement": "maxspeed"}, "traffic"),
        ({}, "unknown"),
    ])
    def test_kind(self, tags, kind):
        assert cctv._element_to_camera(_node(**tags))["kind"] == kind

    def test_way_uses_its_center_point(self):
        element = {"type": "way", "id": 9, "center": {"lat": 13.0, "lon": 77.5},
                   "tags": {"man_made": "surveillance"}}
        camera = cctv._element_to_camera(element)
        assert (camera["lat"], camera["lon"]) == (13.0, 77.5)
        assert camera["osm_url"].endswith("/way/9")

    def test_element_without_a_position_is_dropped(self):
        assert cctv._element_to_camera(
            {"type": "way", "id": 3, "tags": {"man_made": "surveillance"}}) is None


class TestPublicFeedUrl:
    def test_documented_webcam_tags_are_used(self):
        camera = cctv._element_to_camera(
            _node(**{"contact:webcam": "https://example.org/live.m3u8"}))
        assert camera["stream_url"] == "https://example.org/live.m3u8"

    def test_website_is_not_treated_as_a_feed(self):
        # `website` on a camera node is normally the operator's corporate
        # site; offering it as "public feed" would be a lie in the UI.
        camera = cctv._element_to_camera(_node(website="https://police.example"))
        assert camera["stream_url"] is None

    def test_non_http_value_is_ignored(self):
        camera = cctv._element_to_camera(_node(webcam="yes"))
        assert camera["stream_url"] is None


# --------------------------------------------------------------------------
# Adapter behaviour
# --------------------------------------------------------------------------

class TestAdapter:
    def test_point_lookup_sorts_by_distance_and_measures_it(self, db, adapter, monkeypatch):
        _stub_overpass(monkeypatch, [
            _node(1, lat=12.9800, lon=77.5900),   # ~330 m north
            _node(2, lat=12.9705, lon=77.5900),   # ~55 m south
        ])

        data, snapshot = adapter.run(db, lat=12.9710, lon=77.5900, radius_m=400)

        assert snapshot.status == "ok"
        ids = [c["osm_id"] for c in data["cameras"]]
        assert ids == ["node/2", "node/1"]
        assert data["cameras"][0]["distance_m"] < data["cameras"][1]["distance_m"]

    def test_bbox_lookup_has_no_distance(self, db, adapter, monkeypatch):
        _stub_overpass(monkeypatch, [_node(1)])
        data, _ = adapter.run(db, bbox=(77.4, 12.8, 77.8, 13.2))
        assert "distance_m" not in data["cameras"][0]

    def test_counts_by_kind(self, db, adapter, monkeypatch):
        _stub_overpass(monkeypatch, [
            _node(1, surveillance="public"),
            _node(2, surveillance="public"),
            _node(3, **{"surveillance:zone": "traffic"}),
        ])
        data, _ = adapter.run(db, bbox=(0, 0, 1, 1))
        assert data["counts_by_kind"] == {"public": 2, "traffic": 1}

    def test_overpass_failure_degrades_to_an_empty_labelled_set(self, db, adapter, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("overpass 429")
        monkeypatch.setattr(cctv, "run_overpass_query", boom)

        data, snapshot = adapter.run(db, lat=12.97, lon=77.59)

        # C1: a dead Tier-2 source costs one empty layer, never an exception.
        assert snapshot.status == "failed"
        assert data["cameras"] == []
        assert data["unavailable"] is True
        assert "OpenStreetMap" in data["attribution"]

    def test_cache_key_ignores_the_db_handle(self, adapter):
        # The base implementation hashes every kwarg including `db`, whose
        # repr embeds a memory address -- which would change the key on every
        # process restart and defeat a weekly TTL entirely.
        key_a = adapter._cache_key(None, {"lat": 12.97, "lon": 77.59, "db": object()})
        key_b = adapter._cache_key(None, {"lat": 12.97, "lon": 77.59, "db": object()})
        assert key_a == key_b

    def test_cache_key_separates_bbox_from_point(self, adapter):
        assert (adapter._cache_key(None, {"bbox": (0, 0, 1, 1)})
                != adapter._cache_key(None, {"lat": 0, "lon": 0}))


class TestFeatureCollection:
    def test_geojson_is_lon_lat_with_null_tags_dropped(self):
        data = {"cameras": [{"osm_id": "node/1", "lat": 12.97, "lon": 77.59,
                             "kind": "public", "operator": None, "direction": 90.0}],
                "attribution": "(c) OpenStreetMap contributors (ODbL)",
                "counts_by_kind": {"public": 1}}

        fc = cctv.to_feature_collection(data)

        feature = fc["features"][0]
        assert feature["geometry"]["coordinates"] == [77.59, 12.97]
        assert feature["properties"]["direction"] == 90.0
        assert "operator" not in feature["properties"]
        assert "lat" not in feature["properties"]
        assert fc["attribution"].startswith("(c) OpenStreetMap")

    def test_empty_data_is_still_a_valid_feature_collection(self):
        fc = cctv.to_feature_collection(None)
        assert fc["type"] == "FeatureCollection"
        assert fc["features"] == []


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

class TestRoutes:
    def test_city_cameras_returns_geojson(self, official, monkeypatch):
        _stub_overpass(monkeypatch, [_node(1, surveillance="public")])
        response = official.get("/api/twin/hyderabad/cameras")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["type"] == "FeatureCollection"
        assert payload["status"] in ("ok", "degraded", "failed")

    def test_unknown_city_is_404(self, official):
        assert official.get("/api/twin/atlantis/cameras").status_code == 404

    def test_unknown_zone_is_404(self, official):
        assert official.get(
            "/api/twin/hyderabad/cameras?zone=nowhere").status_code == 404

    def test_point_lookup_requires_coordinates(self, official):
        assert official.get("/api/twin/cctv").status_code == 400
        assert official.get("/api/twin/cctv?lat=abc&lon=1").status_code == 400

    def test_point_lookup_rejects_out_of_range_coordinates(self, official):
        assert official.get("/api/twin/cctv?lat=99&lon=0").status_code == 400

    def test_radius_is_clamped(self, official, monkeypatch):
        _stub_overpass(monkeypatch, [])
        payload = official.get("/api/twin/cctv?lat=12.97&lon=77.59&radius=99999").get_json()
        assert payload["radius_m"] == 2000

    def test_anonymous_callers_are_refused(self, anon):
        assert anon.get("/api/twin/cctv?lat=12.97&lon=77.59").status_code in (401, 403)
        assert anon.get("/api/twin/hyderabad/cameras").status_code in (401, 403)

    def test_citizens_are_refused(self, citizen):
        assert citizen.get("/api/twin/hyderabad/cameras").status_code == 403


class TestZoneBounds:
    """The camera layer's zone filter, which cannot rely on cell assignment.

    A default seed leaves every zone `boundary_source='approximate'` with a
    centre point and almost no cells carrying its zone_id, so the filter has
    to fall through to progressively weaker bases -- and must never guess a
    box so tight that it hides most of a city's cameras.
    """

    def test_geojson_bounds_of_a_polygon(self):
        from twin.routes import _geojson_bounds
        raw = json.dumps({"type": "Polygon", "coordinates": [
            [[77.5, 12.9], [77.7, 12.9], [77.7, 13.1], [77.5, 13.1], [77.5, 12.9]]]})
        assert _geojson_bounds(raw) == (77.5, 12.9, 77.7, 13.1)

    def test_geojson_bounds_of_a_multipolygon(self):
        from twin.routes import _geojson_bounds
        raw = json.dumps({"type": "MultiPolygon", "coordinates": [
            [[[77.5, 12.9], [77.6, 12.9], [77.6, 13.0], [77.5, 12.9]]],
            [[[77.8, 13.2], [77.9, 13.2], [77.9, 13.3], [77.8, 13.2]]]]})
        assert _geojson_bounds(raw) == (77.5, 12.9, 77.9, 13.3)

    def test_unreadable_geojson_is_none(self):
        from twin.routes import _geojson_bounds
        assert _geojson_bounds("not json") is None
        assert _geojson_bounds(json.dumps({"type": "Polygon"})) is None

    def test_falls_back_to_a_box_around_an_approximate_zone_centre(self, app, db):
        from twin import models as m
        from twin.routes import _zone_bounds, _ZONE_FALLBACK_KM

        with app.app_context():
            city = db.session.query(m.TwinCity).filter_by(slug="bengaluru").one()
            zone = (db.session.query(m.TwinZone)
                    .filter_by(city_id=city.id).first())
            bounds = _zone_bounds(db, city, zone)

        assert bounds is not None
        min_lon, min_lat, max_lon, max_lat = bounds
        assert min_lat < zone.center_latitude < max_lat
        assert min_lon < zone.center_longitude < max_lon
        # Roughly the fallback half-width, not a degenerate point.
        assert (max_lat - min_lat) == pytest.approx(2 * _ZONE_FALLBACK_KM / 111.0, rel=0.01)
