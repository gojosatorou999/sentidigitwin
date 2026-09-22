"""Street-level imagery and nearby webcams for a twin cell (ground truth layer).

The twin scores a hexagon from rain, discharge, terrain and reports; an
operator's next question is always "what does that place actually look like?".
This adapter answers it from *free-tier* sources only, in a strict
availability order so the panel works out of the box with no API key at all:

1. **KartaView / OpenStreetCam** -- keyless. Public crowd-sourced street-level
   photography with real coverage in Hyderabad and Bengaluru. This is the
   default provider and the reason the panel needs no configuration.
2. **Mapillary** -- free, but needs a client token (``MAPILLARY_TOKEN``).
   Denser and more recent than KartaView in many Indian wards, so it is tried
   first whenever a token is present.
3. **Windy Webcams** -- free tier, needs ``WINDY_WEBCAMS_KEY``. Adds actual
   *live* cameras rather than an archived pass-by photo.

Every provider is optional and every failure is soft: a dead or unkeyed
provider contributes zero images and is reported as such, exactly like any
other Tier-2 source (C1/C5/C7). A Google Street View deep link is always
returned as a last-resort fallback -- it is a plain maps.google.com URL that
opens in the operator's own browser, so it costs nothing and needs no key.
"""

import logging

from .. import config
from .. import geo
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.streetview")

KARTAVIEW_URL = "https://api.openstreetcam.org/2.0/photo/"
MAPILLARY_URL = "https://graph.mapillary.com/images"
WINDY_WEBCAMS_URL = "https://api.windy.com/webcams/api/v3/webcams"

#: Metres. KartaView's endpoint gets markedly slower as this grows, and a
#: photo more than ~600 m from the cell centre is no longer showing the
#: cell -- confirmed live: radius=1500 regularly exceeded a 25 s timeout
#: while radius=300 answered in under two seconds with the same nearest hit.
DEFAULT_RADIUS_M = 350

#: Second-attempt radius for a cell the tight query found nothing in.
WIDEN_RADIUS_M = 900
MAX_IMAGES = 8


def google_streetview_url(lat, lon):
    """Keyless deep link into Google's own Street View viewer.

    Not an embed and not an API call -- ``map_action=pano`` is a documented
    public maps.google.com URL, so this works with no key, no quota and no
    request from our servers. It is the honest fallback for a cell that no
    open imagery provider covers.
    """
    return ("https://www.google.com/maps/@?api=1&map_action=pano"
            "&viewpoint=%.6f,%.6f" % (lat, lon))


def osm_url(lat, lon):
    return "https://www.openstreetmap.org/#map=18/%.5f/%.5f" % (lat, lon)


