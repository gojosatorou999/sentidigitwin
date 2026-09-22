"""Live camera catalogs from public road-authority feeds.

``twin/ingest/cctv.py`` answers "is there a camera here, and whose is it?"
from OpenStreetMap. It cannot answer "what does it see right now", because
OSM maps installations, not feeds. ``twin/cameras.py`` answers that -- but
only for feeds an operator has already hand-listed in a JSON file.

This module is the third case, and the one that scales: a road authority
that publishes its whole camera network as a keyless catalog, each entry
carrying a position and the URL of that camera's current frame. One entry in
:data:`PROVIDERS` is one authority. The adapter fetches the catalogs that
matter, normalises them into exactly the shape ``twin/cameras.py`` already
produces, and hands them to the same console panel.

Three rules hold for every provider here, and they are what make the layer
trustworthy rather than decorative:

* **Keyless.** Every catalog below is public and unauthenticated, so this
  works on a fresh clone with an empty ``.env`` -- the same property that
  made the OSM adapter worth writing.
* **Coverage-gated.** A provider declares the bbox it actually serves and is
  only called when that bbox intersects the one being asked about. Hyderabad
  and Bengaluru match no provider, so nothing is fetched and nothing is
  drawn there. **A foreign city's cameras never stand in for a local one**
  -- that substitution, not an empty panel, is the failure worth preventing.
* **Never proxied.** Frame URLs are *built* from an official origin and a
  validated id, never copied from whatever string the upstream returned, and
  the server never fetches them. The browser loads a frame directly from the
  authority that published it, exactly as ``twin/cameras.py`` already
  promises. The twin does not become an access route to a camera network.

Adding an authority is one :class:`Provider` entry plus its parser. When an
Indian authority publishes a catalog -- a GHMC ICCC feed, a BTP one -- it
joins this list and the two modelled cities light up for real, with no other
code change.

Ported from the CCTV provider layer of ``bilawalsidhu/gods-eye-view``
(server/providers/cctv/), whose field mappings and liveness filters are the
surveyed part; the coverage gate, the cache and the snapshot contract are
this codebase's.
"""

import io
import logging
import math
import re

from .. import config
from .. import geo
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.cctv_live")

#: Frames must settle inside the console's refresh cadence, so a camera that
#: publishes only a slow video stream is registered as ``image`` when it also
#: publishes a still, and skipped when it does not. Matches STREAM_TYPES in
#: twin/cameras.py -- a value this module cannot produce is a value the
#: console cannot render.
FEED_TYPES = ("image", "mjpeg", "hls")

#: Degrees. Slack added to a provider's declared service area before testing
#: it against the requested bbox, so a camera just outside a city boundary
#: still appears when an operator pans to the edge of it.
COVERAGE_PAD_DEG = 0.25

_COMPASS = {
    "n": 0.0, "north": 0.0, "nb": 0.0, "northbound": 0.0,
    "ne": 45.0, "northeast": 45.0,
    "e": 90.0, "east": 90.0, "eb": 90.0, "eastbound": 90.0,
    "se": 135.0, "southeast": 135.0,
    "s": 180.0, "south": 180.0, "sb": 180.0, "southbound": 180.0,
    "sw": 225.0, "southwest": 225.0,
    "w": 270.0, "west": 270.0, "wb": 270.0, "westbound": 270.0,
    "nw": 315.0, "northwest": 315.0,
}

#: Travel-direction tokens as they appear *inside* a camera name
#: ("US-290 EB @ Parmer"). Bare cardinals are deliberately absent: road names
#: are full of them ("N Lamar", "West Ave") and matching those would point
#: half a city's cameras at a street's name rather than its traffic.
_TRAVEL_TOKEN = re.compile(
    r"\b(NB|SB|EB|WB|NORTHBOUND|SOUTHBOUND|EASTBOUND|WESTBOUND)\b", re.I)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _num(value):
    """A finite float, or None. ``float(None)`` and ``float('')`` must not
    become 0.0 -- that silently parks a camera on Null Island."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def heading_from_text(value, allow_bare_cardinal=False):
    """Degrees clockwise from north parsed out of free text, or None.

    ``allow_bare_cardinal`` is for fields whose *only* job is direction
    ("West", "Southbound"). It stays off when reading a camera's name, for
    the reason documented on :data:`_TRAVEL_TOKEN`.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    if allow_bare_cardinal:
        key = text.lower().replace("-", "").replace(" ", "")
        if key in _COMPASS:
            return _COMPASS[key]
        number = _num(text)
        if number is not None:
            return number % 360.0

    match = _TRAVEL_TOKEN.search(text)
    if match:
        return _COMPASS[match.group(1).lower()]
    return None


def fallback_heading(camera_id):
    """A stable pseudo-heading derived from the camera id.

    A camera with no published bearing still has to be drawn somewhere. Using
    a constant would stack every such camera's view cone on one bearing, which
    reads as a survey finding that does not exist; hashing the id instead fans
    them out and is stable across restarts. Always paired with
    ``heading_confidence="low"`` so the panel can say the cone is a guess.
    """
    digest = 0
    for char in str(camera_id):
        digest = (digest * 31 + ord(char)) & 0xFFFFFFFF
    return float(digest % 360)


