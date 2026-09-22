"""Official CAP alerts: parsing, geo-resolution, idempotency, expiry.

Nothing here touches the network. The three documents SACHET serves are
pinned as fixtures because their differences are exactly what breaks this
feature in ways that produce no error: the alert document is namespaced and
the polygon document is not, and CAP writes coordinates lat,lon while GeoJSON
writes them lon,lat.

The highest-value test in this file is
``test_repolling_the_same_feed_creates_no_duplicates`` -- SACHET republishes
every live alert on every poll, so without upsert semantics a three-hour
warning becomes 36 rows and the incident sub-score climbs purely because time
passed.
"""

import json
from datetime import timedelta

import h3
import pytest

from twin import alerts as twin_alerts
from twin import config
from twin import models as m
from twin.ingest import sachet

# --------------------------------------------------------------------------
# Fixtures -- real document shapes, trimmed
# --------------------------------------------------------------------------

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Karnataka: CAP Disaster Alert Feeds</title>
    <item>
      <title>Light Thunderstorm with surface wind is likely over Bengaluru Urban</title>
      <category>Met</category>
      <link>https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=1787755032813019</link>
      <author>controlroom@ndma.gov.in (IMD Bengaluru)</author>
      <guid isPermaLink="false">1787755032813019</guid>
      <pubDate>Wed, 26 Aug 2026 14:42:54 GMT</pubDate>
    </item>
  </channel>
</rss>"""

CAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <cap:identifier>IN-1787755032813019_19</cap:identifier>
  <cap:sender>Karnataka-SNDMC</cap:sender>
  <cap:sent>2026-08-26T20:00:00+05:30</cap:sent>
  <cap:status>Actual</cap:status>
  <cap:msgType>Alert</cap:msgType>
  <cap:info>
    <cap:category>Met</cap:category>
    <cap:event>Light Thunderstorm with surface wind</cap:event>
    <cap:urgency>Expected</cap:urgency>
    <cap:severity>Moderate</cap:severity>
    <cap:certainty>Possible</cap:certainty>
    <cap:effective>2026-08-26T20:06:00+05:30</cap:effective>
    <cap:expires>2026-08-26T23:06:00+05:30</cap:expires>
    <cap:headline>Thunderstorm warning for Bengaluru Urban</cap:headline>
    <cap:instruction>Please follow SDMA guidelines.</cap:instruction>
    <cap:parameter>
      <cap:valueName>Polygon URL</cap:valueName>
      <cap:value>https://sachet.ndma.gov.in/cap_public_website/FetchPolygonXMLFile?identifier=1787755032813019</cap:value>
    </cap:parameter>
    <cap:area>
      <cap:areaDesc>Bengaluru Rural,Bengaluru Urban districts of Karnataka</cap:areaDesc>
      <cap:geocode>
        <cap:valueName>LGD District Code</cap:valueName>
        <cap:value>526</cap:value>
      </cap:geocode>
    </cap:area>
  </cap:info>
</cap:alert>"""

#: Deliberately NOT namespaced -- this is how SACHET serves it, and it is the
#: difference that makes a naive parser return nothing at all.
POLYGON_XML = """<?xml version="1.0" encoding="UTF-8"?>
<alert>
  <identifier>IN-1787755032813019_19</identifier>
  <polygon>12.980,77.580 12.980,77.620 12.950,77.620 12.950,77.580 12.980,77.580</polygon>
</alert>"""


class TestRssParsing:
    def test_extracts_items_with_stable_guids(self):
        items = sachet.parse_rss(RSS_XML)
        assert len(items) == 1
        assert items[0]["guid"] == "1787755032813019"
        assert items[0]["link"].endswith("identifier=1787755032813019")

    def test_a_malformed_feed_yields_no_items_rather_than_raising(self):
        # An empty or broken feed is indistinguishable from a quiet one at the
        # parse layer, and a disaster feed being quiet is the normal case.
        assert sachet.parse_rss("<rss><channel>") == []