class StreetViewAdapter(IngestAdapter):
    """Nearest street-level imagery + live webcams around a point.

    Returns a dict, never a bare list, so the panel can tell "no coverage
    here" (``images: []`` with ``providers`` explaining why) apart from "the
    lookup itself failed" (``run()`` degrades to cache or ``neutral_value``).
    """

    source_key = "streetview"
    #: Street-level imagery is archival -- a photo taken in 2020 is the same
    #: photo an hour from now. A long TTL keeps a demo audience clicking
    #: through twenty hexes off KartaView's rate limiter entirely.
    cache_ttl_s = 24 * 60 * 60
    #: Bounded low on purpose: this call sits on the drill-down's critical
    #: path, so a slow provider must fail fast and let the panel render "no
    #: coverage" rather than spin. KartaView answers a hit in ~2 s; anything
    #: past 6 s is a provider stall, not a slow hit worth waiting for.
    timeout_s = 6.0
    max_retries = 1

    def fetch_raw(self, lat=None, lon=None, radius_m=DEFAULT_RADIUS_M, **_):
        if lat is None or lon is None:
            raise ValueError("streetview lookup needs lat and lon")

        images = []
        providers = {}

        if config.MAPILLARY_TOKEN:
            images, providers["mapillary"] = self._safe(self._mapillary, lat, lon, radius_m)
        else:
            providers["mapillary"] = "no MAPILLARY_TOKEN configured"

        if not images:
            images, providers["kartaview"] = self._safe(self._kartaview, lat, lon, radius_m)

        if config.WINDY_WEBCAMS_KEY:
            webcams, providers["windy_webcams"] = self._safe(self._windy, lat, lon, radius_m)
        else:
            webcams, providers["windy_webcams"] = [], "no WINDY_WEBCAMS_KEY configured"

        return {
            "lat": lat,
            "lon": lon,
            "radius_m": radius_m,
            "images": images[:MAX_IMAGES],
            "webcams": webcams[:MAX_IMAGES],
            "providers": providers,
            "google_streetview_url": google_streetview_url(lat, lon),
            "osm_url": osm_url(lat, lon),
        }

    def neutral_value(self, lat=None, lon=None, **_):
        """No network at all still leaves the operator a working way to look
        at the place -- the deep link needs neither our server nor a key."""
        if lat is None or lon is None:
            return None
        return {
            "lat": lat, "lon": lon, "images": [], "webcams": [],
            "providers": {"all": "network unavailable"},
            "google_streetview_url": google_streetview_url(lat, lon),
            "osm_url": osm_url(lat, lon),
        }

    def record_count(self, data):
        if not data:
            return 0
        return len(data.get("images") or []) + len(data.get("webcams") or [])

    def _cache_key(self, city_id, kwargs):
        """Key on the location alone, rounded to ~11 m.

        The base implementation hashes every kwarg, and one of them is the
        Flask-SQLAlchemy handle whose default repr embeds a memory address --
        so the key silently changes on every process restart and a 24-hour
        TTL never survives one. That is invisible for a 15-minute weather
        cache but defeats the entire point here, where the whole reason to
        cache is that a photo taken in 2020 will not change today.
        """
        import hashlib

        lat = round(float(kwargs.get("lat") or 0.0), 4)
        lon = round(float(kwargs.get("lon") or 0.0), 4)
        radius = int(kwargs.get("radius_m") or DEFAULT_RADIUS_M)
        raw = "streetview:%s:%s:%s" % (lat, lon, radius)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    # -- providers ---------------------------------------------------------

    def _safe(self, fn, lat, lon, radius_m):
        """(results, status_string). One dead provider never fails the panel."""
        try:
            results = fn(lat, lon, radius_m)
        except Exception as exc:  # noqa: BLE001 - Tier-2, degrade don't raise
            log.warning("streetview provider %s failed: %s", fn.__name__, exc)
            return [], "error: %s" % exc
        return results, "ok (%d)" % len(results)

    def _kartaview(self, lat, lon, radius_m):
        rows = self._kartaview_rows(lat, lon, radius_m)
        if not rows and radius_m < WIDEN_RADIUS_M:
            # Coverage is a road network, not a blanket: measured across
            # random cells in both cities, ~350 m finds imagery for a bit
            # over a third of them, and widening to ~900 m roughly doubles
            # that. Widening unconditionally is the wrong trade -- the same
            # measurement showed the wider query is several times slower
            # where coverage is dense -- so it is only used as a second
            # attempt for cells the tight query found nothing in.
            rows = self._kartaview_rows(lat, lon, WIDEN_RADIUS_M)

        images = []
        for row in rows[:MAX_IMAGES]:
            # imageProcUrl is KartaView's CDN-resized, face/plate-blurred
            # rendition; fileurlProc is the origin. Prefer the CDN one --
            # the origin storageNN hosts are noticeably slower.
            full = row.get("imageProcUrl") or row.get("fileurlProc") or row.get("fileurl")
            thumb = row.get("imageLthUrl") or row.get("fileurlLTh") or full
            if not full:
                continue
            images.append({
                "provider": "kartaview",
                "id": row.get("id"),
                "thumb_url": thumb,
                "image_url": full,
                "lat": _as_float(row.get("lat")),
                "lon": _as_float(row.get("lng")),
                "captured_at": (row.get("shotDate") or row.get("dateAdded") or "")[:10] or None,
                "heading": _as_float(row.get("headers")),
                "distance_m": _as_float(row.get("distance")),
                "attribution": "(c) KartaView contributors (CC BY-SA)",
                "permalink": "https://kartaview.org/details/%s/%s" % (
                    row.get("sequenceId"), row.get("sequenceIndex")),
            })
        images.sort(key=lambda i: i["distance_m"] if i["distance_m"] is not None else 1e9)
        return images

    def _kartaview_rows(self, lat, lon, radius_m):
        response = self.session.get(
            KARTAVIEW_URL,
            params={"lat": lat, "lng": lon, "radius": int(radius_m)},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        return ((response.json() or {}).get("result") or {}).get("data") or []

    def _mapillary(self, lat, lon, radius_m):
        """Nearest Mapillary photos, discovered through vector tiles.

        Not through ``/images?bbox=``, which the documentation points at and
        which returns zero rows for every bbox and every city tested with a
        valid token -- including ones with dense coverage. See
        twin/ingest/mapillary_tiles.py for the measurements. Using it meant
        the panel reported "no imagery" on ground that has thousands of
        photos, which is the one failure shape worth engineering around.
        """
        from .mapillary_tiles import nearby_images

        photos = nearby_images(self.session, config.MAPILLARY_TOKEN,
                               lat, lon, radius_m, MAX_IMAGES, self.timeout_s)

        return [{
            "provider": "mapillary",
            "id": photo["id"],
            "thumb_url": photo.get("thumb_256_url") or photo.get("thumb_1024_url"),
            "image_url": photo.get("thumb_1024_url"),
            "lat": photo["lat"],
            "lon": photo["lon"],
            "captured_at": _epoch_ms_to_date(photo.get("captured_at")),
            "heading": photo.get("compass_angle"),
            "distance_m": photo.get("distance_m"),
            "attribution": "(c) Mapillary contributors (CC BY-SA)",
            "permalink": "https://www.mapillary.com/app/?pKey=%s&focus=photo" % photo["id"],
        } for photo in photos]

    def _windy(self, lat, lon, radius_m):
        radius_km = max(1, int(round(radius_m / 1000.0)) or 5)
        response = self.session.get(
            WINDY_WEBCAMS_URL,
            params={
                "nearby": "%f,%f,%d" % (lat, lon, radius_km),
                # ``player`` yields the public embeddable live player; ``images``
                # yields the continuously-updated snapshot. The panel embeds the
                # first and auto-refreshes the second -- both are what makes a
                # webcam "live" rather than a one-off photo.
                "include": "location,images,urls,player",
                "limit": MAX_IMAGES,
            },
            headers={"x-windy-api-key": config.WINDY_WEBCAMS_KEY},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        rows = (response.json() or {}).get("webcams") or []

        webcams = []
        for row in rows:
            location = row.get("location") or {}
            current = (row.get("images") or {}).get("current") or {}
            player = row.get("player") or {}
            # Windy publishes these ``/embed/player/<id>/*`` URLs specifically
            # for iframe embedding, so surfacing one is a link the operator was
            # meant to follow -- never a proxied or scraped stream.
            player_url = player.get("day") or player.get("live") or player.get("month")
            webcams.append({
                "provider": "windy",
                "id": row.get("webcamId") or row.get("id"),
                "title": row.get("title"),
                "thumb_url": current.get("preview") or current.get("thumbnail"),
                "image_url": current.get("preview"),
                # A live embeddable player, when Windy exposes one for this cam.
                "player_url": player_url,
                # ISO8601; the panel shows it as the "last frame" age so an
                # operator can judge how live "live" actually is.
                "last_updated": row.get("lastUpdatedOn"),
                "status": row.get("status"),
                "lat": location.get("latitude"),
                "lon": location.get("longitude"),
                "live": True,
                "attribution": "(c) windy.com webcams",
                "permalink": (row.get("urls") or {}).get("detail"),
            })
        return webcams


# --------------------------------------------------------------------------
# Small numeric helpers. KartaView returns every numeric field as a *string*
# ("distance": "6.49"), so nothing here may assume it got a number.
# --------------------------------------------------------------------------

def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms_to_date(value):
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