def _pose(has_heading):
    """Declared optical assumptions, not measurements.

    Two personalities, matching the source project: a camera with a published
    bearing is assumed to be a purposefully-aimed traffic camera (narrower,
    longer, higher); one without is assumed to be a generic wide mount. Both
    are surfaced through the API as assumptions so the legend can say so.
    """
    if has_heading:
        return {"pitch": -24.0, "fov": 56.0, "range_m": 210.0, "mount_height_m": 10.0}
    return {"pitch": -18.0, "fov": 44.0, "range_m": 145.0, "mount_height_m": 8.0}


def make_camera(camera_id, name, lat, lon, url, provider, license_note,
                heading=None, feed_type="image", city=None, operator=None,
                ground_elevation_m=None, code=None, credit=None):
    """One normalised camera, in the shape twin/cameras.py already emits.

    Returns None when the record cannot be drawn honestly -- no position, no
    frame URL, or a feed type the console has no player for. A caller that
    silently kept those would put a permanently-broken tile on the panel.
    """
    lat, lon = _num(lat), _num(lon)
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    if not url or not str(url).lower().startswith("https://"):
        return None
    if feed_type not in FEED_TYPES:
        return None

    has_heading = heading is not None and _num(heading) is not None
    direction = _num(heading) % 360.0 if has_heading else fallback_heading(camera_id)
    pose = _pose(has_heading)

    return {
        "id": str(camera_id),
        "name": str(name or camera_id),
        "city": (city or "").strip().lower() or None,
        "lat": lat,
        "lon": lon,
        "direction": direction,
        "heading_confidence": "high" if has_heading else "low",
        "url": str(url),
        "type": feed_type,
        "operator": operator or provider,
        "attribution": license_note,
        "provider": provider,
        "code": code,
        "credit": credit or None,
        "ground_elevation_m": _num(ground_elevation_m),
        "fov": pose["fov"],
        "range_m": pose["range_m"],
        "pitch": pose["pitch"],
        "mount_height_m": pose["mount_height_m"],
        "live": True,
        "source": "provider",
    }


# --------------------------------------------------------------------------
# Provider parsers
#
# Each takes the authority's parsed payload and returns normalised cameras.
# They are pure, so every liveness filter below is unit-testable without a
# network -- and each filter is load-bearing: a catalog row that is planned,
# retired or offline still carries a frame URL, and that URL still returns an
# image. It is just an image from a camera that no longer exists, or one
# frozen years ago. Rendering it as live is the worst thing this layer could
# do, so the status filters are not tidiness, they are the feature.
# --------------------------------------------------------------------------

AUSTIN_ROWS_URL = ("https://data.austintexas.gov/api/views/b4k4-adkb/"
                   "rows.json?accessType=DOWNLOAD")
AUSTIN_FRAME_ORIGIN = "https://cctv.austinmobility.io/image/"
#: Austin publishes position as WKT, not as numeric columns.
_WKT_POINT = re.compile(r"^\s*POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)\s*$", re.I)


def parse_austin(payload):
    """City of Austin traffic cameras (Socrata rows.json).

    The dataset carries DESIRED (approved but never built), REMOVED and VOID
    rows alongside live ones. A missing status column keeps the row, so a
    schema change fails open rather than blanking the city.

    Nothing in this dataset is a camera bearing. ``signal_eng_area``
    ("NORTHEAST") is the engineering district the pole belongs to, not the
    direction the lens points, so every Austin camera takes the id-hash
    fallback and low confidence rather than a compass reading that would be
    wrong for three quarters of the network.
    """
    columns = (((payload or {}).get("meta") or {}).get("view") or {}).get("columns") or []
    names = [c.get("fieldName") for c in columns]
    cameras = []

    for row in (payload or {}).get("data") or []:
        if not isinstance(row, list):
            continue
        record = dict(zip(names, row))

        status = str(record.get("camera_status") or "").strip().upper()
        if status and status != "TURNED_ON":
            continue

        camera_id = str(record.get("camera_id") or "").strip()
        if not camera_id:
            continue

        lat, lon = _num(record.get("location_latitude")), _num(record.get("location_longitude"))
        if lat is None or lon is None:
            match = _WKT_POINT.match(str(record.get("location") or ""))
            if match:
                lon, lat = _num(match.group(1)), _num(match.group(2))

        # The row names its own frame; it is used only once pinned to the
        # official origin, and the id-built URL is the fallback.
        frame = str(record.get("screenshot_address") or "").strip()
        if not frame.startswith(AUSTIN_FRAME_ORIGIN):
            frame = "%s%s.jpg" % (AUSTIN_FRAME_ORIGIN, camera_id)

        name = record.get("location_name") or record.get("landmark")
        camera = make_camera(
            "austin-%s" % camera_id,
            name if isinstance(name, str) and name else "Austin camera %s" % camera_id,
            lat, lon, frame,
            "Austin Transportation & Public Works",
            "Public city traffic camera frame",
            city="austin", ground_elevation_m=150, code=camera_id)
        if camera:
            cameras.append(camera)
    return cameras


CALTRANS_URL = "https://cwwp2.dot.ca.gov/data/d%s/cctv/cctvStatusD%02d.json"
CALTRANS_FRAME_ORIGIN = "https://cwwp2.dot.ca.gov/"
CALTRANS_DISTRICTS = (3, 4, 7, 11)

