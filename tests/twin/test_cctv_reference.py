"""The reference feed: a stand-in camera list, and the limits on it.

The coverage gate in ``twin/ingest/cctv_live.py`` is correct and stays. Its
consequence, though, is that both modelled cities are permanently empty, so
the layer can never be seen working and a dead panel looks exactly like an
honest one. The reference feed is the answer to that, and it is only
defensible while four properties hold:

1. It engages **only** where nothing local exists -- never beside a real
   camera, so it can never dilute or outrank local ground.
2. Every stream is **flagged and labelled** with where it actually comes from.
3. It is **positionless**, so nothing can draw it as a pin on this city's map.
4. It **never reaches scoring** -- no risk term, flag or brief reads it.

Each of those is pinned below from both directions. Nothing here touches the
network: the provider is monkeypatched with a fixture catalog.
"""

import os

import pytest

from twin import cameras as twin_cameras
from twin import config
from twin.ingest import cctv_live


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

HYDERABAD_BBOX = (78.24, 17.22, 78.66, 17.60)
HONGKONG_BBOX = (113.82, 22.13, 114.45, 22.58)


def _hk_rows(count):
    """A TD-shaped catalog of ``count`` rows, spread across the territory."""
    return [{
        "key": "H%03dF" % index,
        "region": "Hong Kong Island",
        "district": "Southern",
        "description": "Test Road %d [H%03dF]" % (index, index),
        "latitude": "22.%03d" % (250 + index % 200),
        "longitude": "114.%03d" % (150 + index % 100),
    } for index in range(count)]


@pytest.fixture()
def hk_catalog(monkeypatch):
    """The Hong Kong provider, answering from a fixture instead of the network.

    Patched on the Provider instance rather than on the registry tuple, as
    the rest of this suite does: the gate is being exercised for real, only
    the transport is stubbed.
    """
    cameras = cctv_live.parse_hongkong(_hk_rows(200))
    assert len(cameras) == 200, "fixture rows must all survive parsing"

    for provider in cctv_live.PROVIDERS:
        if provider.key == "hongkong":
            monkeypatch.setattr(provider, "fetch",
                                lambda session, timeout_s: [dict(c) for c in cameras])
        else:
            monkeypatch.setattr(provider, "fetch",
                                lambda session, timeout_s: [])

    monkeypatch.setattr(config, "CCTV_LIVE_ENABLED", True)
    monkeypatch.setattr(config, "CCTV_LIVE_PROVIDERS", "")
    monkeypatch.setattr(config, "CCTV_REFERENCE_ENABLED", True)
    monkeypatch.setattr(config, "CCTV_REFERENCE_PROVIDER", "hongkong")
    return cameras


# --------------------------------------------------------------------------
# 1. It engages only where nothing local exists
# --------------------------------------------------------------------------

def test_a_city_with_no_provider_gets_the_reference_feed(db, hk_catalog):
    streams, meta = twin_cameras.live_streams(
        db, city_slug="hyderabad", bbox=HYDERABAD_BBOX)

    assert streams, "an uncovered city should fall back to the reference feed"
    assert meta["reference"]["provider"] == "hongkong"
    assert meta["providers_considered"] == [], "no authority covers Hyderabad"


def test_a_covered_city_never_gets_one(db, hk_catalog):
    """The gate still decides. Real local ground is never joined by a stand-in."""
    streams, meta = twin_cameras.live_streams(
        db, city_slug="hongkong", bbox=HONGKONG_BBOX)

    assert meta.get("reference") is None
    assert streams, "Hong Kong is covered by a real provider"
    assert all(not s.get("reference") for s in streams)


def test_an_operator_feed_suppresses_it(db, hk_catalog, tmp_path, monkeypatch):
    """A deployment that hand-listed its own camera keeps its own camera.

    This is the property that stops the reference feed being a nuisance in a
    real deployment: the moment an operator has anything local, the stand-in
    goes away rather than padding the list out beneath it.
    """
    stream_file = tmp_path / "cctv_streams.json"
    stream_file.write_text(
        '[{"id": "iccc-1", "name": "GHMC ICCC 1", "city": "hyderabad",'
        ' "lat": 17.4, "lon": 78.48, "url": "https://example.gov.in/a.m3u8",'
        ' "type": "hls"}]',
        encoding="utf-8")
    monkeypatch.setattr(config, "CCTV_STREAMS_FILE", str(stream_file))

    streams, meta = twin_cameras.live_streams(
        db, city_slug="hyderabad", bbox=HYDERABAD_BBOX,
        path=str(stream_file))

    assert meta.get("reference") is None
    assert [s["id"] for s in streams] == ["iccc-1"]


def test_an_unscoped_request_still_fetches_nothing(db, hk_catalog):
    """providers_for(None) returns (), and this must not be a way around it.

    A request with no city is "cameras, anywhere", which this layer cannot
    answer usefully. When the reference feed was first wired up it engaged
    here too, so an unscoped call quietly pulled a foreign national catalog
    -- reintroducing exactly the fetch the unscoped case exists to prevent.
    """
    streams, meta = twin_cameras.live_streams(db, city_slug=None, bbox=None)

    assert streams == []
    assert meta.get("reference") is None


def test_the_switch_restores_the_strictly_empty_panel(db, hk_catalog, monkeypatch):
    monkeypatch.setattr(config, "CCTV_REFERENCE_ENABLED", False)

    streams, meta = twin_cameras.live_streams(
        db, city_slug="hyderabad", bbox=HYDERABAD_BBOX)

    assert streams == []
    assert meta.get("reference") is None


