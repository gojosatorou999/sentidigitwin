"""The Mapillary vector-tile reader.

``twin/ingest/streetview.py`` used to find photos through the Graph API's
``/images?bbox=`` search, which the documentation recommends and which -- with
a valid token, on ground carrying thousands of photos -- returns zero rows and
HTTP 200. The panel therefore reported "no recent street imagery" for
Bengaluru and Hyderabad while a single vector tile over the same ground held
15,412 photos.

So the discovery path is now a tile decode, and the decoder is hand-rolled
(MVT is protobuf; the point-and-scalar subset is small, and the project's
standing rule is that it boots with no optional package installed). That makes
the decoder this project's code rather than a library's, so its wire-format
handling is pinned here: a varint, a zigzag delta, a tile-to-WGS84 conversion
and a property table lookup are each places where a silent off-by-one puts a
photo in the wrong hemisphere rather than raising.

Tiles are built in-process rather than downloaded, so no test here touches the
network.
"""

import math
import struct

import pytest

from twin import geo
from twin.ingest import mapillary_tiles as mt


# --------------------------------------------------------------------------
# A minimal MVT encoder, so the decoder is tested against bytes it did not
# produce itself.
# --------------------------------------------------------------------------

def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _delimited(field, payload):
    return _tag(field, 2) + _varint(len(payload)) + payload


def _zigzag(value):
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def _string_value(text):
    return _delimited(1, text.encode("utf-8"))


def _int_value(number):
    return _tag(4, 0) + _varint(number)


def _double_value(number):
    return _tag(3, 1) + struct.pack("<d", number)


def _bool_value(flag):
    return _tag(7, 0) + _varint(1 if flag else 0)


def build_tile(features, layer_name=mt.IMAGE_LAYER, extent=4096):
    """One MVT tile holding point features.

    ``features`` is a list of ``(x, y, {key: encoded_value})`` in tile-local
    units.
    """
    keys, values = [], []
    key_index, value_index = {}, {}

    def intern(table, index, item, encoder):
        encoded = encoder(item)
        if encoded not in index:
            index[encoded] = len(table)
            table.append(encoded)
        return index[encoded]

    body = b""
    for x, y, props in features:
        tags = []
        for key, encoded_value in props.items():
            if key not in key_index:
                key_index[key] = len(keys)
                keys.append(key.encode("utf-8"))
            tags.append(key_index[key])
            if encoded_value not in value_index:
                value_index[encoded_value] = len(values)
                values.append(encoded_value)
            tags.append(value_index[encoded_value])

        geometry = [(1 << 3) | 1, _zigzag(x), _zigzag(y)]   # MoveTo, one pair
        feature = (
            _delimited(2, b"".join(_varint(t) for t in tags))
            + _tag(3, 0) + _varint(1)                        # POINT
            + _delimited(4, b"".join(_varint(g) for g in geometry))
        )
        body += _delimited(2, feature)

    layer = _delimited(1, layer_name.encode("utf-8")) + body
    layer += b"".join(_delimited(3, k) for k in keys)
    layer += b"".join(_delimited(4, v) for v in values)
    layer += _tag(5, 0) + _varint(extent)
    layer += _tag(15, 0) + _varint(2)                        # version
    return _delimited(3, layer)


BENGALURU = (12.9757, 77.6069)


