"""NASA GIBS true-colour satellite imagery: tile URLs + date availability.

This is a pure URL builder for a raster basemap layer (section 8.4, layer 2)
-- there is nothing to score, so it is not an IngestAdapter subclass. The one
piece of real logic is probing which date actually has imagery, because GIBS
can lag by a day or two and a naive "use today's date" produces a blank tile
with no visible error (section 15).
"""

import logging
from datetime import date, timedelta

import requests

from .. import config

log = logging.getLogger("twin.ingest.nasa_gibs")

WMTS_TEMPLATE = (
    "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
    "{layer}/default/{date}/{tms}/{z}/{y}/{x}.jpg"
)

#: The trailing "_LevelN" on each TileMatrixSet name is GIBS's own maximum
#: zoom for that layer -- both true-colour layers cap at 9 because that
#: matches their native ~500m/pixel imagery resolution, not an arbitrary
#: choice. Confirmed live: a raster source with no `maxzoom` keeps
#: requesting z=10/11 tiles that don't exist and gets a 400 for every one,
#: because nothing told MapLibre where to stop and over-zoom the last valid
#: tile instead (twin/routes.py's /gibs response surfaces `max_zoom` from
#: this table so the frontend never has to hardcode it either).
LAYERS = {
    "viirs": {"id": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
             "tms": "GoogleMapsCompatible_Level9", "max_zoom": 9},
    "modis_terra": {"id": "MODIS_Terra_CorrectedReflectance_TrueColor",
                    "tms": "GoogleMapsCompatible_Level9", "max_zoom": 9},
}

#: A single known-good tile (low zoom, over India) used only to test whether
#: a given date has coverage -- cheap, not the full capabilities XML.
_PROBE_Z, _PROBE_X, _PROBE_Y = 3, 6, 3


def tile_url_template(layer="viirs", for_date=None):
    """A `{z}/{x}/{y}` URL template for one GIBS layer + date."""
    spec = LAYERS.get(layer, LAYERS["viirs"])
    day = (for_date or date.today()).isoformat()
    return WMTS_TEMPLATE.format(layer=spec["id"], date=day, tms=spec["tms"], z="{z}", y="{y}", x="{x}")


def is_date_available(layer, target_date, session=None, timeout_s=None):
    """Whether `layer` actually serves a tile for `target_date`. Never raises
    -- a network error is treated as "not available", not an error, since
    the caller's job is to decide whether to show this date or fall back."""
    session = session or requests.Session()
    timeout_s = timeout_s or config.HTTP_TIMEOUT_S
    spec = LAYERS.get(layer, LAYERS["viirs"])
    url = WMTS_TEMPLATE.format(
        layer=spec["id"], date=target_date.isoformat(), tms=spec["tms"],
        z=_PROBE_Z, y=_PROBE_Y, x=_PROBE_X,
    )
    try:
        response = session.head(url, timeout=timeout_s, allow_redirects=True)
        return response.status_code == 200
    except requests.RequestException as exc:
        log.debug("gibs probe failed for %s: %s", target_date, exc)
        return False


def probe_available_date(layer="viirs", max_days_back=5, session=None, timeout_s=None):
    """The most recent date (<= today) that actually serves a tile for `layer`.

    Falls back by walking backwards day by day; returns None (never raises)
    if nothing in the window works, so the caller can hide the layer and
    label it unavailable rather than show a blank raster (C1).
    """
    session = session or requests.Session()
    for offset in range(max_days_back):
        day = date.today() - timedelta(days=offset)
        if is_date_available(layer, day, session=session, timeout_s=timeout_s):
            return day
    return None