def test_a_misconfigured_provider_key_yields_nothing(monkeypatch):
    monkeypatch.setattr(config, "CCTV_LIVE_ENABLED", True)
    monkeypatch.setattr(config, "CCTV_REFERENCE_ENABLED", True)
    monkeypatch.setattr(config, "CCTV_REFERENCE_PROVIDER", "atlantis")

    assert cctv_live.reference_provider() is None


def test_the_allowlist_still_governs_it(monkeypatch):
    """Narrowing TWIN_CCTV_LIVE_PROVIDERS must not be undone by this door."""
    monkeypatch.setattr(config, "CCTV_LIVE_ENABLED", True)
    monkeypatch.setattr(config, "CCTV_REFERENCE_ENABLED", True)
    monkeypatch.setattr(config, "CCTV_REFERENCE_PROVIDER", "hongkong")
    monkeypatch.setattr(config, "CCTV_LIVE_PROVIDERS", "tfl")

    assert cctv_live.reference_provider() is None


def test_the_master_switch_still_governs_it(monkeypatch):
    monkeypatch.setattr(config, "CCTV_REFERENCE_ENABLED", True)
    monkeypatch.setattr(config, "CCTV_LIVE_ENABLED", False)

    assert cctv_live.reference_provider() is None


# --------------------------------------------------------------------------
# 2 & 3. Flagged, labelled, and positionless
# --------------------------------------------------------------------------

def test_every_reference_stream_is_flagged_and_attributed(db, hk_catalog):
    streams, meta = twin_cameras.live_streams(
        db, city_slug="hyderabad", bbox=HYDERABAD_BBOX)

    assert streams
    for stream in streams:
        assert stream["reference"] is True
        assert stream["reference_region"] == "Hong Kong SAR"
        assert stream["reference_provider"] == "Transport Department, HKSAR"
    assert "Hong Kong SAR" in meta["reference"]["note"]


def test_reference_streams_carry_no_position(db, hk_catalog):
    """The property that makes a mis-render impossible rather than unlikely.

    A consumer that forgets the flag still cannot place one of these on the
    city map, because there is no coordinate to place it at.
    """
    streams, _meta = twin_cameras.live_streams(
        db, city_slug="hyderabad", bbox=HYDERABAD_BBOX)

    assert streams
    assert all(s["lat"] is None and s["lon"] is None for s in streams)
    assert all(s.get("distance_m") is None for s in streams)


def test_the_real_catalog_keeps_its_positions(db, hk_catalog):
    """Nulling coordinates is done on the copy, not on the cached catalog."""
    streams, _meta = twin_cameras.live_streams(
        db, city_slug="hongkong", bbox=HONGKONG_BBOX)

    assert streams
    assert all(s["lat"] is not None and s["lon"] is not None for s in streams)


def test_it_is_capped_and_spread_across_the_catalog(db, hk_catalog, monkeypatch):
    """Taking the first N would show one neighbourhood; striding shows the city."""
    monkeypatch.setattr(config, "CCTV_REFERENCE_MAX", 10)

    streams, meta = twin_cameras.live_streams(
        db, city_slug="hyderabad", bbox=HYDERABAD_BBOX)

    assert len(streams) == 10
    assert meta["reference"]["catalog_count"] == 200
    ids = [s["id"] for s in streams]
    assert ids != [c["id"] for c in hk_catalog[:10]], "must not be the first ten"
    assert len(set(ids)) == 10


def test_spread_is_stable_and_in_order():
    items = list(range(100))
    assert twin_cameras._spread(items, 5) == [0, 20, 40, 60, 80]
    assert twin_cameras._spread(items, 0) == []
    assert twin_cameras._spread([1, 2], 10) == [1, 2]


# --------------------------------------------------------------------------
# 4. It never reaches scoring
# --------------------------------------------------------------------------

def test_live_streams_is_only_read_by_the_console_route():
    """The structural guarantee behind "no reference camera reaches scoring".

    Rather than asserting on a score, this pins the one thing that makes the
    claim true: ``live_streams`` has exactly one caller, and it is the panel
    endpoint. A new caller in the scoring path would fail here and force the
    question to be answered deliberately.
    """
    callers = set()
    for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, "twin")):
        for name in files:
            if not name.endswith(".py") or name == "cameras.py":
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            if "live_streams(" in body:
                callers.add(os.path.relpath(path, REPO_ROOT).replace("\\", "/"))

    assert callers == {"twin/routes.py"}


# --------------------------------------------------------------------------
# What the operator actually sees
# --------------------------------------------------------------------------

def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def test_the_map_layer_excludes_reference_feeds():
    source = _read("static", "js", "digital-twin.js")
    fetch = source.split("async fetchStreams()")[1].split("async fetchSummary()")[0]
    assert "!stream.reference" in fetch
    assert "reference: payload.reference" in fetch


def test_the_panel_says_it_is_not_local():
    render = _read("static", "js", "twin-console.js").split("renderLiveCams(citySlug) {")[1]
    assert "twin-cctv-reference" in render
    assert "Not local" in render
    assert "not read by scoring" in render


def test_the_player_carries_the_label_too():
    """A frame shown full-screen with no list around it still has to say it."""
    source = _read("static", "js", "twin-console.js")
    modal = source.split("openStream(stream) {")[1].split("_playHls(")[0]
    assert "stream.reference" in modal
    assert "Reference feed" in modal


def test_the_banner_has_styling():
    css = _read("static", "css", "twin.css")
    assert ".twin-cctv-reference {" in css
    assert ".twin-cctv-chip.reference {" in css