def local_xy(lat, lon, extent=4096):
    """Where a point sits *inside* its own tile, in tile-local units.

    Tests that place a photo "near" a query point have to offset from here,
    not from the tile centre: the centre of tile 11723/7596 is ~700 m from
    Bengaluru, which is enough to invert a nearest-first ordering.
    """
    n = 2 ** mt.TILE_ZOOM
    tile_x, tile_y = mt.tile_for(lat, lon)
    fx = (lon + 180.0) / 360.0 * n - tile_x
    fy = ((1.0 - math.log(math.tan(math.radians(lat))
                          + 1 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n) - tile_y
    return int(fx * extent), int(fy * extent)


def _photo(x, y, image_id="211876114077110", angle=191.9, captured=1481970275999):
    return (x, y, {
        "id": _int_value(int(image_id)),
        "compass_angle": _double_value(angle),
        "captured_at": _int_value(captured),
        "is_pano": _bool_value(False),
    })


# --------------------------------------------------------------------------
# Tile arithmetic
# --------------------------------------------------------------------------

def test_tile_indices_match_the_slippy_map_formula():
    assert mt.tile_for(*BENGALURU) == (11723, 7596)
    assert mt.tile_for(17.3850, 78.4867) == (11764, 7388)


def test_tile_indices_stay_inside_the_grid_at_the_poles():
    limit = 2 ** mt.TILE_ZOOM - 1
    x, y = mt.tile_for(85.0, 179.9)
    assert 0 <= x <= limit and 0 <= y <= limit


def test_a_decoded_point_lands_back_where_it_was_encoded():
    """The round trip that matters: a tile-local offset must map to the right
    place on Earth, not merely to a plausible-looking number."""
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    tile = build_tile([_photo(2048, 2048)])          # tile centre

    photos = mt.decode_image_layer(tile, tile_x, tile_y)
    assert len(photos) == 1

    # The centre of the tile containing Bengaluru is within a kilometre of it.
    distance = geo.distance_m(BENGALURU[0], BENGALURU[1],
                               photos[0]["lat"], photos[0]["lon"])
    assert distance < 1500


def test_opposite_corners_decode_to_opposite_corners():
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    tile = build_tile([_photo(0, 0, "1"), _photo(4095, 4095, "2")])

    photos = {p["id"]: p for p in mt.decode_image_layer(tile, tile_x, tile_y)}

    # Tile y grows southward, x eastward.
    assert photos["1"]["lat"] > photos["2"]["lat"]
    assert photos["1"]["lon"] < photos["2"]["lon"]


# --------------------------------------------------------------------------
# Property decoding
# --------------------------------------------------------------------------

def test_properties_are_read_off_the_key_value_tables():
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    tile = build_tile([_photo(100, 100, angle=53.6, captured=1424681815000)])

    photo = mt.decode_image_layer(tile, tile_x, tile_y)[0]

    assert photo["id"] == "211876114077110"
    assert photo["compass_angle"] == pytest.approx(53.6)
    assert photo["captured_at"] == 1424681815000
    assert photo["is_pano"] is False


def test_a_feature_without_an_id_is_dropped():
    """It cannot be linked to a thumbnail, so it can only be miscounted."""
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    tile = build_tile([(100, 100, {"compass_angle": _double_value(90.0)})])

    assert mt.decode_image_layer(tile, tile_x, tile_y) == []


def test_only_the_image_layer_is_read():
    """A tile also carries `sequence` linestrings and an `overview` layer;
    neither is one photo with a bearing."""
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    tile = build_tile([_photo(100, 100)], layer_name="sequence")

    assert mt.decode_image_layer(tile, tile_x, tile_y) == []


def test_a_non_default_extent_is_honoured():
    """Layers may declare any extent; assuming 4096 would misplace photos."""
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    coarse = mt.decode_image_layer(
        build_tile([_photo(512, 512)], extent=1024), tile_x, tile_y)[0]
    fine = mt.decode_image_layer(
        build_tile([_photo(2048, 2048)], extent=4096), tile_x, tile_y)[0]

    assert coarse["lat"] == pytest.approx(fine["lat"])
    assert coarse["lon"] == pytest.approx(fine["lon"])


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------

def test_a_truncated_tile_costs_one_panel_not_the_drilldown():
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    tile = build_tile([_photo(100, 100)])

    assert mt.decode_image_layer(tile[:len(tile) // 2], tile_x, tile_y) == []


def test_an_empty_tile_decodes_to_nothing():
    assert mt.decode_image_layer(b"", 11723, 7596) == []


def test_garbage_is_not_an_exception():
    assert mt.decode_image_layer(b"\xff\xff\xff\xff\x7f", 11723, 7596) == []


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

class _FakeResponse(object):
    def __init__(self, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession(object):
    """Answers the tile request with bytes and the Graph request with JSON."""

    def __init__(self, tile, thumbs):
        self.tile = tile
        self.thumbs = thumbs
        self.graph_calls = 0

    def get(self, url, params=None, timeout=None, headers=None):
        if url.startswith(mt.GRAPH_URL):
            self.graph_calls += 1
            return _FakeResponse(payload={"data": self.thumbs})
        return _FakeResponse(content=self.tile)


def _thumb_rows(*ids):
    return [{"id": i, "thumb_256_url": "https://img/%s_256.jpg" % i,
             "thumb_1024_url": "https://img/%s_1024.jpg" % i} for i in ids]


def test_nearby_images_returns_the_nearest_first():
    # Offsets are measured from the query point's own position in the tile,
    # and deliberately encoded out of order.
    px, py = local_xy(*BENGALURU)
    tile = build_tile([_photo(px + 100, py, "3"), _photo(px + 10, py, "1"),
                       _photo(px + 40, py, "2")])
    session = _FakeSession(tile, _thumb_rows("1", "2", "3"))

    photos = mt.nearby_images(session, "TOKEN", BENGALURU[0], BENGALURU[1],
                              5000, 8, 30)

    assert [p["id"] for p in photos] == ["1", "2", "3"]
    assert photos[0]["thumb_256_url"] == "https://img/1_256.jpg"


def test_photos_beyond_the_radius_are_excluded():
    px, py = local_xy(*BENGALURU)
    tile = build_tile([_photo(px, py, "11"), _photo(0, 0, "22")])
    session = _FakeSession(tile, _thumb_rows("11", "22"))

    photos = mt.nearby_images(session, "TOKEN", BENGALURU[0], BENGALURU[1],
                              350, 8, 30)

    assert [p["id"] for p in photos] == ["11"]


def test_thumbnails_are_fetched_in_one_call_not_one_per_photo():
    """A tile holds thousands of points; paying per photo to learn that would
    make the drill-down slower than the panel is worth."""
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    px, py = local_xy(*BENGALURU)
    tile = build_tile([_photo(px + i, py, str(i + 1)) for i in range(6)])
    session = _FakeSession(tile, _thumb_rows(*[str(i + 1) for i in range(6)]))

    mt.nearby_images(session, "TOKEN", BENGALURU[0], BENGALURU[1], 5000, 8, 30)

    assert session.graph_calls == 1


def test_a_photo_without_a_thumbnail_is_not_returned():
    """It cannot be shown, only counted."""
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    px, py = local_xy(*BENGALURU)
    tile = build_tile([_photo(px, py, "1"), _photo(px + 2, py, "2")])
    session = _FakeSession(tile, _thumb_rows("1"))

    photos = mt.nearby_images(session, "TOKEN", BENGALURU[0], BENGALURU[1],
                              5000, 8, 30)

    assert [p["id"] for p in photos] == ["1"]


def test_a_token_without_graph_scope_says_so(caplog):
    """The live failure this module was written against: tiles resolve, every
    thumbnail comes back empty. That must not read as 'no coverage here'."""
    tile_x, tile_y = mt.tile_for(*BENGALURU)
    px, py = local_xy(*BENGALURU)
    tile = build_tile([_photo(px, py, "1")])
    session = _FakeSession(tile, [])                 # Graph returns data: []

    with caplog.at_level("WARNING"):
        photos = mt.nearby_images(session, "TOKEN", BENGALURU[0], BENGALURU[1],
                                  5000, 8, 30)

    assert photos == []
    assert "Graph read scope" in caplog.text


def test_no_photos_at_all_makes_no_graph_call(caplog):
    """Genuinely empty ground must stay distinguishable from a scope problem."""
    session = _FakeSession(build_tile([]), [])

    with caplog.at_level("WARNING"):
        assert mt.nearby_images(session, "TOKEN", BENGALURU[0], BENGALURU[1],
                                350, 8, 30) == []

    assert session.graph_calls == 0
    assert "Graph read scope" not in caplog.text