#: Caltrans reports elevation in FEET. Read as metres, the Sierra passes
#: (7,427 ft) would place a camera above Mt Whitney.
_FEET_TO_M = 0.3048


def parse_caltrans(payload, district):
    """Caltrans district CCTV. One identical-schema feed per district."""
    cameras = []
    for row in (payload or {}).get("data") or []:
        cctv = (row or {}).get("cctv") or {}
        if str(cctv.get("inService", "")).strip().lower() != "true":
            continue
        location = cctv.get("location") or {}

        image_url = str(((cctv.get("imageData") or {}).get("static") or {})
                        .get("currentImageURL") or "")
        # Pin to the official host. The server never fetches these, but the
        # URL does reach a browser, and it is the one field the upstream
        # controls freely. It also drops rows with no still image at all.
        if not image_url.startswith(CALTRANS_FRAME_ORIGIN):
            continue

        location_name = str(location.get("locationName") or "").strip()
        match = re.match(r"^([A-Za-z0-9_-]+)\s*--", location_name)
        code = (match.group(1) if match else "x%d" % len(cameras)).lower()
        label = re.sub(r"^([A-Za-z0-9_-]+)\s*--\s*", "", location_name) or \
            "Caltrans D%s %s" % (district, code)
        nearby = str(location.get("nearbyPlace") or "").strip()

        elevation_ft = _num(location.get("elevation"))
        elevation_m = (max(-100.0, min(4000.0, elevation_ft * _FEET_TO_M))
                       if elevation_ft is not None else 150.0)

        camera = make_camera(
            "caltrans-d%s-%s" % (district, code),
            "%s (%s)" % (label, nearby) if nearby else label,
            location.get("latitude"), location.get("longitude"),
            image_url, "Caltrans", "Public Caltrans highway camera frame",
            heading=heading_from_text(location.get("direction"), True),
            city=nearby.lower() or None, ground_elevation_m=elevation_m, code=code)
        if camera:
            cameras.append(camera)
    return cameras


TFL_JAMCAM_URL = "https://api.tfl.gov.uk/Place/Type/JamCam"
TFL_FRAME_ORIGIN = "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/"


def parse_tfl(payload):
    """Transport for London JamCams.

    JamCam records carry no bearing of any kind, so every camera here takes
    the id-hash fallback and low confidence. Stills are used rather than the
    published videoUrl: a still is what the console's refresh cadence can
    actually keep current.
    """
    cameras = []
    for place in payload or []:
        props = {p.get("key"): p.get("value")
                 for p in (place or {}).get("additionalProperties") or []
                 if p.get("key")}
        if str(props.get("available", "")).strip().lower() != "true":
            continue

        image_url = str(props.get("imageUrl") or "")
        if not image_url.startswith(TFL_FRAME_ORIGIN):
            continue

        raw_id = re.sub(r"^JamCams_", "", str(place.get("id") or ""))
        if not raw_id:
            continue

        camera = make_camera(
            "tfl-%s" % raw_id,
            str(place.get("commonName") or "JamCam %s" % raw_id),
            place.get("lat"), place.get("lon"), image_url,
            "Transport for London", "Powered by TfL Open Data",
            city="london", ground_elevation_m=15, code=raw_id)
        if camera:
            cameras.append(camera)
    return cameras


ONTARIO_URL = "https://511on.ca/api/v2/get/cameras?format=json&lang=en"
ONTARIO_FRAME_ORIGIN = "https://511on.ca/map/Cctv/"


def _ontario_view_url(value):
    """Rebuild an Ontario view URL on the official host, or return ''.

    The upstream URL is parsed for its view id alone and the frame URL is
    then constructed; nothing of the supplied host or path survives.
    """
    from urllib.parse import urlparse, quote, unquote
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme != "https":
        return ""
    host = (parsed.hostname or "").lower()
    if host != "511on.ca" and not host.endswith(".traveliq.co"):
        return ""
    match = re.match(r"^/map/Cctv/([^/?#]+)$", parsed.path)
    if not match:
        return ""
    view_id = unquote(match.group(1))
    if not re.match(r"^[A-Za-z0-9_.-]+$", view_id):
        return ""
    return ONTARIO_FRAME_ORIGIN + quote(view_id, safe="")


def parse_ontario(payload):
    """Ontario 511 highway cameras.

    A camera can publish several views; the first enabled one whose label
    does not say "down" is chosen, because a view labelled down serves a
    frame that will not refresh.
    """
    cameras = []
    for row in payload or []:
        raw_id = str(row.get("Id") or row.get("id") or "").strip()
        if not raw_id:
            continue

        views = []
        for view in row.get("Views") or row.get("views") or []:
            status = str(view.get("Status") or view.get("status") or "").strip().lower()
            if status != "enabled":
                continue
            url = _ontario_view_url(view.get("Url") or view.get("url"))
            if url:
                description = str(view.get("Description") or view.get("description") or "").strip()
                views.append((url, description))
        if not views:
            continue
        url, description = next(
            (v for v in views if not re.search(r"\bdown\b", v[1], re.I)), views[0])

        location = str(row.get("Location") or row.get("location") or "").strip()
        roadway = str(row.get("Roadway") or row.get("roadway") or "").strip()
        view_label = "" if re.search(r"\bdown\b", description, re.I) else description
        label = " - ".join(x for x in (
            location or roadway or "Ontario 511 camera %s" % raw_id, view_label) if x)

        heading = heading_from_text(row.get("Direction") or row.get("direction"), True)
        if heading is None:
            heading = heading_from_text(description, True)

        camera = make_camera(
            "ontario-%s" % raw_id, label,
            row.get("Latitude") or row.get("latitude"),
            row.get("Longitude") or row.get("longitude"),
            url, "Ontario 511", "Open Government Licence - Ontario",
            heading=heading, city=(location or roadway or "ontario").lower(),
            ground_elevation_m=200, code=raw_id)
        if camera:
            cameras.append(camera)
    return cameras


