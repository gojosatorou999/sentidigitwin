"""Water bodies + drains as a map layer, and the twin blueprint's gzip.

The Overpass terrain query already ran weekly to derive one scalar per cell
(``TwinCell.drain_length_m``); the geometry it fetched was computed and
thrown away, so the console's "Water & drains" toggle switched a layer no
endpoint could populate. These tests pin down the two things that turns on:
how a raw Overpass way becomes a GeoJSON feature, and the fact that the
resulting payload -- the largest the twin serves -- goes out compressed.

No test in this file touches the network; Overpass is stubbed.
"""

import gzip
import json

import pytest

from twin.ingest import overpass


@pytest.fixture()
def adapter(db):
    """An OverpassWaterAdapter. The shared `app` fixture redirects the disk
    cache into tmp_path, so nothing here reads or poisons the running app's
    weekly water cache."""
    return overpass.OverpassWaterAdapter()


def _way(way_id=1, tags=None, points=((77.59, 12.97), (77.60, 12.98))):
    return {"type": "way", "id": way_id, "tags": tags or {},
            "geometry": [{"lat": lat, "lon": lon} for lon, lat in points]}


def _ring(way_id=2, tags=None):
    points = ((77.59, 12.97), (77.60, 12.97), (77.60, 12.98),
              (77.59, 12.98), (77.59, 12.97))
    return _way(way_id, tags or {"natural": "water"}, points)


def _stub_overpass(monkeypatch, elements):
    monkeypatch.setattr(overpass, "run_overpass_query",
                        lambda session, query, timeout: {"elements": elements})


# --------------------------------------------------------------------------
# Feature conversion
# --------------------------------------------------------------------------

class TestFeatureConversion:
    def test_closed_water_body_becomes_a_polygon(self):
        feature = overpass._element_to_water_feature(_ring())
        assert feature["geometry"]["type"] == "Polygon"
        assert feature["properties"]["kind"] == "water"

    def test_closed_waterway_stays_a_line(self):
        # A ring canal is closed but is still a channel, not a lake: the
        # geometry type follows the tag, not just whether the ring closes.
        feature = overpass._element_to_water_feature(
            _ring(tags={"waterway": "canal"}))
        assert feature["geometry"]["type"] == "LineString"
        assert feature["properties"]["kind"] == "canal"

    @pytest.mark.parametrize("waterway,kind", [
        ("drain", "drain"), ("ditch", "drain"), ("canal", "canal"),
        ("stream", "stream"), ("river", "river"),
    ])
    def test_waterway_kinds(self, waterway, kind):
        feature = overpass._element_to_water_feature(
            _way(tags={"waterway": waterway}))
        assert feature["properties"]["kind"] == kind

    def test_coordinates_are_lon_lat_and_rounded(self):
        element = _way(points=((77.5946123456, 12.9716123456),
                               (77.6046123456, 12.9816123456)))
        coords = overpass._element_to_water_feature(element)["geometry"]["coordinates"]
        # _way_geometry hands back [lat, lon]; GeoJSON needs [lon, lat], and
        # getting that backwards puts every drain in the Indian Ocean.
        assert coords[0] == [77.59461, 12.97161]

    def test_only_styled_tags_are_carried_through(self):
        feature = overpass._element_to_water_feature(_way(tags={
            "waterway": "drain", "name": "Koramangala Valley",
            "source": "survey", "note": "resurveyed 2021"}))
        assert feature["properties"]["name"] == "Koramangala Valley"
        assert "source" not in feature["properties"]
        assert "note" not in feature["properties"]

    def test_degenerate_geometry_is_dropped(self):
        assert overpass._element_to_water_feature(
            _way(points=((77.59, 12.97),))) is None
        assert overpass._element_to_water_feature(
            {"type": "way", "id": 3, "tags": {"natural": "water"}}) is None


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------

