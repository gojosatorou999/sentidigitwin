"""Mapillary image discovery through vector tiles, because bbox search is dead.

Mapillary's documented way to find photos near a point is the Graph API's
``/images?bbox=`` search. Measured against the live API on 2026-09-20 with a
valid token, that endpoint returns **zero rows everywhere** -- Bengaluru,
Hyderabad, Amsterdam, Helsinki -- at every bbox size from 0.002 to 0.04
degrees, with HTTP 200 and no error. It is not a coverage problem and it is
not the token: the same token against the vector-tile endpoint returns 15,412
image points in a single Bengaluru z14 tile.

So the imagery panel's Mapillary half was silently empty whenever a token was
configured, which is the worst shape a failure can take -- an operator sees
"no recent street imagery here" and believes it.

This module takes the working path instead:

1. Fetch the ``mly1_public`` vector tile covering the point (z14, ~2.4 km at
   the equator, so one tile answers any cell-sized query).
2. Decode its ``image`` layer, whose point features carry ``id``,
   ``captured_at`` and ``compass_angle`` -- everything the panel needs except
   the thumbnail URL.
3. Ask the Graph API for thumbnails of the handful of nearest ids, in one
   batched call rather than one per photo.

The decoder below is a minimal Mapbox Vector Tile reader: MVT is protobuf,
and the subset needed here (points, string/number properties) is about a
hundred lines. That is deliberately preferred over adding
``mapbox-vector-tile`` as a dependency for one adapter, in a project whose
standing rule is that it boots with no optional package installed.
"""

import logging
import math
import struct

from .. import geo

log = logging.getLogger("twin.ingest.mapillary_tiles")

TILE_URL = "https://tiles.mapillary.com/maps/vtp/mly1_public/2/%d/%d/%d"
GRAPH_URL = "https://graph.mapillary.com/images"

#: z14 is ~2.4 km across at the equator and ~2.3 km at 13-17 deg N, so a
#: single tile covers any cell-sized radius this adapter is asked for. Going
#: deeper would need four tiles for the same ground; going shallower returns
#: megabytes of points to discard.
TILE_ZOOM = 14

#: The layer inside the tile that holds individual photos. The tile also
#: carries ``sequence`` (linestrings) and ``overview`` (sparser points at low
#: zoom); neither is one photo with a bearing, which is what the panel shows.
IMAGE_LAYER = "image"


# --------------------------------------------------------------------------
# Minimal MVT (protobuf) reader
# --------------------------------------------------------------------------

def _varint(buf, pos):
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(buf, start, end):
    """Yield ``(field_number, wire_type, value)`` for one protobuf message.

    Length-delimited values come back as a ``(start, end)`` span into ``buf``
    rather than a copy, so decoding a megabyte tile does not also allocate a
    megabyte of slices.
    """
    pos = start
    while pos < end:
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
            yield field, wire, value
        elif wire == 1:
            yield field, wire, buf[pos:pos + 8]
            pos += 8
        elif wire == 2:
            length, pos = _varint(buf, pos)
            yield field, wire, (pos, pos + length)
            pos += length
        elif wire == 5:
            yield field, wire, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError("unsupported protobuf wire type %d" % wire)


def _value(buf, start, end):
    """One MVT ``Value``, which is a union of seven possible scalar fields."""
    for field, wire, raw in _fields(buf, start, end):
        if field == 1 and wire == 2:
            return buf[raw[0]:raw[1]].decode("utf-8", "replace")
        if field == 2 and wire == 5:
            return struct.unpack("<f", raw)[0]
        if field == 3 and wire == 1:
            return struct.unpack("<d", raw)[0]
        if field in (4, 5) and wire == 0:
            return raw
        if field == 6 and wire == 0:                      # zigzag sint64
            return (raw >> 1) ^ -(raw & 1)
        if field == 7 and wire == 0:
            return bool(raw)
    return None


def _packed(buf, span):
    values, pos = [], span[0]
    while pos < span[1]:
        value, pos = _varint(buf, pos)
        values.append(value)
    return values


def _first_point(geometry, extent, tile_x, tile_y, zoom):
    """The lat/lon of a point feature's first vertex.

    MVT geometry is a command stream; for a point the only command that
    matters is MoveTo (id 1) followed by one zigzag-encoded dx,dy pair in
    tile-local units.
    """
    if len(geometry) < 3:
        return None, None
    command = geometry[0]
    if command & 0x7 != 1:                                # not MoveTo
        return None, None

    dx = (geometry[1] >> 1) ^ -(geometry[1] & 1)
    dy = (geometry[2] >> 1) ^ -(geometry[2] & 1)

    scale = 2 ** zoom
    lon = (tile_x + dx / float(extent)) / scale * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * (tile_y + dy / float(extent)) / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lon