FINTRAFFIC_URL = "https://tie.digitraffic.fi/api/weathercam/v1/stations"
FINTRAFFIC_FRAME_ORIGIN = "https://weathercam.digitraffic.fi/"
#: Digitraffic asks every client to identify itself.
DIGITRAFFIC_USER = "sentinel-ai-twin"
#: Metres. 0 in this payload means "not reported", not sea level, so the
#: observed median across reporting stations stands in for it.
FINTRAFFIC_DEFAULT_ELEVATION_M = 90
_FINTRAFFIC_PRESET = re.compile(r"^C\d{7}$")


def parse_fintraffic(payload):
    """Fintraffic road weather cameras (Digitraffic), all of Finland.

    One preset -- one fixed view of a station -- is one camera; the presets
    of a station share its position. The per-preset ``direction`` field is
    road-register relative ("towards higher road addresses"), not a bearing,
    so it is deliberately not read as one.
    """
    cameras = []
    for feature in (payload or {}).get("features") or []:
        props = (feature or {}).get("properties") or {}
        station_id = str(props.get("id") or "").strip()
        if not station_id:
            continue
        if str(props.get("collectionStatus") or "").upper() != "GATHERING":
            continue

        coords = ((feature.get("geometry") or {}).get("coordinates")) or []
        lon = _num(coords[0]) if len(coords) > 0 else None
        lat = _num(coords[1]) if len(coords) > 1 else None
        reported = _num(coords[2]) if len(coords) > 2 else None
        elevation = (min(1400.0, reported) if reported and reported > 0
                     else FINTRAFFIC_DEFAULT_ELEVATION_M)

        station_name = str(props.get("name") or station_id)
        for preset in props.get("presets") or []:
            if preset.get("inCollection") is not True:
                continue
            preset_id = str(preset.get("id") or "").strip()
            # Strict shape: also the guard that keeps a hostile id out of the
            # frame URL's path, which is built rather than copied.
            if not _FINTRAFFIC_PRESET.match(preset_id):
                continue
            if not preset_id.startswith(station_id):
                continue

            camera = make_camera(
                "fintraffic-%s" % preset_id.lower(),
                "%s (%s)" % (station_name, preset_id[-2:]),
                lat, lon, "%s%s.jpg" % (FINTRAFFIC_FRAME_ORIGIN, preset_id),
                "Fintraffic", "Fintraffic / digitraffic.fi (CC BY 4.0)",
                city="finland", ground_elevation_m=elevation, code=preset_id)
            if camera:
                cameras.append(camera)
    return cameras


DRIVEBC_URL = "https://www.drivebc.ca/api/webcams/"
DRIVEBC_FRAME_URL = "https://www.drivebc.ca/images/%d.jpg"
_DRIVEBC_CREDIT = re.compile(
    r"courtesy|provided by|presented in cooperation|city of|parks canada", re.I)


def _drivebc_credit(raw):
    """Third-party image attribution only.

    DriveBC's ``credit`` field mixes real attribution ("Images courtesy of
    TransLink") with operational notes ("relies on solar power"). Only the
    attribution kind is carried onto the camera, so a partner-owned feed
    names its owner; the rest is dropped rather than shown as a credit.
    """
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(raw or ""))).strip()
    return text if text and _DRIVEBC_CREDIT.search(text) else None


def parse_drivebc(payload):
    """DriveBC highway cameras (British Columbia)."""
    cameras = []
    for row in payload or []:
        if row.get("is_on") is not True or row.get("should_appear") is not True:
            continue
        camera_id = row.get("id")
        if isinstance(camera_id, bool) or not isinstance(camera_id, int) or camera_id <= 0:
            continue

        coords = ((row.get("location") or {}).get("coordinates")) or []
        if len(coords) < 2:
            continue

        region = str(row.get("region_name") or "").strip()
        elevation = _num(row.get("elevation"))
        city = "bc border" if region == "Border Cams" else (region or "british columbia")
        camera = make_camera(
            "drivebc-%d" % camera_id,
            str(row.get("name") or "").strip() or "DriveBC camera %d" % camera_id,
            coords[1], coords[0], DRIVEBC_FRAME_URL % camera_id,
            "DriveBC", "DriveBC, Open Government Licence - British Columbia",
            heading=heading_from_text(row.get("orientation"), True),
            city=city.lower(),
            ground_elevation_m=(max(-100.0, min(4000.0, elevation))
                                if elevation is not None else 0.0),
            code=str(camera_id), credit=_drivebc_credit(row.get("credit")))
        if camera:
            cameras.append(camera)
    return cameras