class TestCapParsing:
    def test_reads_namespaced_fields(self):
        alert = sachet.parse_cap(CAP_XML)
        assert alert["event"] == "Light Thunderstorm with surface wind"
        assert alert["severity"] == "Moderate"
        assert alert["certainty"] == "Possible"
        assert alert["area_desc"].startswith("Bengaluru Rural")

    def test_polygon_url_comes_from_a_parameter_not_the_area_block(self):
        alert = sachet.parse_cap(CAP_XML)
        assert alert["polygon_url"].endswith("identifier=1787755032813019")
        assert alert["polygon_points"] is None  # not inline in this document

    def test_geocodes_are_captured_as_a_list(self):
        # One IMD warning routinely names 23 districts, each its own
        # cap:geocode. Keeping a single value per valueName -- the obvious
        # dict assignment -- silently drops all but the last.
        alert = sachet.parse_cap(CAP_XML)
        assert alert["geocodes"]["LGD District Code"] == ["526"]

    def test_every_district_code_in_a_multi_district_alert_is_kept(self):
        xml = CAP_XML.replace(
            "<cap:value>526</cap:value>",
            "<cap:value>526</cap:value></cap:geocode><cap:geocode>"
            "<cap:valueName>LGD District Code</cap:valueName><cap:value>525</cap:value>")
        alert = sachet.parse_cap(xml)
        assert alert["geocodes"]["LGD District Code"] == ["526", "525"]

    def test_timestamps_keep_their_offset(self):
        alert = sachet.parse_cap(CAP_XML)
        effective = sachet.parse_cap_datetime(alert["effective"])
        # 20:06 IST is 14:36 UTC. Reading it as UTC would place an active
        # warning 5.5 hours in the future -- long enough to look expired.
        assert (effective.hour, effective.minute) == (14, 36)

    def test_a_naive_timestamp_is_read_as_ist_not_utc(self):
        parsed = sachet.parse_cap_datetime("2026-08-26T20:06:00")
        assert (parsed.hour, parsed.minute) == (14, 36)


class TestPolygonParsing:
    def test_parses_the_un_namespaced_polygon_document(self):
        points = sachet.parse_polygon_document(POLYGON_XML)
        assert points is not None
        assert points[0] == (12.980, 77.580)

    def test_geojson_conversion_flips_to_lon_lat(self):
        points = sachet.parse_polygon_document(POLYGON_XML)
        geometry = sachet.cap_polygon_to_geojson(points)
        first = geometry["coordinates"][0][0]
        # CAP said lat=12.98, lon=77.58. GeoJSON must say [77.58, 12.98] --
        # the other order puts Bengaluru in the Indian Ocean, silently.
        assert first == [77.58, 12.98]
        assert first[0] > 70, "longitude must be the first element"

    def test_a_degenerate_ring_is_rejected(self):
        assert sachet.parse_polygon_text("12.9,77.5 12.9,77.6") is None
        assert sachet.cap_polygon_to_geojson([(12.9, 77.5)]) is None

    def test_junk_pairs_are_skipped_not_fatal(self):
        points = sachet.parse_polygon_text("12.9,77.5 banana 12.8,77.6 12.7,77.7")
        assert len(points) == 3


class TestVocabularyMapping:
    @pytest.mark.parametrize("severity,expected", [
        ("Extreme", "critical"), ("Severe", "high"),
        ("Moderate", "medium"), ("Minor", "low"), (None, "low"),
    ])
    def test_cap_severity_maps_to_scoring_priority(self, severity, expected):
        assert sachet.priority_for(severity) == expected

    @pytest.mark.parametrize("certainty,expected", [
        ("Observed", 1.0), ("Likely", 0.75), ("Possible", 0.5), ("Unlikely", 0.25),
    ])
    def test_cap_certainty_maps_to_confidence(self, certainty, expected):
        assert sachet.confidence_for(certainty) == expected

    def test_hazard_family_is_keyword_driven(self):
        assert sachet.hazard_type_for({"event": "Urban flooding in low areas"}) == "flood"
        assert sachet.hazard_type_for({"event": "Thunderstorm with squall"}) == "rain"
        assert sachet.hazard_type_for({"event": "Heat wave conditions"}) == "heat"
        assert sachet.hazard_type_for({"event": "Something novel"}) == "other"


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

@pytest.fixture()
def bengaluru(db):
    return db.session.query(m.TwinCity).filter_by(slug="bengaluru").one()


@pytest.fixture()
def cells_inside_polygon(db, bengaluru):
    """Three real grid cells inside the fixture polygon.

    The alert pipeline intersects a CAP footprint with cells the city actually
    has, so a test with no cells would pass for the wrong reason.
    """
    created = []
    for lat, lon in ((12.965, 77.590), (12.970, 77.600), (12.975, 77.610)):
        index = h3.latlng_to_cell(lat, lon, config.H3_RESOLUTION)
        cell = m.TwinCell(h3_index=index, city_id=bengaluru.id,
                          center_latitude=lat, center_longitude=lon)
        db.session.add(cell)
        created.append(index)
    db.session.commit()
    return created


