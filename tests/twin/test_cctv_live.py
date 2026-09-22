"""Live camera-catalog providers: the coverage gate and the liveness filters.

Two classes of behaviour matter here, and both fail silently if they regress.

The **coverage gate** is what keeps the layer honest. A provider that is
fetched outside its service area would put Helsinki's cameras on a Hyderabad
map, and an operator reading that panel has no way to tell. An empty panel is
a correct answer; a foreign camera is a wrong one, so the gate is pinned from
both directions.

The **liveness filters** are the other half. Every catalog publishes rows for
cameras that are planned, retired or offline, and each of those rows still
carries a frame URL that still returns an image -- just one from a camera
that was decommissioned, or frozen years ago. Dropping those rows is the
whole reason to parse a status field, so each provider's filter is tested
against a row it must reject.

No test here touches the network: every payload is a fixture shaped like the
upstream's real response.
"""

import pytest

from twin.ingest import cctv_live


# --------------------------------------------------------------------------
# Coverage gate
# --------------------------------------------------------------------------

HYDERABAD_BBOX = (78.24, 17.22, 78.66, 17.60)
BENGALURU_BBOX = (77.44, 12.83, 77.78, 13.14)
LONDON_BBOX = (-0.20, 51.40, 0.00, 51.60)


def test_no_provider_covers_the_modelled_cities():
    """The load-bearing assertion of the whole module.

    If this ever fails, some provider has been given a coverage box wide
    enough to swallow India, and the console will start showing a foreign
    authority's cameras as though they watched Hyderabad.
    """
    assert cctv_live.providers_for(HYDERABAD_BBOX) == ()
    assert cctv_live.providers_for(BENGALURU_BBOX) == ()


def test_provider_matches_its_own_city():
    keys = [p.key for p in cctv_live.providers_for(LONDON_BBOX)]
    assert keys == ["tfl"]


def test_coverage_pad_admits_the_edge_of_a_service_area():
    """An operator panning to a city's edge should not lose its cameras."""
    tfl = next(p for p in cctv_live.PROVIDERS if p.key == "tfl")
    just_outside = (0.36, 51.30, 0.45, 51.40)      # east of the declared box
    assert tfl.covers(just_outside) is True
    assert tfl.covers(just_outside, pad_deg=0.0) is False


def test_disabled_registry_yields_nothing(monkeypatch):
    monkeypatch.setattr(cctv_live.config, "CCTV_LIVE_ENABLED", False)
    assert cctv_live.enabled_providers() == ()


def test_provider_allowlist_narrows_the_registry(monkeypatch):
    monkeypatch.setattr(cctv_live.config, "CCTV_LIVE_PROVIDERS", "tfl, calgary")
    assert [p.key for p in cctv_live.enabled_providers()] == ["tfl", "calgary"]


# --------------------------------------------------------------------------
# make_camera: what may and may not reach the console
# --------------------------------------------------------------------------

def _camera(**kwargs):
    defaults = dict(camera_id="x-1", name="Test", lat=51.5, lon=-0.1,
                    url="https://example.gov/frame.jpg", provider="Test Authority",
                    license_note="Test licence")
    defaults.update(kwargs)
    return cctv_live.make_camera(**defaults)


def test_camera_without_a_position_is_dropped():
    assert _camera(lat=None) is None
    assert _camera(lat="", lon="") is None


def test_empty_coordinate_does_not_become_null_island():
    """``float('')`` raising, not returning 0.0, is the point."""
    assert cctv_live._num("") is None
    assert cctv_live._num(None) is None
    assert cctv_live._num(True) is None          # bools are not coordinates


def test_insecure_or_unplayable_feeds_are_dropped():
    assert _camera(url="http://example.gov/frame.jpg") is None
    assert _camera(url="javascript:alert(1)") is None
    assert _camera(feed_type="youtube") is None  # no provider produces this