NSW_URL = "https://data.livetraffic.com/cameras/traffic-cam.json"
NSW_FRAME_ORIGIN = "https://webcams.transport.nsw.gov.au/"
#: Longest ``view`` sentence still usable as a label. NSW occasionally
#: repurposes the field for a multi-paragraph works notice.
NSW_MAX_VIEW_LABEL = 140


def parse_nsw(payload):
    """Live Traffic NSW cameras (Transport for NSW). GeoJSON."""
    cameras = []
    for feature in (payload or {}).get("features") or []:
        props = (feature or {}).get("properties") or {}
        image_url = str(props.get("href") or "")
        if not image_url.startswith(NSW_FRAME_ORIGIN):
            continue

        title = str(props.get("title") or "").strip()
        view = str(props.get("view") or "").strip()
        label = title or "NSW camera"
        if view and len(view) <= NSW_MAX_VIEW_LABEL:
            label = "%s - %s" % (label, view)

        coords = ((feature.get("geometry") or {}).get("coordinates")) or []
        if len(coords) < 2:
            continue

        slug = re.sub(r"[^A-Za-z0-9_.-]", "", image_url[len(NSW_FRAME_ORIGIN):]) or title
        heading = heading_from_text(view, False)
        if heading is None:
            heading = heading_from_text(props.get("direction"), True)

        camera = make_camera(
            "nsw-%s" % slug.lower(), label, coords[1], coords[0], image_url,
            "Transport for NSW", "Live Traffic NSW (CC BY 4.0)",
            heading=heading, city=str(props.get("region") or "new south wales").lower(),
            ground_elevation_m=20, code=title or slug)
        if camera:
            cameras.append(camera)
    return cameras


CALGARY_URL = "https://data.calgary.ca/resource/k7p9-kppz.json?$limit=500"
CALGARY_FRAME_ORIGIN = "https://trafficcam.calgary.ca/"


def parse_calgary(payload):
    """Open Calgary traffic cameras.

    Most rows publish their frame as ``http://``; that host serves HTTPS and
    301s to it, so the scheme is upgraded before the origin is pinned.
    """
    cameras = []
    for row in payload or []:
        raw = row.get("camera_url")
        url = str((raw.get("url") if isinstance(raw, dict) else raw) or "").strip()
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        if not url.startswith(CALGARY_FRAME_ORIGIN):
            continue

        coords = (row.get("point") or {}).get("coordinates") or []
        if len(coords) >= 2:
            lat, lon = _num(coords[1]), _num(coords[0])
        else:
            lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))

        label = str(row.get("camera_location") or row.get("quadrant")
                    or "Calgary camera").strip()
        slug = re.sub(r"[^A-Za-z0-9_.-]", "", url[len(CALGARY_FRAME_ORIGIN):]) or label
        camera = make_camera(
            "calgary-%s" % slug.lower(), label, lat, lon, url,
            "City of Calgary", "Open Calgary (Open Government Licence - Calgary)",
            heading=heading_from_text(label, False), city="calgary",
            ground_elevation_m=1045, code=label)
        if camera:
            cameras.append(camera)
    return cameras


HONGKONG_CATALOG_URL = ("https://static.data.gov.hk/td/traffic-snapshot-images/"
                        "code/Traffic_Camera_Locations_En.csv")
HONGKONG_FRAME_ORIGIN = "https://tdcctv.data.one.gov.hk/"

#: The key is the only part of the frame URL this module takes from upstream,
#: so it is bounded before being concatenated onto the origin. The bound is
#: "uppercase alphanumerics only, 3-16 of them", which is what actually
#: matters: no slash, dot, or query character can reach the path, so no key
#: can traverse out of the origin or graft a query onto it.
#:
#: It is deliberately *not* tighter than that. TD publishes at least five key
#: shapes -- ``H429F``, ``H422F2``, ``ST712F1``, ``AID01101`` and longer
#: district-prefixed AID ids -- and an earlier pattern modelled on the first
#: one silently dropped 812 of 1,013 cameras. Each of those shapes was
#: confirmed to serve a real JPEG before the bound was widened to admit it.
_HK_KEY = re.compile(r"^[A-Z0-9]{3,16}$")


def parse_hongkong(rows):
    """Transport Department traffic snapshot images.

    The catalog is a UTF-16 tab-separated file, not JSON -- the only one in
    this registry that is. It carries ``url`` per row, but that column is
    ignored on purpose: the frame is rebuilt as origin + validated key, so a
    changed or hostile upstream string can never become the ``src`` of a tag
    the console renders. Rule 3 in this module's docstring is the whole
    reason the HK feed was worth adding over feeds whose ids are opaque.
    """
    cameras = []
    for row in rows or []:
        key = str(row.get("key") or "").strip().upper()
        if not _HK_KEY.match(key):
            continue

        label = str(row.get("description") or "").strip()
        # Descriptions end in the key already ("Nathan Road ... [K106F]"),
        # which reads as noise once the chip is labelled -- strip it back off.
        label = re.sub(r"\s*\[%s\]\s*$" % re.escape(key), "", label) or key
        district = str(row.get("district") or "").strip()
        name = "%s, %s" % (label, district) if district else label

        camera = make_camera(
            "hongkong-%s" % key.lower(), name,
            _num(row.get("latitude")), _num(row.get("longitude")),
            "%s%s.JPG" % (HONGKONG_FRAME_ORIGIN, key),
            "Transport Department, HKSAR",
            "(c) Transport Department, HKSAR "
            "(data.gov.hk Terms of Use -- free to use with attribution)",
            heading=heading_from_text(label, False), city="hongkong",
            ground_elevation_m=20, code=key)
        if camera:
            cameras.append(camera)
    return cameras