class TestAdapter:
    def test_returns_a_feature_collection(self, db, adapter, monkeypatch):
        _stub_overpass(monkeypatch, [_ring(), _way(tags={"waterway": "drain"})])

        data, snapshot = adapter.run(db, bbox=(77.4, 12.8, 77.8, 13.2))

        assert snapshot.status == "ok"
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 2
        assert "OpenStreetMap" in data["attribution"]

    def test_overpass_failure_degrades_to_an_empty_collection(self, db, adapter, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("overpass 504")
        monkeypatch.setattr(overpass, "run_overpass_query", boom)

        data, snapshot = adapter.run(db, bbox=(77.4, 12.8, 77.8, 13.2))

        assert snapshot.status == "failed"
        assert data["features"] == []
        assert data["unavailable"] is True

    def test_cache_key_ignores_the_db_handle(self, adapter):
        bbox = (77.4, 12.8, 77.8, 13.2)
        assert (adapter._cache_key(None, {"bbox": bbox, "db": object()})
                == adapter._cache_key(None, {"bbox": bbox, "db": object()}))

    def test_cache_key_separates_cities(self, adapter):
        assert (adapter._cache_key(None, {"bbox": (77.4, 12.8, 77.8, 13.2)})
                != adapter._cache_key(None, {"bbox": (78.2, 17.2, 78.7, 17.6)}))


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------

class TestRoute:
    def test_serves_geojson(self, official, monkeypatch):
        _stub_overpass(monkeypatch, [_ring(), _way(tags={"waterway": "drain"})])
        response = official.get("/api/twin/hyderabad/water")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["type"] == "FeatureCollection"
        assert len(payload["features"]) == 2

    def test_unknown_city_is_404(self, official):
        assert official.get("/api/twin/atlantis/water").status_code == 404

    def test_unknown_zone_is_404(self, official):
        assert official.get("/api/twin/hyderabad/water?zone=nowhere").status_code == 404

    def test_anonymous_callers_are_refused(self, anon):
        assert anon.get("/api/twin/hyderabad/water").status_code in (401, 403)

    def test_a_line_crossing_the_zone_is_kept(self):
        # Containment would be the wrong test for a drain: the one an
        # operator cares about is precisely the one running through the zone
        # and out the other side.
        from twin.routes import _feature_overlaps

        crossing = {"geometry": {"type": "LineString",
                                 "coordinates": [[77.0, 12.9], [78.0, 12.9]]}}
        assert _feature_overlaps(crossing, (77.5, 12.8, 77.6, 13.0)) is True

    def test_a_feature_outside_the_zone_is_dropped(self):
        from twin.routes import _feature_overlaps

        elsewhere = {"geometry": {"type": "LineString",
                                  "coordinates": [[80.0, 12.9], [80.1, 12.9]]}}
        assert _feature_overlaps(elsewhere, (77.5, 12.8, 77.6, 13.0)) is False


# --------------------------------------------------------------------------
# Blueprint-scoped compression
# --------------------------------------------------------------------------

class TestGzip:
    def _big_payload(self, monkeypatch):
        # Enough vertices to clear the 8 KB floor comfortably.
        points = tuple((77.5 + i / 10000.0, 12.9 + i / 10000.0) for i in range(600))
        _stub_overpass(monkeypatch, [_way(1, {"waterway": "drain"}, points)])

    def test_large_json_is_gzipped_and_decodes(self, official, monkeypatch):
        self._big_payload(monkeypatch)
        response = official.get("/api/twin/hyderabad/water",
                                headers={"Accept-Encoding": "gzip"})
        assert response.headers["Content-Encoding"] == "gzip"
        assert "Accept-Encoding" in response.headers["Vary"]
        payload = json.loads(gzip.decompress(response.get_data()))
        assert payload["type"] == "FeatureCollection"

    def test_content_length_matches_the_compressed_body(self, official, monkeypatch):
        # A stale Content-Length from before compression truncates the body
        # for any client that trusts the header.
        self._big_payload(monkeypatch)
        response = official.get("/api/twin/hyderabad/water",
                                headers={"Accept-Encoding": "gzip"})
        assert int(response.headers["Content-Length"]) == len(response.get_data())

    def test_a_client_that_does_not_accept_gzip_gets_plain_json(self, official, monkeypatch):
        self._big_payload(monkeypatch)
        response = official.get("/api/twin/hyderabad/water",
                                headers={"Accept-Encoding": "identity"})
        assert response.headers.get("Content-Encoding") is None
        assert response.get_json()["type"] == "FeatureCollection"

    def test_small_json_is_left_alone(self, official):
        response = official.get("/api/twin/health", headers={"Accept-Encoding": "gzip"})
        assert response.headers.get("Content-Encoding") is None

    def test_the_sse_stream_is_never_buffered(self, official):
        # /stream is an endless generator; compressing it would mean reading
        # it to completion first.
        response = official.get("/api/twin/stream?city=hyderabad",
                                headers={"Accept-Encoding": "gzip"},
                                buffered=False)
        assert response.headers.get("Content-Encoding") is None
        assert response.mimetype == "text/event-stream"
        response.close()