def test_missing_heading_is_marked_low_confidence_not_invented():
    camera = _camera()
    assert camera["heading_confidence"] == "low"
    assert 0 <= camera["direction"] < 360
    # Stable across calls, so a camera's cone does not move between refreshes.
    assert camera["direction"] == _camera()["direction"]


def test_published_heading_is_marked_high_confidence():
    camera = _camera(heading=90)
    assert camera["heading_confidence"] == "high"
    assert camera["direction"] == 90.0


def test_headingless_cameras_do_not_stack_on_one_bearing():
    bearings = {cctv_live.fallback_heading("cam-%d" % i) for i in range(40)}
    assert len(bearings) > 20


# --------------------------------------------------------------------------
# Heading parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("West", 270.0), ("Southbound", 180.0), ("NE", 45.0), ("135", 135.0),
])
def test_dedicated_direction_field_accepts_bare_cardinals(text, expected):
    assert cctv_live.heading_from_text(text, True) == expected


@pytest.mark.parametrize("name", ["N Lamar Blvd", "West Ave / 5th", "South Congress"])
def test_a_street_name_is_never_read_as_a_bearing(name):
    """Texas and Austin route names are full of cardinals. Reading them as
    headings would point a large share of a city's cones at a street's name."""
    assert cctv_live.heading_from_text(name, False) is None


def test_travel_token_inside_a_name_is_read():
    assert cctv_live.heading_from_text("US-290 EB @ Parmer", False) == 90.0


# --------------------------------------------------------------------------
# Per-provider liveness filters
# --------------------------------------------------------------------------

def _austin_payload(status="TURNED_ON", camera_id="7"):
    return {
        "meta": {"view": {"columns": [
            {"fieldName": "camera_id"}, {"fieldName": "location_name"},
            {"fieldName": "camera_status"}, {"fieldName": "screenshot_address"},
            {"fieldName": "location"}]}},
        "data": [[camera_id, "AIRPORT BLVD / OAK SPRINGS DR", status,
                  "https://cctv.austinmobility.io/image/%s.jpg" % camera_id,
                  "POINT (-97.6979 30.2736)"]],
    }


def test_austin_keeps_live_cameras_and_reads_wkt():
    cameras = cctv_live.parse_austin(_austin_payload())
    assert len(cameras) == 1
    assert cameras[0]["lat"] == pytest.approx(30.2736)
    assert cameras[0]["lon"] == pytest.approx(-97.6979)


@pytest.mark.parametrize("status", ["DESIRED", "REMOVED", "VOID"])
def test_austin_drops_cameras_that_were_never_built_or_are_gone(status):
    """These rows carry working-looking frame URLs; only the status says no."""
    assert cctv_live.parse_austin(_austin_payload(status=status)) == []


def test_austin_keeps_a_row_whose_status_column_vanished():
    """A schema change must cost a status filter, not the whole city."""
    assert len(cctv_live.parse_austin(_austin_payload(status=""))) == 1


def _caltrans_payload(in_service="true", image="https://cwwp2.dot.ca.gov/d3/a.jpg"):
    return {"data": [{"cctv": {
        "inService": in_service,
        "location": {"latitude": "38.48", "longitude": "-121.51",
                     "locationName": "TV102 -- I-5 : at Pocket",
                     "nearbyPlace": "Sacramento", "direction": "West",
                     "elevation": "30"},
        "imageData": {"static": {"currentImageURL": image}}}}]}


def test_caltrans_keeps_in_service_cameras():
    cameras = cctv_live.parse_caltrans(_caltrans_payload(), 3)
    assert len(cameras) == 1
    assert cameras[0]["direction"] == 270.0
    assert cameras[0]["heading_confidence"] == "high"


def test_caltrans_drops_out_of_service_cameras():
    assert cctv_live.parse_caltrans(_caltrans_payload(in_service="false"), 3) == []