SINGAPORE_URL = "https://api.data.gov.sg/v1/transport/traffic-images"
SINGAPORE_FRAME_ORIGIN = "https://images.data.gov.sg/"


def parse_singapore(payload):
    """LTA traffic camera images, republished keyless through data.gov.sg.

    **A documented exception to the "build, never copy" rule.** Every other
    provider here exposes a stable per-camera frame path that this module
    reconstructs. Singapore does not: each refresh publishes the frame under
    a fresh UUID, so the URL genuinely cannot be derived from the camera id.

    The weaker guarantee that replaces it is an origin pin -- the URL is used
    only when it is already on the authority's own image host, which is
    checked here rather than assumed. That still bounds what can reach the
    browser to one government origin, and the server still never fetches it.
    It is weaker than the others and is called out as such rather than
    quietly levelled in.
    """
    items = (payload or {}).get("items") or []
    rows = items[0].get("cameras") if items else []
    cameras = []
    for row in rows or []:
        url = str(row.get("image") or "").strip()
        if not url.startswith(SINGAPORE_FRAME_ORIGIN):
            continue

        camera_id = re.sub(r"[^A-Za-z0-9_.-]", "", str(row.get("camera_id") or ""))
        if not camera_id:
            continue

        location = row.get("location") or {}
        camera = make_camera(
            "singapore-%s" % camera_id.lower(), "Camera %s" % camera_id,
            _num(location.get("latitude")), _num(location.get("longitude")),
            url, "Land Transport Authority",
            "(c) Land Transport Authority (data.gov.sg Open Data Licence)",
            city="singapore", ground_elevation_m=15, code=camera_id)
        if camera:
            cameras.append(camera)
    return cameras


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

class Provider(object):
    """One authority that publishes a keyless live camera catalog.

    ``coverage`` is the service area the authority actually operates in, as
    (min_lon, min_lat, max_lon, max_lat). It is the whole coverage gate: a
    provider whose area does not meet the requested bbox is never called, so
    an operator looking at Hyderabad never pays for -- or sees -- a camera in
    Helsinki.
    """

    def __init__(self, key, name, coverage, fetch, region):
        self.key = key
        self.name = name
        self.coverage = coverage
        self.fetch = fetch
        self.region = region

    def covers(self, bbox, pad_deg=COVERAGE_PAD_DEG):
        if not bbox:
            return False
        min_lon, min_lat, max_lon, max_lat = bbox
        c_min_lon, c_min_lat, c_max_lon, c_max_lat = self.coverage
        return not (min_lon > c_max_lon + pad_deg or max_lon < c_min_lon - pad_deg
                    or min_lat > c_max_lat + pad_deg or max_lat < c_min_lat - pad_deg)

    def as_dict(self):
        return {"key": self.key, "name": self.name, "region": self.region,
                "coverage": list(self.coverage)}


def _get_json(session, url, timeout_s, headers=None):
    response = session.get(url, timeout=timeout_s,
                           headers=dict({"Accept": "application/json"}, **(headers or {})))
    response.raise_for_status()
    return response.json()


def _fetch_austin(session, timeout_s):
    return parse_austin(_get_json(session, AUSTIN_ROWS_URL, timeout_s))


def _fetch_caltrans(session, timeout_s):
    cameras = []
    for district in CALTRANS_DISTRICTS:
        # Districts fail independently: one district's outage must not darken
        # the other three, which is the whole reason this is a loop and not a
        # single call.
        try:
            payload = _get_json(session, CALTRANS_URL % (district, district), timeout_s)
        except Exception as exc:                      # noqa: BLE001 - per-district isolation
            log.warning("caltrans district %s failed (%s)", district, exc)
            continue
        cameras.extend(parse_caltrans(payload, district))
    if not cameras:
        raise RuntimeError("every Caltrans district failed")
    return cameras


def _fetch_tfl(session, timeout_s):
    return parse_tfl(_get_json(session, TFL_JAMCAM_URL, timeout_s))


def _fetch_ontario(session, timeout_s):
    return parse_ontario(_get_json(session, ONTARIO_URL, timeout_s))


def _fetch_fintraffic(session, timeout_s):
    return parse_fintraffic(_get_json(
        session, FINTRAFFIC_URL, timeout_s, {"Digitraffic-User": DIGITRAFFIC_USER}))


def _fetch_drivebc(session, timeout_s):
    return parse_drivebc(_get_json(session, DRIVEBC_URL, timeout_s))


def _fetch_nsw(session, timeout_s):
    return parse_nsw(_get_json(session, NSW_URL, timeout_s))


def _fetch_calgary(session, timeout_s):
    return parse_calgary(_get_json(session, CALGARY_URL, timeout_s))


def _read_hongkong_catalog(body):
    """The TD catalog's rows, as dicts.

    Separated from the request so the decode is testable on bytes. The file
    is UTF-16 and tab-separated, and carries a second BOM *inside* the
    decoded text on top of the one the codec consumes -- which silently
    renames the first column to ``﻿key`` and made every row fail key
    validation until it was stripped here.
    """
    import csv

    text = body.decode("utf-16") if isinstance(body, bytes) else body
    text = text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text), delimiter="	"))


