"""Live point sources: AQI maths, feed folding, persistence, transit signals.

The index maths is tested hardest here. ``twin/ingest/stations.py`` computes
the Indian national AQI itself rather than trusting whichever number a
provider labels "AQI", because US EPA and CPCB indices disagree substantially
at the concentrations these cities actually see -- and a colour ramp fed by
two different national scales is wrong in a way that is invisible on screen.

No test in this file touches the network.
"""

import json
from datetime import timedelta

import pytest

from twin import cameras as twin_cameras
from twin import config
from twin import live as twin_live
from twin import models as m
from twin.ingest import stations, transit


@pytest.fixture()
def bengaluru(db):
    return db.session.query(m.TwinCity).filter_by(slug="bengaluru").one()


# --------------------------------------------------------------------------
# CPCB national AQI
# --------------------------------------------------------------------------

class TestCpcbIndex:
    @pytest.mark.parametrize("concentration,expected", [
        (0, 0), (30, 50), (60, 100), (90, 200), (120, 300), (250, 400),
    ])
    def test_pm25_breakpoints_are_exact(self, concentration, expected):
        assert stations.cpcb_sub_index("PM2.5", concentration) == expected

    def test_interpolates_within_a_band(self):
        # Halfway between 30 (index 50) and 60 (index 100).
        assert stations.cpcb_sub_index("pm2.5", 45) == pytest.approx(75.5, abs=1.0)

    def test_aqi_is_the_worst_sub_index_not_an_average(self):
        # Averaging is the standard way to under-report a PM2.5 event: here a
        # severe PM2.5 reading sits beside three clean ones.
        aqi, dominant = stations.cpcb_aqi(
            {"pm2.5": 200, "no2": 10, "so2": 5, "o3": 20})
        assert dominant == "pm2.5"
        assert aqi > 300

    def test_unknown_pollutants_are_ignored_rather_than_guessed(self):
        assert stations.cpcb_sub_index("radon", 12) is None
        assert stations.cpcb_aqi({"radon": 12}) == (None, None)

    def test_readings_above_the_scale_clamp_at_500(self):
        # The index simply stops at 500; extrapolating to 900 would imply a
        # precision the scale does not have.
        assert stations.cpcb_sub_index("pm2.5", 5000) == 500.0

    def test_band_labels_match_cpcb_vocabulary(self):
        assert stations.cpcb_band(45) == "Good"
        assert stations.cpcb_band(150) == "Moderate"
        assert stations.cpcb_band(450) == "Severe"


class TestCpcbFolding:
    def _records(self):
        return [
            {"city": "Bengaluru", "station": "BTM Layout", "latitude": "12.9121",
             "longitude": "77.5937", "pollutant_id": "PM2.5", "pollutant_avg": "78",
             "last_update": "17-09-2026 10:00:00"},
            {"city": "Bengaluru", "station": "BTM Layout", "latitude": "12.9121",
             "longitude": "77.5937", "pollutant_id": "NO2", "pollutant_avg": "22",
             "last_update": "17-09-2026 10:00:00"},
            {"city": "Bengaluru", "station": "BTM Layout", "latitude": "12.9121",
             "longitude": "77.5937", "pollutant_id": "SO2", "pollutant_avg": "NA",
             "last_update": "17-09-2026 10:00:00"},
        ]

    def test_per_pollutant_rows_fold_into_one_station(self):
        folded = stations._fold_cpcb_records(self._records())
        assert len(folded) == 1
        assert folded[0]["name"] == "BTM Layout"
        assert folded[0]["metrics"]["pm2.5"] == 78.0

    def test_na_readings_stay_absent_rather_than_becoming_zero(self):
        # "NA" is how this feed says a station reported nothing this hour.
        # Storing it as 0 would read as pristine air.
        folded = stations._fold_cpcb_records(self._records())
        assert "so2" not in folded[0]["metrics"]

    def test_station_value_is_the_cpcb_index(self):
        folded = stations._fold_cpcb_records(self._records())
        assert folded[0]["unit"] == "AQI (CPCB)"
        assert folded[0]["dominant_pollutant"] == "pm2.5"
        assert 150 < folded[0]["value"] < 200

    def test_rows_without_coordinates_are_dropped(self):
        assert stations._fold_cpcb_records(
            [{"city": "X", "station": "No position", "pollutant_id": "PM2.5",
              "pollutant_avg": "10"}]) == []

    def test_ist_timestamps_are_converted_not_assumed_utc(self):
        folded = stations._fold_cpcb_records(self._records())
        # 10:00 IST is 04:30 UTC.
        assert folded[0]["observed_at"].startswith("2026-09-17T04:30")

    def test_observations_are_json_serialisable(self):
        # IngestAdapter json-dumps whatever fetch_raw returns into its disk
        # cache; a datetime in there is reported by run() as a failed fetch.
        json.dumps(stations._fold_cpcb_records(self._records()))


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _observation(uid="s1", lat=12.97, lon=77.59, value=120.0, kind="air_quality",
                 observed_at=None):
    return {
        "source_key": "cpcb_aqi", "station_uid": uid, "kind": kind,
        "name": "Test station", "lat": lat, "lon": lon, "value": value,
        "unit": "AQI (CPCB)", "metrics": {"pm2.5": 55},
        "observed_at": (observed_at or m.utcnow()).isoformat(),
    }