def test_caltrans_frame_url_must_be_on_the_official_host():
    """The one field the upstream controls freely, and it reaches a browser."""
    payload = _caltrans_payload(image="https://evil.example/a.jpg")
    assert cctv_live.parse_caltrans(payload, 3) == []


def test_caltrans_elevation_is_converted_from_feet():
    camera = cctv_live.parse_caltrans(_caltrans_payload(), 3)[0]
    assert camera["ground_elevation_m"] == pytest.approx(30 * 0.3048)


def _tfl_payload(available="true", image=None):
    image = image or (cctv_live.TFL_FRAME_ORIGIN + "00001.00002.jpg")
    return [{"id": "JamCams_00001.00002", "commonName": "A406 Billet",
             "lat": 51.6007, "lon": -0.0159,
             "additionalProperties": [{"key": "available", "value": available},
                                      {"key": "imageUrl", "value": image}]}]


def test_tfl_keeps_available_cameras_and_strips_the_id_prefix():
    cameras = cctv_live.parse_tfl(_tfl_payload())
    assert [c["id"] for c in cameras] == ["tfl-00001.00002"]


def test_tfl_drops_unavailable_cameras():
    assert cctv_live.parse_tfl(_tfl_payload(available="false")) == []


def test_tfl_frame_url_must_be_on_the_official_bucket():
    assert cctv_live.parse_tfl(_tfl_payload(image="https://evil.example/a.jpg")) == []


def _ontario_payload(status="Enabled", url="https://511on.ca/map/Cctv/42"):
    return [{"Id": "42", "Location": "QEW West of Thompson", "Direction": "Unknown",
             "Latitude": 43.3, "Longitude": -79.8,
             "Views": [{"Status": status, "Url": url, "Description": "QEW Toronto Bound"}]}]


def test_ontario_keeps_enabled_views_and_rebuilds_the_url():
    cameras = cctv_live.parse_ontario(_ontario_payload())
    assert cameras[0]["url"] == cctv_live.ONTARIO_FRAME_ORIGIN + "42"


def test_ontario_drops_disabled_views():
    assert cctv_live.parse_ontario(_ontario_payload(status="Disabled")) == []


def test_ontario_refuses_a_view_url_on_an_unofficial_host():
    assert cctv_live.parse_ontario(_ontario_payload(url="https://evil.example/map/Cctv/42")) == []


def test_ontario_unknown_direction_stays_low_confidence():
    """Every Ontario row publishes Direction='Unknown'. Reading that as a
    bearing would put a province's worth of cones on a made-up heading."""
    assert cctv_live.parse_ontario(_ontario_payload())[0]["heading_confidence"] == "low"


def _fintraffic_payload(collection="GATHERING", in_collection=True, preset="C0150301"):
    return {"features": [{
        "geometry": {"coordinates": [23.9962, 60.0537, 0]},
        "properties": {"id": "C01503", "name": "kt51_Inkoo",
                       "collectionStatus": collection,
                       "presets": [{"id": preset, "inCollection": in_collection}]}}]}


def test_fintraffic_keeps_gathering_stations():
    cameras = cctv_live.parse_fintraffic(_fintraffic_payload())
    assert cameras[0]["url"] == cctv_live.FINTRAFFIC_FRAME_ORIGIN + "C0150301.jpg"


def test_fintraffic_drops_stations_that_stopped_collecting():
    assert cctv_live.parse_fintraffic(
        _fintraffic_payload(collection="REMOVED_TEMPORARILY")) == []


def test_fintraffic_drops_presets_out_of_collection():
    assert cctv_live.parse_fintraffic(_fintraffic_payload(in_collection=False)) == []


def test_fintraffic_rejects_a_preset_id_that_could_escape_the_frame_path():
    assert cctv_live.parse_fintraffic(_fintraffic_payload(preset="../../etc/passwd")) == []


def test_fintraffic_zero_altitude_means_unreported_not_sea_level():
    camera = cctv_live.parse_fintraffic(_fintraffic_payload())[0]
    assert camera["ground_elevation_m"] == cctv_live.FINTRAFFIC_DEFAULT_ELEVATION_M