def _fetch_hongkong(session, timeout_s):
    response = session.get(HONGKONG_CATALOG_URL, timeout=timeout_s,
                           headers={"Accept": "text/csv"})
    response.raise_for_status()
    return parse_hongkong(_read_hongkong_catalog(response.content))


def _fetch_singapore(session, timeout_s):
    return parse_singapore(_get_json(session, SINGAPORE_URL, timeout_s))


#: Every registered authority. Ordering is merge order; the cap is shared
#: round-robin (see :func:`_allocate`) so position never decides who survives.
#:
#: Deliberately absent, and why -- each omission is a rule being kept rather
#: than an authority being missed:
#:
#: * **TxDOT (Texas).** Its snapshot endpoint returns JSON carrying a base64
#:   JPEG, not an image body, so a browser cannot render it. Showing it would
#:   mean the server fetching and decoding every frame, which is exactly the
#:   proxying this module promises never to do.
#: * **Tallinn, Warendorf.** Both are hand-curated catalog files that ship
#:   inside the source project rather than feeds an authority publishes. That
#:   is bundled sample data, and bundled sample data is not live data.
#: * **Tarktee (Estonia).** DATEX2 XML. Worth adding, but it needs an XML
#:   parser rather than the one JSON path every entry here shares.
PROVIDERS = (
    Provider("austin", "Austin Transportation & Public Works",
             (-98.2, 30.0, -97.4, 30.6), _fetch_austin, "Austin, Texas, USA"),
    Provider("caltrans", "Caltrans",
             (-124.5, 32.4, -114.1, 42.1), _fetch_caltrans, "California, USA"),
    Provider("tfl", "Transport for London",
             (-0.55, 51.25, 0.35, 51.72), _fetch_tfl, "London, UK"),
    Provider("ontario", "Ontario 511",
             (-95.6, 41.0, -74.0, 57.5), _fetch_ontario, "Ontario, Canada"),
    Provider("fintraffic", "Fintraffic",
             (19.0, 59.5, 31.6, 70.1), _fetch_fintraffic, "Finland"),
    Provider("drivebc", "DriveBC",
             (-139.1, 48.2, -114.0, 60.1), _fetch_drivebc, "British Columbia, Canada"),
    Provider("nsw", "Transport for NSW",
             (140.9, -37.6, 153.7, -28.1), _fetch_nsw, "New South Wales, Australia"),
    Provider("calgary", "City of Calgary",
             (-114.32, 50.84, -113.85, 51.22), _fetch_calgary, "Calgary, Alberta, Canada"),
    Provider("hongkong", "Transport Department, HKSAR",
             (113.82, 22.13, 114.45, 22.58), _fetch_hongkong, "Hong Kong SAR"),
    Provider("singapore", "Land Transport Authority",
             (103.59, 1.19, 104.09, 1.48), _fetch_singapore, "Singapore"),
)


def enabled_providers():
    """Registered providers the configuration allows, in registry order."""
    if not config.CCTV_LIVE_ENABLED:
        return ()
    allowed = {k.strip().lower() for k in config.CCTV_LIVE_PROVIDERS.split(",") if k.strip()}
    if not allowed:
        return PROVIDERS
    return tuple(p for p in PROVIDERS if p.key in allowed)


def reference_provider():
    """The provider that stands in when no authority covers the city, or None.

    Resolved through :func:`enabled_providers`, not the raw registry, so the
    ``TWIN_CCTV_LIVE_PROVIDERS`` allowlist still governs it -- a deployment
    that has narrowed the registry does not get the narrowed-out catalog back
    through this door. A configured key that names nothing is logged rather
    than ignored: it is a typo in an env file, and silence there would read
    as "the reference feed is off", which is a different decision.
    """
    if not (config.CCTV_LIVE_ENABLED and config.CCTV_REFERENCE_ENABLED):
        return None

    key = (config.CCTV_REFERENCE_PROVIDER or "").strip().lower()
    if not key:
        return None

    for provider in enabled_providers():
        if provider.key == key:
            return provider

    log.warning("cctv reference provider %r is not a registered, enabled provider", key)
    return None


def providers_for(bbox):
    """The enabled providers whose service area meets ``bbox``.

    No bbox means no providers, deliberately. "Cameras, anywhere" is not a
    question this layer can answer usefully, and treating it as "every
    authority" would have one unscoped request pull eight national catalogs
    -- which is exactly what it did before this was pinned down.
    """
    if not bbox:
        return ()
    return tuple(p for p in enabled_providers() if p.covers(bbox))


def _allocate(by_provider, max_cameras):
    """Fill the cap round-robin across providers, nearest-first within each.

    A straight concatenation-then-truncate would let one dense catalog
    (Ontario publishes thousands) consume the whole budget and evict a sparse
    neighbour entirely. Round-robin means a provider that matched the bbox
    always contributes something.
    """
    ordered = [list(cameras) for cameras in by_provider.values() if cameras]
    if not ordered:
        return []

    merged, index = [], 0
    while len(merged) < max_cameras and any(index < len(c) for c in ordered):
        for cameras in ordered:
            if index < len(cameras):
                merged.append(cameras[index])
                if len(merged) >= max_cameras:
                    break
        index += 1
    return merged