class TestPersistence:
    def test_observations_are_stored_with_an_h3_cell(self, db, bengaluru):
        twin_live.persist_observations(db, bengaluru, [_observation()])
        row = db.session.query(m.TwinObservation).one()
        assert row.h3_index
        assert row.city_id == bengaluru.id
        assert row.status == "ok"

    def test_repolling_updates_in_place(self, db, bengaluru):
        twin_live.persist_observations(db, bengaluru, [_observation(value=100.0)])
        twin_live.persist_observations(db, bengaluru, [_observation(value=180.0)])

        assert db.session.query(m.TwinObservation).count() == 1
        assert db.session.query(m.TwinObservation).one().value == 180.0

    def test_points_outside_the_city_bbox_are_dropped(self, db, bengaluru):
        bbox = (bengaluru.bbox_min_lon, bengaluru.bbox_min_lat,
                bengaluru.bbox_max_lon, bengaluru.bbox_max_lat)
        # Two Indian cities sharing a name is common enough that a national
        # feed filtered by name returns the odd station 400 km away.
        written = twin_live.persist_observations(
            db, bengaluru, [_observation(uid="far", lat=28.6, lon=77.2)], bbox=bbox)
        assert written == 0
        assert db.session.query(m.TwinObservation).count() == 0

    def test_a_stale_reading_is_labelled_stale_not_hidden(self, db, bengaluru):
        old = m.utcnow() - timedelta(hours=6)
        twin_live.persist_observations(db, bengaluru, [_observation(observed_at=old)])
        assert db.session.query(m.TwinObservation).one().status == "stale"

    def test_feature_collection_reports_age_server_side(self, db, bengaluru):
        twin_live.persist_observations(
            db, bengaluru, [_observation(observed_at=m.utcnow() - timedelta(minutes=5))])
        collection = twin_live.observations_feature_collection(
            db, bengaluru, kind="air_quality")

        properties = collection["features"][0]["properties"]
        # Age is computed on the server because the operator's clock may be
        # minutes off, and a negative age reads as a bug in the data.
        assert 290 <= properties["age_seconds"] <= 310
        assert collection["features"][0]["geometry"]["coordinates"][0] == 77.59


# --------------------------------------------------------------------------
# Transit
# --------------------------------------------------------------------------

class TestTransitSignals:
    def _vehicle(self, uid, lat, lon, speed, status=None, minutes_ago=0):
        return {
            "lat": lat, "lon": lon, "value": speed,
            "metrics": {"current_status": status},
            "observed_at": (m.utcnow() - timedelta(minutes=minutes_ago)).isoformat(),
        }

    def _h3_for(self, lat, lon):
        import h3
        return h3.latlng_to_cell(lat, lon, config.H3_RESOLUTION)

    def test_stopped_vehicles_raise_the_cell_stall_rate(self):
        vehicles = [
            self._vehicle("a", 12.97, 77.59, 0.0),
            self._vehicle("b", 12.9701, 77.5901, 1.0),
            self._vehicle("c", 12.9702, 77.5902, 30.0),
        ]
        rates = transit.stall_rate_by_cell(vehicles, h3_for=self._h3_for)
        cell = self._h3_for(12.97, 77.59)
        assert rates[cell]["vehicles"] == 3
        assert rates[cell]["stalled"] == 2
        assert rates[cell]["stall_rate"] == pytest.approx(2 / 3)

    def test_a_vehicle_that_stopped_reporting_counts_as_stalled(self):
        # A bus in floodwater stops reporting. Treating silence as "fine" is
        # exactly backwards -- that is the signal, not the absence of one.
        vehicles = [self._vehicle("a", 12.97, 77.59, 40.0, minutes_ago=45)]
        rates = transit.stall_rate_by_cell(vehicles, h3_for=self._h3_for)
        assert rates[self._h3_for(12.97, 77.59)]["stall_rate"] == 1.0

    def test_gtfs_rt_json_feeds_are_accepted(self):
        payload = {"entity": [{
            "id": "v1",
            "vehicle": {
                "trip": {"routeId": "500A", "tripId": "t1"},
                "position": {"latitude": 12.97, "longitude": 77.59,
                             "speed": 5.0, "bearing": 90},
                "vehicle": {"label": "KA01F1234"},
                "timestamp": 1789000000,
                "currentStatus": "IN_TRANSIT_TO",
            },
        }]}
        vehicles = transit._vehicles_from_json(payload)
        assert len(vehicles) == 1
        assert vehicles[0]["kind"] == "transit_vehicle"
        # GTFS-RT speed is metres/second; the layer shows km/h.
        assert vehicles[0]["value"] == pytest.approx(18.0)
        assert vehicles[0]["metrics"]["route_id"] == "500A"

    def test_an_unconfigured_feed_is_not_an_error(self, db, bengaluru, monkeypatch):
        monkeypatch.setattr(config, "GTFS_RT_URLS", {})
        result = twin_live.refresh_transit(db, bengaluru)
        assert result["status"] == "not_configured"
        assert result["written"] == 0


