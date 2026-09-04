"""Street-level imagery adapter and route.

Every provider here is a free tier, and two of the three need a key this
deployment may not have, so the behaviour worth pinning down is not "does it
return photos" -- it is what happens when it cannot. The panel must always
hand the operator *some* way to look at the place, and must always say which
provider it did or did not use.

No test in this file touches the network; the providers are stubbed.
"""

import json

import pytest
import requests

from twin.ingest import streetview as sv


class _Response:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("%d" % self.status_code)


def _kartaview_payload(rows):
    return {"status": {"httpCode": 200}, "result": {"data": rows}}


def _kartaview_row(photo_id="1", distance="12.5", lat="17.36", lng="78.47"):
    # Every numeric field really does come back as a string from KartaView.
    return {
        "id": photo_id,
        "distance": distance,
        "lat": lat,
        "lng": lng,
        "headers": "276.69",
        "shotDate": "2020-05-11 09:09:21.000",
        "dateAdded": "2020-05-11 09:58:48",
        "sequenceId": "2220454",
        "sequenceIndex": "2",
        "imageProcUrl": "https://cdn.kartaview.org/proc/%s.jpg" % photo_id,
        "imageLthUrl": "https://cdn.kartaview.org/lth/%s.jpg" % photo_id,
        "fileurlProc": "https://storage13.openstreetcam.org/proc/%s.jpg" % photo_id,
    }


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    """A StreetViewAdapter with an isolated cache directory.

    Without this the 24-hour disk cache is shared with the running app's,
    and a test would either read a real cached lookup or poison one.
    """
    monkeypatch.setattr(sv.config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(sv.config, "MAPILLARY_TOKEN", None)
    monkeypatch.setattr(sv.config, "WINDY_WEBCAMS_KEY", None)
    return sv.StreetViewAdapter()


class TestKeylessDefault:
    def test_kartaview_is_used_when_no_token_is_configured(self, db, adapter, monkeypatch):
        monkeypatch.setattr(adapter.session, "get",
                            lambda *a, **k: _Response(_kartaview_payload([_kartaview_row()])))

        data, snapshot = adapter.run(db, lat=17.36, lon=78.47)

        assert snapshot.status == "ok"
        assert len(data["images"]) == 1
        image = data["images"][0]
        assert image["provider"] == "kartaview"
        assert image["distance_m"] == 12.5          # parsed out of the string
        assert image["captured_at"] == "2020-05-11"  # date only, not a timestamp
        assert data["providers"]["mapillary"].startswith("no MAPILLARY_TOKEN")
        assert data["providers"]["windy_webcams"].startswith("no WINDY_WEBCAMS_KEY")

    def test_a_deep_link_is_returned_even_with_no_coverage(self, db, adapter, monkeypatch):
        monkeypatch.setattr(adapter.session, "get",
                            lambda *a, **k: _Response(_kartaview_payload([])))

        data, _snapshot = adapter.run(db, lat=12.97, lon=77.59)

        assert data["images"] == []
        assert "map_action=pano" in data["google_streetview_url"]
        assert "12.970000,77.590000" in data["google_streetview_url"]

    def test_empty_result_retries_once_at_a_wider_radius(self, db, adapter, monkeypatch):
        radii = []

        def fake_get(url, params=None, **kwargs):
            radii.append(params["radius"])
            # Nothing at the tight radius, one hit at the wide one.
            return _Response(_kartaview_payload(
                [] if params["radius"] < sv.WIDEN_RADIUS_M else [_kartaview_row()]))

        monkeypatch.setattr(adapter.session, "get", fake_get)
        data, _snapshot = adapter.run(db, lat=17.36, lon=78.47)

        assert radii == [sv.DEFAULT_RADIUS_M, sv.WIDEN_RADIUS_M]
        assert len(data["images"]) == 1

    def test_a_hit_at_the_tight_radius_does_not_widen(self, db, adapter, monkeypatch):
        radii = []

        def fake_get(url, params=None, **kwargs):
            radii.append(params["radius"])
            return _Response(_kartaview_payload([_kartaview_row()]))

        monkeypatch.setattr(adapter.session, "get", fake_get)
        adapter.run(db, lat=17.36, lon=78.47)

        assert radii == [sv.DEFAULT_RADIUS_M]


class TestDegradation:
    def test_a_dead_provider_degrades_to_an_empty_list_not_an_exception(
            self, db, adapter, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError("simulated network kill")

        monkeypatch.setattr(adapter.session, "get", boom)
        data, snapshot = adapter.run(db, lat=17.36, lon=78.47)

        # The lookup itself still succeeded -- it is the *provider* that
        # failed, and the panel must be able to say so and still offer the
        # keyless deep link.
        assert snapshot.status == "ok"
        assert data["images"] == []
        assert data["providers"]["kartaview"].startswith("error:")
        assert data["google_streetview_url"]

    def test_missing_coordinates_are_rejected_before_any_request(self, db, adapter, monkeypatch):
        monkeypatch.setattr(adapter.session, "get",
                            lambda *a, **k: pytest.fail("should not reach the network"))

        data, snapshot = adapter.run(db, lat=None, lon=None)

        assert snapshot.status == "failed"
        assert data is None

    def test_cache_key_ignores_the_db_handle(self, adapter):
        """The base class hashes every kwarg including `db`, whose repr
        carries a memory address -- which would silently invalidate the
        24-hour cache on every process restart."""
        key_a = adapter._cache_key(None, {"lat": 17.36, "lon": 78.47,
                                          "radius_m": 350, "db": object()})
        key_b = adapter._cache_key(None, {"lat": 17.36, "lon": 78.47,
                                          "radius_m": 350, "db": object()})
        key_far = adapter._cache_key(None, {"lat": 12.97, "lon": 77.59, "radius_m": 350})

        assert key_a == key_b
        assert key_a != key_far


class TestRoute:
    def test_requires_a_twin_role(self, anon, citizen):
        assert anon.get("/api/twin/streetview?lat=17.36&lon=78.47").status_code in (401, 403)
        assert citizen.get("/api/twin/streetview?lat=17.36&lon=78.47").status_code == 403

    def test_rejects_missing_or_malformed_coordinates(self, analyst):
        assert analyst.get("/api/twin/streetview").status_code == 400
        assert analyst.get("/api/twin/streetview?lat=abc&lon=78.47").status_code == 400
        assert analyst.get("/api/twin/streetview?lat=200&lon=78.47").status_code == 400

    def test_clamps_an_absurd_radius(self, analyst, monkeypatch, tmp_path):
        monkeypatch.setattr(sv.config, "CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(sv.config, "MAPILLARY_TOKEN", None)
        monkeypatch.setattr(sv.config, "WINDY_WEBCAMS_KEY", None)
        seen = {}

        def fake_fetch(self, lat=None, lon=None, radius_m=None, **_):
            seen["radius_m"] = radius_m
            return {"lat": lat, "lon": lon, "radius_m": radius_m, "images": [],
                    "webcams": [], "providers": {},
                    "google_streetview_url": sv.google_streetview_url(lat, lon),
                    "osm_url": sv.osm_url(lat, lon)}

        monkeypatch.setattr(sv.StreetViewAdapter, "fetch_raw", fake_fetch)

        response = analyst.get("/api/twin/streetview?lat=17.36&lon=78.47&radius=999999")

        assert response.status_code == 200
        assert seen["radius_m"] == 2000
        assert json.loads(response.data)["status"] == "ok"