def decode_image_layer(buf, tile_x, tile_y, zoom=TILE_ZOOM):
    """Every photo point in a Mapillary tile, as dicts with lat/lon.

    Returns ``[]`` for an empty or unparseable tile rather than raising: a
    malformed tile must cost one panel, not the drill-down.
    """
    photos = []
    try:
        for field, wire, span in _fields(buf, 0, len(buf)):
            if field != 3 or wire != 2:                   # Tile.layers
                continue

            name, extent, keys, values, features = None, 4096, [], [], []
            for lf, lw, lv in _fields(buf, span[0], span[1]):
                if lf == 1 and lw == 2:
                    name = buf[lv[0]:lv[1]].decode("utf-8", "replace")
                elif lf == 2 and lw == 2:
                    features.append(lv)
                elif lf == 3 and lw == 2:
                    keys.append(buf[lv[0]:lv[1]].decode("utf-8", "replace"))
                elif lf == 4 and lw == 2:
                    values.append(_value(buf, lv[0], lv[1]))
                elif lf == 5 and lw == 0:
                    extent = lv or 4096

            if name != IMAGE_LAYER:
                continue

            for fs, fe in features:
                tags, geometry = [], []
                for ff, fw, fv in _fields(buf, fs, fe):
                    if ff == 2 and fw == 2:
                        tags = _packed(buf, fv)
                    elif ff == 4 and fw == 2:
                        geometry = _packed(buf, fv)

                lat, lon = _first_point(geometry, extent, tile_x, tile_y, zoom)
                if lat is None:
                    continue

                props = {}
                for i in range(0, len(tags) - 1, 2):
                    if tags[i] < len(keys) and tags[i + 1] < len(values):
                        props[keys[tags[i]]] = values[tags[i + 1]]

                image_id = props.get("id")
                if image_id is None:
                    continue
                photos.append({
                    "id": str(image_id),
                    "lat": lat,
                    "lon": lon,
                    "captured_at": props.get("captured_at"),
                    "compass_angle": props.get("compass_angle"),
                    "is_pano": bool(props.get("is_pano")),
                })
    except (IndexError, ValueError, struct.error) as exc:
        log.warning("mapillary tile could not be decoded (%s)", exc)
        return []
    return photos


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

def tile_for(lat, lon, zoom=TILE_ZOOM):
    """Slippy-map tile indices containing a point."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat))
                            + 1 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def nearby_images(session, token, lat, lon, radius_m, limit, timeout_s):
    """The nearest Mapillary photos to a point, with thumbnails.

    Tiles are fetched, not searched, for the reason in the module docstring.
    Only the ``limit`` nearest ids are hydrated with thumbnail URLs, in one
    batched Graph call -- the tile alone answers "is there imagery here" for
    thousands of photos, and paying per photo to find that out would make the
    drill-down slower than the panel is worth.
    """
    tile_x, tile_y = tile_for(lat, lon)
    response = session.get(TILE_URL % (TILE_ZOOM, tile_x, tile_y),
                           params={"access_token": token}, timeout=timeout_s)
    response.raise_for_status()

    photos = decode_image_layer(response.content, tile_x, tile_y)
    for photo in photos:
        photo["distance_m"] = geo.distance_m(lat, lon, photo["lat"], photo["lon"])

    near = [p for p in photos if p["distance_m"] is not None
            and p["distance_m"] <= radius_m]
    near.sort(key=lambda p: p["distance_m"])
    near = near[:limit]
    if not near:
        return []

    thumbs = _thumbnails(session, token, [p["id"] for p in near], timeout_s)
    for photo in near:
        photo.update(thumbs.get(photo["id"]) or {})

    # A photo with no thumbnail cannot be shown, only counted.
    shown = [p for p in near if p.get("thumb_256_url") or p.get("thumb_1024_url")]

    if near and not shown:
        # Tiles resolved photos but the Graph API returned no thumbnail for a
        # single one of them. That is not "no coverage here" -- it is almost
        # always a token that carries tile access without Graph *read* scope,
        # which the Graph API reports as "object does not exist, cannot be
        # loaded due to missing permissions" per id and as an empty `data`
        # array for a batch. Logged explicitly because the two look identical
        # from the panel, and an operator reading "no recent imagery" over
        # ground with thousands of photos is exactly the wrong conclusion.
        log.warning(
            "mapillary: %d photos found in the tile at %.4f,%.4f but none "
            "returned a thumbnail -- MAPILLARY_TOKEN likely lacks Graph read "
            "scope; regenerate it at mapillary.com/dashboard/developers",
            len(near), lat, lon)

    return shown


def _thumbnails(session, token, image_ids, timeout_s):
    """Thumbnail URLs for up to a handful of ids, in one call."""
    try:
        response = session.get(
            GRAPH_URL,
            params={"access_token": token, "image_ids": ",".join(image_ids),
                    "fields": "id,thumb_256_url,thumb_1024_url"},
            timeout=timeout_s)
        response.raise_for_status()
        rows = (response.json() or {}).get("data") or []
    except Exception as exc:                              # noqa: BLE001
        log.warning("mapillary thumbnail lookup failed (%s)", exc)
        return {}

    return {str(row.get("id")): {"thumb_256_url": row.get("thumb_256_url"),
                                 "thumb_1024_url": row.get("thumb_1024_url")}
            for row in rows if row.get("id") is not None}