# --------------------------------------------------------------------------
# Operator-supplied camera streams
# --------------------------------------------------------------------------

class TestCameraStreams:
    def test_no_config_file_means_no_streams_not_an_error(self, tmp_path):
        assert twin_cameras.configured_streams(path=str(tmp_path / "absent.json")) == []

    def test_valid_streams_load(self, tmp_path):
        path = tmp_path / "streams.json"
        path.write_text(json.dumps([{
            "id": "iccc-1", "name": "Charminar junction", "city": "hyderabad",
            "lat": 17.36, "lon": 78.47, "direction": 90,
            "url": "https://example.gov.in/a.m3u8", "type": "hls",
            "operator": "GHMC ICCC",
        }]), encoding="utf-8")

        streams = twin_cameras.configured_streams(path=str(path))
        assert len(streams) == 1
        assert streams[0]["type"] == "hls"
        assert streams[0]["city"] == "hyderabad"

    def test_non_http_urls_are_rejected(self, tmp_path):
        # This file is hand-edited during incidents and is handed straight to
        # the browser, so a javascript: or file: URL must never survive load.
        path = tmp_path / "streams.json"
        path.write_text(json.dumps([
            {"id": "x", "url": "javascript:alert(1)", "type": "iframe"},
            {"id": "y", "url": "file:///etc/passwd", "type": "image"},
        ]), encoding="utf-8")
        assert twin_cameras.configured_streams(path=str(path)) == []

    def test_unknown_player_types_are_rejected(self, tmp_path):
        path = tmp_path / "streams.json"
        path.write_text(json.dumps([
            {"id": "x", "url": "https://example.com/a", "type": "rtsp"}]),
            encoding="utf-8")
        assert twin_cameras.configured_streams(path=str(path)) == []

    def test_a_malformed_file_costs_one_panel_not_the_console(self, tmp_path):
        path = tmp_path / "streams.json"
        path.write_text("{not json,,,", encoding="utf-8")
        assert twin_cameras.configured_streams(path=str(path)) == []

    def test_streams_filter_by_city(self, tmp_path):
        path = tmp_path / "streams.json"
        path.write_text(json.dumps([
            {"id": "a", "city": "hyderabad", "url": "https://e/a.m3u8", "type": "hls"},
            {"id": "b", "city": "bengaluru", "url": "https://e/b.m3u8", "type": "hls"},
        ]), encoding="utf-8")

        streams = twin_cameras.configured_streams(city_slug="bengaluru", path=str(path))
        assert [s["id"] for s in streams] == ["b"]


class TestLiveRoutes:
    def test_live_layer_route_returns_geojson(self, analyst, db, bengaluru):
        twin_live.persist_observations(db, bengaluru, [_observation()])
        response = analyst.get("/api/twin/bengaluru/live/air")
        assert response.status_code == 200
        assert response.get_json()["type"] == "FeatureCollection"

    def test_unknown_kind_is_rejected(self, analyst):
        assert analyst.get("/api/twin/bengaluru/live/everything").status_code == 400

    def test_live_summary_lists_each_layer(self, analyst, db, bengaluru):
        body = analyst.get("/api/twin/bengaluru/live").get_json()
        assert set(body["layers"]) >= {"air_quality", "transit_vehicle", "alerts"}

    def test_stream_route_is_honest_when_nothing_is_configured(self, analyst, monkeypatch):
        monkeypatch.setattr(config, "CCTV_STREAMS_FILE", "does-not-exist.json")
        body = analyst.get("/api/twin/cctv/streams").get_json()
        assert body["configured"] is False
        assert body["streams"] == []
        assert "never proxies" in body["note"]

    def test_stream_route_without_a_city_calls_no_provider(self, analyst, monkeypatch):
        """An unscoped request must not pull eight national camera catalogs."""
        monkeypatch.setattr(config, "CCTV_STREAMS_FILE", "does-not-exist.json")
        body = analyst.get("/api/twin/cctv/streams").get_json()
        assert body["providers"]["considered"] == []
        assert body["provider_count"] == 0

    def test_stream_route_names_the_providers_it_considered(self, analyst, db, bengaluru):
        """Bengaluru is covered by no authority, and the panel must say which
        authorities were weighed rather than leaving an operator guessing."""
        body = analyst.get("/api/twin/cctv/streams?city=bengaluru").get_json()
        assert body["providers"]["considered"] == []
        assert body["streams"] == []

    def test_provider_registry_route_explains_an_empty_panel(self, analyst, db, bengaluru):
        body = analyst.get("/api/twin/cctv/providers?city=bengaluru").get_json()
        assert body["matching_count"] == 0
        assert len(body["providers"]) >= 8
        assert all(p["covers_city"] is False for p in body["providers"])