def _drivebc_payload(is_on=True, should_appear=True, credit=""):
    return [{"id": 854, "name": "Mountain Highway", "is_on": is_on,
             "should_appear": should_appear, "orientation": "S",
             "elevation": 120, "region_name": "Lower Mainland", "credit": credit,
             "location": {"coordinates": [-123.0377, 49.3157]}}]


def test_drivebc_keeps_published_cameras():
    cameras = cctv_live.parse_drivebc(_drivebc_payload())
    assert cameras[0]["direction"] == 180.0
    assert cameras[0]["url"] == "https://www.drivebc.ca/images/854.jpg"


@pytest.mark.parametrize("kwargs", [{"is_on": False}, {"should_appear": False}])
def test_drivebc_drops_cameras_that_are_off_or_unpublished(kwargs):
    assert cctv_live.parse_drivebc(_drivebc_payload(**kwargs)) == []


def test_drivebc_carries_partner_attribution_but_not_operational_notes():
    attributed = cctv_live.parse_drivebc(
        _drivebc_payload(credit="Images courtesy of TransLink"))[0]
    assert attributed["credit"] == "Images courtesy of TransLink"

    noted = cctv_live.parse_drivebc(
        _drivebc_payload(credit="This camera relies on solar power"))[0]
    assert noted["credit"] is None


def _nsw_payload(href=None):
    href = href or (cctv_live.NSW_FRAME_ORIGIN + "cameras/5_ways.jpeg")
    return {"features": [{"geometry": {"coordinates": [151.1053, -34.0298]},
                          "properties": {"title": "5 Ways (Miranda)",
                                         "view": "Looking north", "href": href}}]}


def test_nsw_keeps_cameras_on_the_official_host():
    assert len(cctv_live.parse_nsw(_nsw_payload())) == 1


def test_nsw_drops_a_frame_url_off_host():
    assert cctv_live.parse_nsw(_nsw_payload(href="https://evil.example/a.jpg")) == []


def test_nsw_ignores_a_works_notice_masquerading_as_a_view_label():
    payload = _nsw_payload()
    payload["features"][0]["properties"]["view"] = "x" * 400
    camera = cctv_live.parse_nsw(payload)[0]
    assert camera["name"] == "5 Ways (Miranda)"


def _calgary_payload(url="http://trafficcam.calgary.ca/loc86.jpg"):
    return [{"camera_url": {"url": url}, "camera_location": "Stoney Tr / Deerfoot Tr SE",
             "point": {"coordinates": [-113.9766, 50.9007]}}]


def test_calgary_upgrades_http_frames_to_https():
    camera = cctv_live.parse_calgary(_calgary_payload())[0]
    assert camera["url"] == "https://trafficcam.calgary.ca/loc86.jpg"


def test_calgary_drops_a_frame_url_off_host():
    assert cctv_live.parse_calgary(
        _calgary_payload(url="http://evil.example/loc86.jpg")) == []


# --------------------------------------------------------------------------
# Hong Kong (Transport Department) -- the only non-JSON catalog in the
# registry, and the one whose frame URL is rebuilt from a key.
# --------------------------------------------------------------------------

def _hk_row(**over):
    row = {
        "key": "H429F",
        "region": "Hong Kong Island",
        "district": "Southern",
        "description": "Aberdeen Praya Road near Fish Market [H429F]",
        "latitude": "22.24845",
        "longitude": "114.1505",
        "url": "https://tdcctv.data.one.gov.hk/H429F.JPG",
    }
    row.update(over)
    return [row]


def test_hongkong_builds_the_frame_url_from_the_key():
    camera = cctv_live.parse_hongkong(_hk_row())[0]
    assert camera["url"] == "https://tdcctv.data.one.gov.hk/H429F.JPG"