def _resolved_alert(**overrides):
    """One fully-resolved alert, as the adapter would hand it to alerts.py.

    The validity window is made relative to now. The fixture document carries
    real August timestamps, which is what the parsing tests want, but any test
    about *active* alerts would otherwise silently pass or fail depending on
    the date it is run -- and the correct behaviour for a long-past warning is
    to be excluded.
    """
    alert = sachet.parse_cap(CAP_XML)
    alert["polygon_points"] = sachet.parse_polygon_document(POLYGON_XML)
    alert["geometry"] = sachet.cap_polygon_to_geojson(alert["polygon_points"])
    alert["geometry_kind"] = "polygon"
    alert["source_uid"] = "1787755032813019"
    alert["raw_url"] = "https://sachet.ndma.gov.in/x"
    alert["effective"] = (m.utcnow() - timedelta(minutes=30)).isoformat()
    alert["expires"] = (m.utcnow() + timedelta(hours=2)).isoformat()
    alert.update(overrides)
    return alert


class TestGeoResolution:
    def test_polygon_resolves_to_this_city_s_cells(self, db, bengaluru, cells_inside_polygon):
        geometry = sachet.cap_polygon_to_geojson(sachet.parse_polygon_document(POLYGON_XML))
        cells = twin_alerts.cells_for_geometry(db, bengaluru, geometry)
        assert set(cells) == set(cells_inside_polygon)

    def test_cells_outside_the_grid_are_not_invented(self, db, bengaluru):
        # No TwinCell rows exist for this city in this test, so a perfectly
        # valid polygon must resolve to nothing rather than to cells the map
        # cannot draw.
        geometry = sachet.cap_polygon_to_geojson(sachet.parse_polygon_document(POLYGON_XML))
        assert twin_alerts.cells_for_geometry(db, bengaluru, geometry) == set()


class TestUpsert:
    def test_an_alert_is_stored_with_its_cells(self, db, bengaluru, cells_inside_polygon):
        outcome = twin_alerts._upsert_alert(db, bengaluru, _resolved_alert())
        db.session.commit()

        assert outcome == "created"
        row = db.session.query(m.TwinExternalAlert).one()
        assert row.event.startswith("Light Thunderstorm")
        assert row.geometry_kind == "polygon"
        assert json.loads(row.geometry_geojson)["type"] == "Polygon"
        assert db.session.query(m.TwinAlertCell).count() == len(cells_inside_polygon)

    def test_repolling_the_same_feed_creates_no_duplicates(self, db, bengaluru,
                                                           cells_inside_polygon):
        for _ in range(5):
            twin_alerts._upsert_alert(db, bengaluru, _resolved_alert())
            db.session.commit()

        assert db.session.query(m.TwinExternalAlert).count() == 1
        assert db.session.query(m.TwinAlertCell).count() == len(cells_inside_polygon)

    def test_a_shrinking_footprint_drops_the_cells_it_left(self, db, bengaluru,
                                                           cells_inside_polygon):
        twin_alerts._upsert_alert(db, bengaluru, _resolved_alert())
        db.session.commit()

        # Same alert, re-issued over a smaller area.
        tight = sachet.cap_polygon_to_geojson([
            (12.9665, 77.5885), (12.9665, 77.5915), (12.9635, 77.5915), (12.9635, 77.5885)])
        twin_alerts._upsert_alert(db, bengaluru, _resolved_alert(geometry=tight))
        db.session.commit()

        remaining = {row.h3_index for row in db.session.query(m.TwinAlertCell).all()}
        assert remaining == {h3.latlng_to_cell(12.965, 77.590, config.H3_RESOLUTION)}

    def test_an_alert_for_somewhere_else_is_discarded(self, db, bengaluru,
                                                      cells_inside_polygon):
        elsewhere = sachet.cap_polygon_to_geojson([
            (28.60, 77.20), (28.60, 77.25), (28.55, 77.25), (28.55, 77.20)])
        outcome = twin_alerts._upsert_alert(db, bengaluru, _resolved_alert(geometry=elsewhere))
        db.session.commit()

        assert outcome == "skipped"
        assert db.session.query(m.TwinExternalAlert).count() == 0

    def test_a_polygonless_alert_is_kept_when_it_names_the_city(self, db, bengaluru):
        alert = _resolved_alert(geometry=None, geometry_kind="district")
        outcome = twin_alerts._upsert_alert(db, bengaluru, alert)
        db.session.commit()

        assert outcome == "created"
        row = db.session.query(m.TwinExternalAlert).one()
        assert row.geometry_kind == "district"
        # A district advisory must NOT be exploded across the whole grid.
        assert db.session.query(m.TwinAlertCell).count() == 0

    def test_a_polygonless_alert_naming_another_district_is_discarded(self, db, bengaluru):
        alert = _resolved_alert(
            geometry=None, geometry_kind="district", geocodes={},
            area_desc="Kalaburagi district of Karnataka",
            headline="Heat wave over Kalaburagi", rss_title=None, description=None)
        assert twin_alerts._upsert_alert(db, bengaluru, alert) == "skipped"

    def test_an_lgd_code_matches_a_city_even_when_no_name_is_given(self, db, bengaluru):
        # The common real case: "11 districts of Karnataka", naming nobody.
        # The geocode list is then the only way to know this city is in it.
        alert = _resolved_alert(
            geometry=None, geometry_kind="district",
            area_desc="11 districts of Karnataka",
            headline=None, rss_title=None, description=None,
            geocodes={"LGD District Code": ["548", "525", "738"]})

        assert twin_alerts._upsert_alert(db, bengaluru, alert) == "created"
        row = db.session.query(m.TwinExternalAlert).one()
        assert row.raw["area_confidence"] == "district_code"

    def test_a_state_wide_alert_is_kept_but_labelled_as_inferred(self, db, bengaluru):
        alert = _resolved_alert(
            geometry=None, geometry_kind="district", geocodes={},
            area_desc="23 districts of Karnataka",
            headline=None, rss_title=None, description=None)

        assert twin_alerts._upsert_alert(db, bengaluru, alert) == "created"
        row = db.session.query(m.TwinExternalAlert).one()
        # Kept, because a warning over most of the state almost certainly
        # covers its capital -- but the card must say the geography was
        # inferred rather than stated.
        assert row.raw["area_confidence"] == "state_wide"

    def test_a_state_wide_alert_for_another_state_is_discarded(self, db, bengaluru):
        alert = _resolved_alert(
            geometry=None, geometry_kind="district", geocodes={},
            area_desc="14 districts of Kerala",
            headline=None, rss_title=None, description=None)
        assert twin_alerts._upsert_alert(db, bengaluru, alert) == "skipped"