class CctvLiveAdapter(IngestAdapter):
    """Live camera catalogs from the public authorities that match a bbox.

    ``fetch_raw`` returns a dict, never a bare list, so "no authority covers
    this ground" still carries which providers were considered and stays
    distinguishable from a failed lookup -- which ``run()`` reports through
    the snapshot status instead.
    """

    source_key = "cctv_live"
    cache_ttl_s = config.CCTV_LIVE_CACHE_TTL_S
    #: Catalogs are large and several are fetched per call; a retry storm
    #: across eight authorities costs more than one empty layer.
    max_retries = 0

    def fetch_raw(self, bbox=None, lat=None, lon=None, radius_m=None,
                  timeout_s=None, **_):
        timeout_s = timeout_s or config.CCTV_LIVE_TIMEOUT_S
        if bbox is None and lat is not None and lon is not None:
            bbox = _bbox_around(lat, lon, radius_m or 2000)

        matched = providers_for(bbox)
        by_provider, status = {}, {}

        for provider in matched:
            try:
                cameras = provider.fetch(self.session, timeout_s) or []
            except Exception as exc:                  # noqa: BLE001 - per-provider isolation
                log.warning("cctv provider %s failed (%s)", provider.key, exc)
                status[provider.key] = "failed: %s" % str(exc)[:120]
                continue
            if bbox:
                cameras = [c for c in cameras if _in_bbox(c, bbox)]
            if lat is not None and lon is not None:
                for camera in cameras:
                    camera["distance_m"] = geo.distance_m(lat, lon, camera["lat"], camera["lon"])
                cameras.sort(key=lambda c: c.get("distance_m") or 1e9)
            by_provider[provider.key] = cameras
            status[provider.key] = "ok: %d cameras" % len(cameras)

        cameras = _allocate(by_provider, config.CCTV_LIVE_MAX_CAMERAS)
        if lat is not None and lon is not None:
            cameras.sort(key=lambda c: c.get("distance_m") or 1e9)

        return {
            "cameras": cameras,
            "counts_by_provider": {k: len(v) for k, v in by_provider.items()},
            "providers_considered": [p.as_dict() for p in matched],
            "provider_status": status,
            "bbox": list(bbox) if bbox else None,
            "capped": sum(len(v) for v in by_provider.values()) > len(cameras),
            "source": "Public road-authority camera catalogs",
            "attribution": _attribution(cameras),
        }

    def neutral_value(self, bbox=None, **_):
        """No network: an empty, honestly-labelled camera set, not ``None``."""
        return {
            "cameras": [], "counts_by_provider": {},
            "providers_considered": [p.as_dict() for p in providers_for(bbox)],
            "provider_status": {}, "bbox": list(bbox) if bbox else None,
            "capped": False, "unavailable": True,
            "source": "Public road-authority camera catalogs",
            "attribution": [],
        }

    def record_count(self, data):
        return len((data or {}).get("cameras") or [])

    def _cache_key(self, city_id, kwargs):
        """Key on the query scope alone.

        The base implementation hashes every kwarg, one of which is the
        Flask-SQLAlchemy handle whose repr embeds a memory address -- the key
        would then change on every process restart and a 15-minute TTL would
        never survive one. Same reasoning as CctvOsintAdapter._cache_key.
        """
        import hashlib

        bbox = kwargs.get("bbox")
        if bbox is not None:
            raw = "cctvlive:bbox:" + ",".join("%.3f" % float(v) for v in bbox)
        else:
            raw = "cctvlive:pt:%.3f,%.3f,%d" % (
                float(kwargs.get("lat") or 0.0), float(kwargs.get("lon") or 0.0),
                int(kwargs.get("radius_m") or 2000))
        raw += ":" + ",".join(p.key for p in enabled_providers())
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _bbox_around(lat, lon, radius_m):
    """A bbox around a point. One degree of latitude is ~111 km everywhere;
    longitude shrinks with cos(lat). Same approximation, and the same reason
    for it, as twin/ingest/streetview.py."""
    d_lat = radius_m / 111000.0
    d_lon = radius_m / (111000.0 * max(0.1, math.cos(math.radians(lat))))
    return (lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat)


def _in_bbox(camera, bbox):
    min_lon, min_lat, max_lon, max_lat = bbox
    return (min_lon <= camera["lon"] <= max_lon
            and min_lat <= camera["lat"] <= max_lat)


def _attribution(cameras):
    """Every distinct licence line among the cameras actually returned.

    Built from what is on screen rather than from the registry, so the panel
    never credits an authority whose cameras were all filtered out.
    """
    seen = []
    for camera in cameras:
        note = camera.get("attribution")
        if note and note not in seen:
            seen.append(note)
    return seen


def to_feature_collection(data):
    """GeoJSON for the map layer, straight from ``fetch_raw``'s dict."""
    features = []
    for camera in (data or {}).get("cameras") or []:
        properties = {k: v for k, v in camera.items()
                      if k not in ("lat", "lon") and v is not None}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [camera["lon"], camera["lat"]]},
            "properties": properties,
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "attribution": (data or {}).get("attribution") or [],
        "counts_by_provider": (data or {}).get("counts_by_provider") or {},
    }