def test_hongkong_ignores_the_published_url_column():
    """Rule 3: the frame is built, never copied, even when a url is offered.

    The catalog *does* carry a url column, which makes copying it the
    tempting shortcut. Pinning this stops a later edit from taking it.
    """
    camera = cctv_live.parse_hongkong(
        _hk_row(url="https://evil.example/pwn.JPG"))[0]
    assert camera["url"] == "https://tdcctv.data.one.gov.hk/H429F.JPG"


@pytest.mark.parametrize("key", ["H429F", "H422F2", "ST712F1", "AID01101",
                                 "TSWAID01101"])
def test_hongkong_accepts_every_published_key_shape(key):
    """An earlier pattern modelled on H429F alone dropped 812 of 1,013 rows."""
    cameras = cctv_live.parse_hongkong(_hk_row(key=key))
    assert len(cameras) == 1
    assert cameras[0]["url"] == "https://tdcctv.data.one.gov.hk/%s.JPG" % key


@pytest.mark.parametrize("key", ["", "../../etc/passwd", "H429F?x=1",
                                 "H429F/../x", "H4", "a" * 20, "H429F.JPG"])
def test_hongkong_rejects_a_key_that_could_escape_the_origin(key):
    assert cctv_live.parse_hongkong(_hk_row(key=key)) == []


def test_hongkong_strips_the_key_back_out_of_the_label():
    camera = cctv_live.parse_hongkong(_hk_row())[0]
    assert camera["name"] == "Aberdeen Praya Road near Fish Market, Southern"


def test_hongkong_decodes_the_utf16_tab_separated_catalog():
    """The BOM inside the decoded text renamed the first column and
    silently failed every row until it was stripped.

    Built with explicit separators rather than escapes so the fixture
    cannot drift from what the codec is actually handed.
    """
    tab, newline, bom = chr(9), chr(10), chr(65279)
    header = tab.join(('key', 'district', 'description',
                       'latitude', 'longitude'))
    row = tab.join(('H429F', 'Southern', 'Aberdeen Praya Road [H429F]',
                    '22.24845', '114.1505'))
    body = bom + header + newline + row + newline

    rows = cctv_live._read_hongkong_catalog(body.encode('utf-16'))

    assert rows[0]['key'] == 'H429F'
    assert cctv_live.parse_hongkong(rows)[0]['code'] == 'H429F'

# --------------------------------------------------------------------------
# Singapore -- the documented exception to "build, never copy"
# --------------------------------------------------------------------------

def _sg_payload(image="https://images.data.gov.sg/api/traffic-images/x.jpg"):
    return {"items": [{"cameras": [{
        "camera_id": "2701",
        "image": image,
        "location": {"latitude": 1.29531332, "longitude": 103.871146},
    }]}]}


def test_singapore_keeps_a_frame_url_on_the_authority_host():
    camera = cctv_live.parse_singapore(_sg_payload())[0]
    assert camera["url"].startswith("https://images.data.gov.sg/")
    assert camera["code"] == "2701"


@pytest.mark.parametrize("image", [
    "https://evil.example/api/traffic-images/x.jpg",
    "http://images.data.gov.sg/api/traffic-images/x.jpg",
    "https://images.data.gov.sg.evil.example/x.jpg",
    "",
])
def test_singapore_drops_a_frame_url_off_host(image):
    """The origin pin is the only guarantee here, so it is tested hard.

    Singapore republishes each frame under a fresh UUID, so unlike every
    other provider the URL genuinely cannot be rebuilt from the camera id --
    which makes this check the whole of the contract rather than a backstop.
    """
    assert cctv_live.parse_singapore(_sg_payload(image=image)) == []


def test_singapore_survives_an_empty_payload():
    assert cctv_live.parse_singapore({}) == []
    assert cctv_live.parse_singapore({"items": []}) == []