class TestSupersession:
    def test_an_update_supersedes_the_alert_it_references(self, db, bengaluru,
                                                          cells_inside_polygon):
        twin_alerts._upsert_alert(db, bengaluru, _resolved_alert())
        update = _resolved_alert(
            source_uid="1787755032813020",
            identifier="IN-1787755032813020_19",
            msg_type="Update",
            references="Karnataka-SNDMC,IN-1787755032813019_19,2026-08-26T20:00:00+05:30")
        twin_alerts._upsert_alert(db, bengaluru, update)
        db.session.commit()

        superseded = twin_alerts._apply_supersessions(db, bengaluru)
        db.session.commit()

        assert superseded == 1
        active = twin_alerts.active_alerts(db, bengaluru)
        assert [row.cap_identifier for row in active] == ["IN-1787755032813020_19"]

    def test_reference_parsing_takes_the_identifier_from_the_triple(self):
        uids = twin_alerts._reference_uids(
            "sender,IN-ONE,2026-08-26T20:00:00+05:30 sender,IN-TWO,2026-08-26T21:00:00+05:30")
        assert uids == {"IN-ONE", "IN-TWO"}


class TestExpiry:
    def test_an_expired_alert_stops_being_active(self, db, bengaluru, cells_inside_polygon):
        twin_alerts._upsert_alert(db, bengaluru, _resolved_alert())
        db.session.commit()

        row = db.session.query(m.TwinExternalAlert).one()
        assert len(twin_alerts.active_alerts(db, bengaluru, now=row.expires_at
                                             - timedelta(minutes=1))) == 1
        # cap:expires is authoritative: past it, the warning is over. Leaving
        # it in place is worse than having none, because it still looks current.
        assert twin_alerts.active_alerts(db, bengaluru,
                                         now=row.expires_at + timedelta(minutes=1)) == []

    def test_feature_collection_separates_drawable_from_advisory(self, db, bengaluru,
                                                                 cells_inside_polygon):
        twin_alerts._upsert_alert(db, bengaluru, _resolved_alert())
        twin_alerts._upsert_alert(db, bengaluru, _resolved_alert(
            source_uid="advisory-1", geometry=None, geometry_kind="district"))
        db.session.commit()

        collection = twin_alerts.alerts_feature_collection(db, bengaluru)
        assert len(collection["features"]) == 1
        assert len(collection["advisories"]) == 1
        assert "SACHET" in collection["attribution"]


class TestRoute:
    def test_alerts_route_returns_a_feature_collection(self, analyst, db, bengaluru,
                                                       cells_inside_polygon):
        twin_alerts._upsert_alert(db, bengaluru, _resolved_alert())
        db.session.commit()

        response = analyst.get("/api/twin/bengaluru/alerts")
        assert response.status_code == 200
        body = response.get_json()
        assert body["type"] == "FeatureCollection"
        assert body["features"][0]["properties"]["hazard_type"] == "rain"

    def test_alerts_route_requires_a_role(self, anon):
        assert anon.get("/api/twin/bengaluru/alerts").status_code in (401, 403)

    def test_unknown_city_is_404(self, analyst):
        assert analyst.get("/api/twin/atlantis/alerts").status_code == 404