# --------------------------------------------------------------------------
# Both new providers are still coverage-gated
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bbox", [HYDERABAD_BBOX, BENGALURU_BBOX])
def test_the_asian_providers_do_not_reach_india(bbox):
    """Nearer than Helsinki is still not local, and the gate does not care."""
    keys = {p.key for p in cctv_live.providers_for(bbox)}
    assert "hongkong" not in keys
    assert "singapore" not in keys


def test_hong_kong_and_singapore_cover_their_own_ground():
    hk = {p.key for p in cctv_live.providers_for((114.10, 22.25, 114.20, 22.35))}
    sg = {p.key for p in cctv_live.providers_for((103.80, 1.28, 103.90, 1.36))}
    assert "hongkong" in hk
    assert "singapore" in sg


# --------------------------------------------------------------------------
# Cap allocation
# --------------------------------------------------------------------------

def test_cap_is_shared_so_a_dense_catalog_cannot_evict_a_sparse_one():
    """Concatenate-then-truncate would let Ontario's thousands bury Calgary."""
    by_provider = {"dense": [{"id": "d%d" % i} for i in range(500)],
                   "sparse": [{"id": "s%d" % i} for i in range(5)]}
    merged = cctv_live._allocate(by_provider, 20)

    assert len(merged) == 20
    assert sum(1 for c in merged if c["id"].startswith("s")) == 5


def test_cap_returns_everything_when_under_budget():
    by_provider = {"a": [{"id": "a1"}, {"id": "a2"}], "b": [{"id": "b1"}]}
    assert len(cctv_live._allocate(by_provider, 100)) == 3


def test_empty_providers_allocate_to_nothing():
    assert cctv_live._allocate({"a": [], "b": []}, 100) == []


# --------------------------------------------------------------------------
# Adapter contract
# --------------------------------------------------------------------------

def test_neutral_value_is_an_empty_catalog_not_none():
    """A failed lookup must still render, and must still say what it asked."""
    neutral = cctv_live.CctvLiveAdapter().neutral_value(bbox=LONDON_BBOX)
    assert neutral["cameras"] == []
    assert neutral["unavailable"] is True
    assert [p["key"] for p in neutral["providers_considered"]] == ["tfl"]


def test_fetch_over_indian_ground_calls_no_provider(monkeypatch):
    """The gate must stop the fetch, not merely filter its results."""
    called = []
    for provider in cctv_live.PROVIDERS:
        monkeypatch.setattr(provider, "fetch",
                            lambda s, t, key=provider.key: called.append(key) or [])

    data = cctv_live.CctvLiveAdapter().fetch_raw(bbox=HYDERABAD_BBOX)

    assert called == []
    assert data["cameras"] == []
    assert data["providers_considered"] == []


def test_one_failing_provider_does_not_take_the_others_down(monkeypatch):
    tfl = next(p for p in cctv_live.PROVIDERS if p.key == "tfl")
    monkeypatch.setattr(tfl, "fetch", lambda s, t: (_ for _ in ()).throw(RuntimeError("503")))

    data = cctv_live.CctvLiveAdapter().fetch_raw(bbox=LONDON_BBOX)

    assert data["cameras"] == []
    assert data["provider_status"]["tfl"].startswith("failed")


def test_cameras_outside_the_requested_bbox_are_filtered(monkeypatch):
    tfl = next(p for p in cctv_live.PROVIDERS if p.key == "tfl")
    inside = cctv_live.make_camera("in", "In", 51.50, -0.10,
                                   "https://example.gov/a.jpg", "T", "L")
    outside = cctv_live.make_camera("out", "Out", 55.00, -3.00,
                                    "https://example.gov/b.jpg", "T", "L")
    monkeypatch.setattr(tfl, "fetch", lambda s, t: [inside, outside])

    data = cctv_live.CctvLiveAdapter().fetch_raw(bbox=LONDON_BBOX)

    assert [c["id"] for c in data["cameras"]] == ["in"]


def test_attribution_reflects_what_is_on_screen(monkeypatch):
    """Crediting an authority whose cameras were all filtered out is wrong."""
    tfl = next(p for p in cctv_live.PROVIDERS if p.key == "tfl")
    camera = cctv_live.make_camera("in", "In", 51.50, -0.10,
                                   "https://example.gov/a.jpg", "TfL",
                                   "Powered by TfL Open Data")
    monkeypatch.setattr(tfl, "fetch", lambda s, t: [camera])

    data = cctv_live.CctvLiveAdapter().fetch_raw(bbox=LONDON_BBOX)
    assert data["attribution"] == ["Powered by TfL Open Data"]


def test_cache_key_is_stable_across_restarts():
    """The base implementation hashes every kwarg, including a db handle whose
    repr embeds a memory address -- which would void the cache on restart."""
    adapter = cctv_live.CctvLiveAdapter()
    first = adapter._cache_key("hyderabad", {"bbox": HYDERABAD_BBOX, "db": object()})
    second = adapter._cache_key("hyderabad", {"bbox": HYDERABAD_BBOX, "db": object()})
    assert first == second


def test_feature_collection_drops_null_properties():
    camera = cctv_live.make_camera("in", "In", 51.5, -0.1,
                                   "https://example.gov/a.jpg", "T", "L")
    collection = cctv_live.to_feature_collection({"cameras": [camera]})

    properties = collection["features"][0]["properties"]
    assert "lat" not in properties and "lon" not in properties
    assert all(value is not None for value in properties.values())


# --------------------------------------------------------------------------
# Frontend wiring
#
# The layer is only useful if the console actually asks for it and actually
# draws the answer. Before this was pinned, `fetchStreams()` existed in
# digital-twin.js and nothing ever called it -- the map had a live-camera
# source, a layer and a click handler, and the source was empty forever. A
# unit test of the adapter cannot catch that, so the wiring is asserted here.
# --------------------------------------------------------------------------

import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def test_the_console_refresh_actually_fetches_the_feeds():
    """The regression this file exists for: a defined-but-never-called fetch."""
    source = _read("static", "js", "digital-twin.js")
    refresh = source.split("async refreshAll()")[1].split("}")[0]
    assert "this.fetchStreams()" in refresh


def test_the_drawer_renders_the_feed_panel_for_the_open_cell():
    source = _read("static", "js", "twin-console.js")
    assert "loadLiveCams(citySlug)" in source
    assert "renderLiveCams(citySlug)" in source


def test_an_empty_panel_names_the_authorities_it_weighed():
    """An empty list must not be able to look like a broken layer."""
    source = _read("static", "js", "twin-console.js")
    # Split on the definition, not the call site in loadLiveCams above it.
    render = source.split("renderLiveCams(citySlug) {")[1]
    assert "No registered road authority publishes a camera catalog" in render
    assert "Weighed: " in render


def test_the_console_markup_has_the_feed_block():
    markup = _read("templates", "partials", "twin_console.html")
    for hook in ("data-twin-live-cams", "data-twin-live-body",
                 "data-twin-live-count", "data-twin-live-source"):
        assert hook in markup


def test_every_feed_type_this_module_emits_has_a_player():
    """A camera the console cannot render is worse than one it never lists.

    ``image`` and ``mjpeg`` are matched explicitly in openStream; ``hls``
    is the fall-through, handled by _playHls. All three must stay covered.
    """
    player = _read("static", "js", "twin-console.js").split("openStream(stream) {")[1]
    body = player.split("closeStream()")[0]

    assert '"image"' in body and '"mjpeg"' in body
    assert "_playHls(body, stream)" in body          # the hls fall-through
    assert set(cctv_live.FEED_TYPES) == {"image", "mjpeg", "hls"}


def test_feed_types_are_a_subset_of_what_the_stream_file_allows():
    """twin/cameras.py validates the operator file against STREAM_TYPES; a
    provider emitting a type outside that set would be dropped downstream."""
    from twin import cameras

    assert set(cctv_live.FEED_TYPES) <= set(cameras.STREAM_TYPES)
