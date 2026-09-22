"""Static configuration for the Sentinel digital twin.

Everything here is data, not behaviour. Weights and bands live in one place so
they can be tuned, logged, and later back-tested against TwinCellHistory
(see SYSTEMS.md).
"""

import json
import logging
import os

log = logging.getLogger("twin.config")

# --------------------------------------------------------------------------
# Runtime knobs (section 11). All optional; these are the defaults.
# --------------------------------------------------------------------------

def _int_env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _json_env(name, default):
    """A JSON object from an env var, or `default` if unset or malformed.

    Malformed JSON is logged and ignored rather than raised: these variables
    configure optional layers, and a stray comma in .env must not stop the
    application from booting.
    """
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        value = json.loads(raw)
    except ValueError as exc:
        log.warning("%s is not valid JSON (%s); ignoring", name, exc)
        return default
    return value if isinstance(value, dict) else default


TWIN_ENABLED = os.getenv("TWIN_ENABLED", "1") == "1"
H3_RESOLUTION = _int_env("TWIN_H3_RESOLUTION", 8)
DRILL_RESOLUTION = 9
SCHEDULER_ENABLED = os.getenv("TWIN_SCHEDULER_ENABLED", "1") == "1"
COMPUTE_INTERVAL_MIN = _int_env("TWIN_COMPUTE_INTERVAL_MIN", 5)
WEATHER_INTERVAL_MIN = _int_env("TWIN_WEATHER_INTERVAL_MIN", 15)
AIRQUALITY_INTERVAL_MIN = _int_env("TWIN_AIRQUALITY_INTERVAL_MIN", 30)
FLOOD_INTERVAL_HOURS = _int_env("TWIN_FLOOD_INTERVAL_HOURS", 6)
INCIDENT_INTERVAL_MIN = _int_env("TWIN_INCIDENT_INTERVAL_MIN", 2)
RADAR_INTERVAL_MIN = _int_env("TWIN_RADAR_INTERVAL_MIN", 10)

CACHE_DIR = os.getenv("TWIN_CACHE_DIR", os.path.join("data", "twin", "cache"))
BOUNDARY_DIR = os.getenv("TWIN_BOUNDARY_DIR", os.path.join("data", "twin", "boundaries"))
HTTP_TIMEOUT_S = float(os.getenv("TWIN_HTTP_TIMEOUT_S", "8"))

# Optional keyed sources (section 4.2). Absent key => layer hidden, never an error.
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY") or None
TOMORROW_API_KEY = os.getenv("TOMORROW_API_KEY") or None
DATA_GOV_IN_KEY = os.getenv("DATA_GOV_IN_KEY") or None

# Street-level imagery / webcams (twin/ingest/streetview.py). Both optional:
# the panel falls back to keyless KartaView, and then to a keyless Google
# Street View deep link, so an unkeyed deployment still shows ground truth.
MAPILLARY_TOKEN = os.getenv("MAPILLARY_TOKEN") or None
WINDY_WEBCAMS_KEY = os.getenv("WINDY_WEBCAMS_KEY") or None

#: Metres around a cell centroid searched for street-level imagery.
STREETVIEW_RADIUS_M = _int_env("TWIN_STREETVIEW_RADIUS_M", 350)

#: Metres around a cell centroid searched for OSM surveillance cameras
#: (twin/ingest/cctv.py). Keyless -- there is no key to omit, so unlike the
#: imagery providers this layer is always available.
CCTV_RADIUS_M = _int_env("TWIN_CCTV_RADIUS_M", 400)

# --------------------------------------------------------------------------
# Official external alerts -- NDMA SACHET (twin/ingest/sachet.py)
#
# Keyless, public-domain, government-authoritative CAP 1.2 feeds. This is the
# only source in the module whose records are *statements by an authority*
# rather than measurements, so it is kept structurally distinct from citizen
# reports all the way to the UI: an operator must always be able to see that a
# warning came from IMD rather than from an anonymous submission.
# --------------------------------------------------------------------------

ALERTS_ENABLED = os.getenv("TWIN_ALERTS_ENABLED", "1") == "1"
ALERT_POLL_MIN = _int_env("TWIN_ALERT_POLL_MIN", 5)

#: %s is the lower-case state name. The per-state feeds are used rather than
#: rss_india.xml because they are already scoped to the two modelled cities;
#: the all-India feed is ~10x the volume for the same two cities' alerts.
SACHET_RSS_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_%s.xml"

SACHET_STATES = tuple(
    part.strip().lower()
    for part in os.getenv("TWIN_SACHET_STATES", "telangana,karnataka").split(",")
    if part.strip()
)

#: Extra strings that mean "this city" in an alert with no polygon. IMD writes
#: at district scale ("Bengaluru Rural, Bengaluru Urban districts"), and the
#: old city names still appear in state-authority text, so matching only the
#: twin's own display name drops real warnings.
CITY_ALERT_ALIASES = {
    "hyderabad": ("hyderabad", "ghmc", "rangareddy", "ranga reddy", "medchal",
                  "secunderabad", "cyberabad"),
    "bengaluru": ("bengaluru", "bangalore", "bbmp", "bengaluru urban",
                  "bengaluru rural", "bangalore urban", "bangalore rural"),
}

#: LGD (Local Government Directory) district codes per city.
#:
#: These matter more than they look: IMD routinely writes an alert's area as
#: "23 districts of Telangana" and names none of them -- the cap:geocode list
#: is then the *only* way to tell whether this city is in it.
#:
#: Only codes that have been verified are listed. Bengaluru Urban (525) and
#: Bengaluru Rural (526) were verified against the LGD directory.
#: Hyderabad's are deliberately absent rather than guessed: a wrong code
#: silently attributes another district's warning to this city. Run
#: scripts/learn_lgd_codes.py to derive them from the feed itself, or set
#: TWIN_LGD_DISTRICT_CODES={"hyderabad": ["..."]} once confirmed against
#: https://lgdirectory.gov.in.
CITY_LGD_DISTRICT_CODES = {
    "bengaluru": ("525", "526"),
    "hyderabad": (),
}


def _merge_learned_lgd_codes(mapping, path):
    """Fold in codes derived from the feed, without letting them override .env.

    Learned codes are inferred (see scripts/learn_lgd_codes.py) and are
    therefore additive only: an explicitly configured list always wins, so a
    bad inference can be corrected in one place and stay corrected.
    """
    if not path or not os.path.exists(path):
        return mapping
    try:
        with open(path, "r", encoding="utf-8") as handle:
            learned = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("learned LGD file %s is unreadable (%s); ignoring", path, exc)
        return mapping

    for slug, codes in (learned.get("cities") or {}).items():
        existing = tuple(str(c) for c in mapping.get(slug, ()))
        mapping[slug] = tuple(dict.fromkeys(existing + tuple(str(c) for c in codes)))
    return mapping


#: Where scripts/learn_lgd_codes.py writes what it infers.
LGD_LEARNED_FILE = os.getenv(
    "TWIN_LGD_LEARNED_FILE", os.path.join("data", "twin", "lgd_districts.json"))

CITY_LGD_DISTRICT_CODES = _merge_learned_lgd_codes(
    CITY_LGD_DISTRICT_CODES, LGD_LEARNED_FILE)
CITY_LGD_DISTRICT_CODES.update(_json_env("TWIN_LGD_DISTRICT_CODES", {}))

#: Alerts per feed whose full CAP document (and polygon) is fetched per poll.
#: Each one is a separate HTTP request, so this bounds a poll at roughly
#: 2 x (1 + 2 x SACHET_MAX_ALERTS) requests worst case.
SACHET_MAX_ALERTS = _int_env("TWIN_SACHET_MAX_ALERTS", 25)

#: CAP 1.2 XML namespace. The alert document is namespaced; the *polygon*
#: document served from the same host is not. Matching a bare <alert> against
#: the former finds nothing and fails silently.
CAP_NAMESPACE = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}

#: cap:severity -> the priority vocabulary scoring.py already consumes.
CAP_SEVERITY_PRIORITY = {
    "extreme": "critical",
    "severe": "high",
    "moderate": "medium",
    "minor": "low",
    "unknown": "low",
}

#: cap:certainty -> confidence multiplier. An "Observed" alert is a statement
#: that the thing is happening; "Possible" is a statement that it might.
CAP_CERTAINTY_CONFIDENCE = {
    "observed": 1.0,
    "likely": 0.75,
    "possible": 0.5,
    "unlikely": 0.25,
    "unknown": 0.5,
}

# Horizons computed and stored per cell (section 5.3).
HORIZONS = (0, 3, 6, 24)

# A pane is badged stale when its newest computed_at is older than this many
# multiples of the compute interval (section 15, "Stale compute mistaken for
# live data").
STALE_COMPUTE_MULTIPLIER = 3

# --------------------------------------------------------------------------
# Cities (section 2.1)
# --------------------------------------------------------------------------

CITY_DEFS = {
    "hyderabad": {
        "slug": "hyderabad",
        "display_name": "Hyderabad",
        "state": "Telangana",
        "country": "India",
        "center_latitude": 17.3850,
        "center_longitude": 78.4867,
        # (min_lon, min_lat, max_lon, max_lat) -- a CAMERA/QUERY extent.
        # Never generate the H3 grid from this; clip to the admin_level=8
        # polygon instead (section 3 warning).
        "bbox": (78.24, 17.22, 78.66, 17.60),
        "default_zoom": 10.2,
        "default_pitch": 55.0,
        "default_bearing": -12.5,
        "zone_scheme": "GHMC-6",
        "admin_body": "GHMC",
        # OSM relation admin_level that yields the city clip polygon.
        "clip_admin_level": 8,
        # Probe order for the zone split; the first level that returns
        # plausible polygons wins.
        "zone_admin_levels": (9, 10),
        # GloFAS sampling point for the basin outlet (Musi).
        "basin_outlet": (17.3200, 78.6300),
        "basin_name": "Musi",
    },
    "bengaluru": {
        "slug": "bengaluru",
        "display_name": "Bengaluru",
        "state": "Karnataka",
        "country": "India",
        "center_latitude": 12.9716,
        "center_longitude": 77.5946,
        "bbox": (77.44, 12.83, 77.78, 13.14),
        "default_zoom": 10.2,
        "default_pitch": 55.0,
        "default_bearing": -12.5,
        "zone_scheme": "BBMP-8",
        "admin_body": "BBMP / Greater Bengaluru Authority",
        "clip_admin_level": 8,
        "zone_admin_levels": (9, 10),
        # GloFAS sampling point for the basin outlet (Vrishabhavathi/Arkavathi).
        "basin_outlet": (12.8400, 77.4700),
        "basin_name": "Vrishabhavathi",
    },
}

CITY_ORDER = ("hyderabad", "bengaluru")

# Synthetic "Whole City" zone slug; the default selection in both dropdowns.
ALL_ZONES = "__all__"

# --------------------------------------------------------------------------
# Zones (section 2.2)
#
# center is a seed point used only to build an approximate boundary when
# Overpass yields no polygon for the zone. boundary_source is then set to
# "approximate" and the UI badges it honestly.
# --------------------------------------------------------------------------

ZONE_DEFS = {
    "hyderabad": [
        {"slug": "charminar", "display_name": "Charminar", "center": (17.3616, 78.4747)},
        {"slug": "khairatabad", "display_name": "Khairatabad", "center": (17.4126, 78.4610)},
        {"slug": "serilingampally", "display_name": "Serilingampally", "center": (17.4820, 78.3480)},
        {"slug": "kukatpally", "display_name": "Kukatpally", "center": (17.4948, 78.3996)},
        {"slug": "secunderabad", "display_name": "Secunderabad", "center": (17.4399, 78.4983)},
        {"slug": "lb-nagar", "display_name": "L.B. Nagar", "center": (17.3457, 78.5522)},
    ],
    "bengaluru": [
        {"slug": "east", "display_name": "East", "center": (12.9850, 77.6200)},
        {"slug": "west", "display_name": "West", "center": (12.9800, 77.5400)},
        {"slug": "south", "display_name": "South", "center": (12.9200, 77.5700)},
        {"slug": "bommanahalli", "display_name": "Bommanahalli", "center": (12.8900, 77.6200)},
        {"slug": "mahadevapura", "display_name": "Mahadevapura", "center": (12.9900, 77.6900)},
        {"slug": "rr-nagar", "display_name": "Rajarajeshwari Nagar", "center": (12.9200, 77.5100)},
        {"slug": "dasarahalli", "display_name": "Dasarahalli", "center": (13.0400, 77.5100)},
        {"slug": "yelahanka", "display_name": "Yelahanka", "center": (13.1000, 77.5900)},
    ],
}

# --------------------------------------------------------------------------
# Scoring weights (section 5.1)
#
# Hazard is what is HAPPENING; vulnerability is what is AT STAKE. They multiply.
# A flat weighted sum floors every low-lying, hospital-dense cell in a permanent
# `watch` band with zero rain and zero incidents.
# --------------------------------------------------------------------------

HAZARD_WEIGHTS = {
    "hydro": 0.55,
    "incident": 0.30,
    "env": 0.15,
    # Added with the live-data phase. Weights are renormalised across whatever
    # terms are actually present (twin/scoring.py::_weighted_renormalize), so a
    # city with no transit feed and no alerts in force scores exactly as it did
    # before these existed -- the extension cannot silently re-tune a quiet day.
    #
    # `alert` outranks `incident` deliberately: an IMD warning is an
    # authority's statement about what is coming, while an incident report is
    # an observation of what already happened, and a warning system that
    # weights the past above the forecast has missed its own point.
    "alert": 0.35,
    # Transit disruption is corroboration, not evidence on its own: buses stop
    # for protests, VIP movement and roadworks too, so it nudges rather than
    # drives.
    "disruption": 0.12,
}

#: cap:severity -> a 0..100 level for anomaly.alert_pressure.
CAP_SEVERITY_LEVEL = {
    "extreme": 100.0,
    "severe": 78.0,
    "moderate": 50.0,
    "minor": 25.0,
    "unknown": 30.0,
}

# vulnerability = 1 + VULNERABILITY_GAIN * (blend of terrain and infra) / 100
VULNERABILITY_WEIGHTS = {
    "terrain": 0.60,
    "infra": 0.40,
}
VULNERABILITY_GAIN = 0.60      # multiplier spans 1.0 .. 1.6

# Sub-score internals.
HYDRO_WEIGHTS = {
    # horizon_hours -> (rain_now, rain_forecast, discharge)
    0: (0.40, 0.40, 0.20),
    3: (0.40, 0.40, 0.20),
    6: (0.40, 0.40, 0.20),
    24: (0.25, 0.35, 0.40),
}
TERRAIN_WEIGHTS = {"low_lying": 0.45, "water_prox": 0.35, "drain_gap": 0.20}
ENV_WEIGHTS = {"aqi": 0.60, "heat": 0.40}

INCIDENT_SEVERITY = {"low": 25.0, "medium": 50.0, "high": 75.0, "critical": 100.0}
INCIDENT_DECAY_TAU_HOURS = 12.0     # time constant, not half-life (half-life = 8.3 h)
INCIDENT_NEIGHBOUR_WEIGHT = 0.40    # k-ring-1 spillover
INCIDENT_DEFAULT_CONFIDENCE = 0.50  # used only when confidence_score IS NULL

# Neutral fallbacks, applied when a source is dead but a neutral value is more
# honest than dropping the term (section 4.1 "Fallback" column).
NEUTRAL_AQI = 50.0

# --------------------------------------------------------------------------
# Status bands (section 5.2)
# --------------------------------------------------------------------------

STATUS_BANDS = (
    # (inclusive_min, exclusive_max, status, colour, opacity, height_factor)
    (0.0, 25.0, "normal", "#22c55e", 0.35, 4.0),
    (25.0, 50.0, "watch", "#eab308", 0.50, 6.0),
    (50.0, 75.0, "warning", "#f97316", 0.65, 9.0),
    (75.0, 100.01, "critical", "#ef4444", 0.80, 14.0),
)

STATUS_ORDER = ("normal", "watch", "warning", "critical")


def band_for(risk_score):
    """Return the (status, colour, opacity, height_factor) for a risk score."""
    value = max(0.0, min(100.0, float(risk_score or 0.0)))
    for low, high, status, colour, opacity, height in STATUS_BANDS:
        if low <= value < high:
            return status, colour, opacity, height
    return STATUS_BANDS[-1][2:]


# --------------------------------------------------------------------------
# Infrastructure criticality (section 4.3)
# --------------------------------------------------------------------------

ASSET_CRITICALITY = {
    "hospital": 1.0,
    "fire_station": 1.0,
    "police": 0.8,
    "power_substation": 0.9,
    "water_works": 0.8,
    "school": 0.6,
    "transport_hub": 0.7,
    "shelter": 0.5,
}

# Terrain inputs, not criticality contributors.
TERRAIN_ASSET_TYPES = ("water_body", "drain")

# Overpass filters per asset type. Kept next to the criticality table so the
# two never drift apart.
OVERPASS_FILTERS = {
    "hospital": ['node["amenity"="hospital"]', 'way["amenity"="hospital"]'],
    "fire_station": ['node["amenity"="fire_station"]', 'way["amenity"="fire_station"]'],
    "police": ['node["amenity"="police"]', 'way["amenity"="police"]'],
    "power_substation": ['node["power"="substation"]', 'way["power"="substation"]'],
    "water_works": [
        'node["man_made"="water_works"]', 'way["man_made"="water_works"]',
        'node["man_made"="water_tower"]', 'way["man_made"="water_tower"]',
    ],
    "school": [
        'node["amenity"="school"]', 'way["amenity"="school"]',
        'node["amenity"="college"]', 'way["amenity"="college"]',
    ],
    "transport_hub": [
        'node["railway"="station"]', 'node["amenity"="bus_station"]',
        'way["aeroway"="aerodrome"]',
    ],
    "shelter": [
        'node["amenity"="shelter"]', 'node["amenity"="community_centre"]',
        'way["amenity"="community_centre"]',
    ],
    "water_body": ['way["natural"="water"]', 'relation["natural"="water"]'],
    "drain": [
        'way["waterway"="drain"]', 'way["waterway"="canal"]', 'way["waterway"="stream"]',
    ],
}

INFRA_CRITICALITY_GAIN = 12.0   # infra = min(100, sum(criticality) * 12)
DRAIN_SATURATION_M = 2000.0     # 2000 m of drain in a cell => drain_gap 0

# --------------------------------------------------------------------------
# Ingest sources, for the health pill (section 8.7) and TwinDataSnapshot keys.
# --------------------------------------------------------------------------

TIER1_SOURCES = (
    "open_meteo_forecast",
    "open_meteo_airquality",
    "open_meteo_flood",
    "rainviewer",
    "overpass",
    "internal_reports",
    # Keyless, official, and the only source that speaks with authority rather
    # than measurement -- if it is down, an operator must be told.
    "sachet_cap",
)

OPTIONAL_SOURCES = ("tomtom_traffic", "tomorrow_io", "tgdps", "ksndmc",
                    "streetview", "cctv_osint", "overpass_water",
                    # Live point sources (twin/ingest/stations.py, transit.py).
                    # Each is key-gated or URL-gated: absent credentials mean a
                    # hidden layer, never an error (C5).
                    "cpcb_aqi", "openaq", "aqicn", "iudx",
                    "gtfs_static", "gtfs_realtime",
                    "gdacs", "usgs_quakes", "open_meteo_archive",
                    "cctv_live")

ALL_SOURCES = TIER1_SOURCES + OPTIONAL_SOURCES

# --------------------------------------------------------------------------
# Live point sources: pollution stations, transit vehicles, exchanges
#
# Every entry here is optional. The module's standing rule (C5) is that a
# missing key hides a layer and never breaks a page, so each adapter checks
# its own credential and reports "not configured" as a normal state rather
# than an error.
# --------------------------------------------------------------------------

STATION_POLL_MIN = _int_env("TWIN_STATION_POLL_MIN", 15)
TRANSIT_POLL_MIN = _int_env("TWIN_TRANSIT_POLL_MIN", 2)

#: data.gov.in OGD platform. The resource id is CPCB's live station-level AQI.
DATA_GOV_IN_URL = "https://api.data.gov.in/resource/%s"
CPCB_AQI_RESOURCE_ID = os.getenv(
    "TWIN_CPCB_RESOURCE_ID", "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69")
#: data.gov.in publishes this key on its own API documentation pages for
#: trying endpoints out. It is heavily rate-limited and shared by everyone who
#: ever read those docs, so it is a fallback that keeps the layer alive before
#: DATA_GOV_IN_KEY is set -- not a substitute for registering one.
DATA_GOV_IN_SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"

OPENAQ_API_KEY = os.getenv("OPENAQ_API_KEY") or None
OPENAQ_URL = "https://api.openaq.org/v3"

AQICN_TOKEN = os.getenv("AQICN_TOKEN") or None
AQICN_URL = "https://api.waqi.info"

#: IUDX (India Urban Data Exchange). The catalogue is public; fetching actual
#: resource data needs an account and, for most resources, a per-resource
#: token, so the adapter discovers what is available and only pulls open ones.
IUDX_CATALOGUE_URL = os.getenv(
    "TWIN_IUDX_CATALOGUE_URL", "https://api.catalogue.iudx.org.in/iudx/cat/v1")
IUDX_RESOURCE_URL = os.getenv(
    "TWIN_IUDX_RESOURCE_URL", "https://rs.iudx.org.in/ngsi-ld/v1")
IUDX_TOKEN = os.getenv("IUDX_TOKEN") or None

#: Transit. Both are JSON maps of city slug -> URL, e.g.
#:   TWIN_GTFS_RT_URLS={"bengaluru": "https://.../vehiclepositions.pb"}
#: kept in .env rather than in code because no Indian city publishes a stable
#: public GTFS-RT endpoint that can be hard-coded with a straight face.
GTFS_STATIC_URLS = _json_env("TWIN_GTFS_STATIC_URLS", {})
GTFS_RT_URLS = _json_env("TWIN_GTFS_RT_URLS", {})
GTFS_RT_HEADERS = _json_env("TWIN_GTFS_RT_HEADERS", {})

#: A vehicle that has not moved this long is drawn as stalled rather than
#: live -- a stalled fleet on a flooded arterial is the signal, so it must be
#: visible instead of quietly ageing out.
TRANSIT_STALE_MIN = _int_env("TWIN_TRANSIT_STALE_MIN", 10)

#: Global event feeds. Keyless, small, and useful mostly as corroboration.
GDACS_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
USGS_QUAKE_URL = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
                  "all_day.geojson")
#: Kilometres from a city centre within which a global event is this city's
#: business. A quake 2,000 km away is real and irrelevant.
GLOBAL_EVENT_RADIUS_KM = _int_env("TWIN_GLOBAL_EVENT_RADIUS_KM", 300)

# --------------------------------------------------------------------------
# Live camera streams (twin/ingest/cctv.py renders these; nothing is proxied)
#
# OpenStreetMap maps where cameras *are*; it hosts no feeds, and no public
# live CCTV stream exists for either modelled city. So operator-supplied
# streams live in a JSON file outside the code: when an ICCC, a campus or a
# police feed becomes available, it is a config edit, not a deploy.
#
# The server never fetches, re-hosts or proxies any of these URLs -- the
# browser plays them directly, exactly as it would any other embedded player.
# --------------------------------------------------------------------------

CCTV_STREAMS_FILE = os.getenv(
    "TWIN_CCTV_STREAMS_FILE", os.path.join("data", "twin", "cctv_streams.json"))

# --------------------------------------------------------------------------
# Live camera PROVIDERS (twin/ingest/cctv_live.py)
#
# The stream file above is one operator editing one file. A provider is the
# same thing at the scale of a road authority: a keyless public catalog that
# publishes where its cameras are and where each one's current frame lives.
#
# Every provider is coverage-gated -- it is only fetched when its published
# service area actually intersects the bbox being asked about. That is what
# keeps this honest for Hyderabad and Bengaluru: no provider covers them, so
# none is called and none is drawn, instead of a foreign city's cameras
# standing in for a local one. When an Indian authority publishes a catalog,
# it becomes one entry in the registry and the two cities light up for real.
# --------------------------------------------------------------------------

#: Master switch. "0" disables every provider and leaves only CCTV_STREAMS_FILE.
CCTV_LIVE_ENABLED = os.getenv("TWIN_CCTV_LIVE_ENABLED", "1").strip() != "0"

#: Comma-separated provider keys to allow, or "" for "every registered one".
#: Narrowing this is how a deployment stops calling catalogs it will never
#: display -- the coverage gate already does that geographically.
CCTV_LIVE_PROVIDERS = os.getenv("TWIN_CCTV_LIVE_PROVIDERS", "").strip()

#: Camera *catalogs* change when an authority installs a pole, not by the
#: minute. Frames are fetched by the browser per refresh and are unaffected.
CCTV_LIVE_CACHE_TTL_S = _int_env("TWIN_CCTV_LIVE_CACHE_TTL_S", 15 * 60)

#: Ceiling on cameras returned from one lookup, filled round-robin across the
#: providers that matched, so a dense catalog never evicts a sparse one.
CCTV_LIVE_MAX_CAMERAS = _int_env("TWIN_CCTV_LIVE_MAX_CAMERAS", 1200)

#: Per-provider catalog fetch timeout. One stalled authority must not hold the
#: console's camera panel open.
CCTV_LIVE_TIMEOUT_S = _int_env("TWIN_CCTV_LIVE_TIMEOUT_S", 15)

# --------------------------------------------------------------------------
# Reference feed
#
# The coverage gate above is correct and stays exactly as it is: a camera in
# another city is never presented as this city's ground. But its consequence
# for this deployment is a panel that is empty in *both* modelled cities and
# therefore can never be seen working, which makes a real layer look like a
# broken one and gives an operator no way to tell a dead panel from an honest
# one.
#
# A reference feed is the narrow answer to that, and it is a *display* device
# with no analytical standing:
#
# * It engages only when no authority covers the city -- never alongside
#   local cameras, so it cannot dilute or outrank real local ground.
# * Every stream it returns is flagged ``reference: True``, is labelled with
#   the city and authority it actually comes from, and is rendered under a
#   banner saying it is not local.
# * It is kept off the map. A pin on the city map means "a camera is here",
#   and no reference camera is. It appears in the drawer list only.
# * It never reaches scoring. ``live_streams`` feeds the console panel and
#   nothing else -- no risk term, no flag, no brief reads it.
# --------------------------------------------------------------------------

#: "0" restores the strictly-empty panel: no reference feed is ever offered.
CCTV_REFERENCE_ENABLED = os.getenv("TWIN_CCTV_REFERENCE_ENABLED", "1").strip() != "0"

#: Which registered provider stands in. Hong Kong by default: it is the
#: densest keyless catalog in the registry (1,013 cameras), it publishes
#: plain JPEG stills that the console's existing image player refreshes
#: without a video stack, and it is the nearest such authority to the
#: modelled cities -- a monsoon-season Asian city rather than a North
#: American freeway, so what an operator sees resembles the ground they work.
CCTV_REFERENCE_PROVIDER = os.getenv("TWIN_CCTV_REFERENCE_PROVIDER", "hongkong").strip()

#: How many reference cameras to return. Small on purpose: this is a
#: demonstration that the layer works, not a second city to monitor.
CCTV_REFERENCE_MAX = _int_env("TWIN_CCTV_REFERENCE_MAX", 24)

# --------------------------------------------------------------------------
# Anomaly detection and baselines (twin/anomaly.py, scripts/backfill_baselines.py)
# --------------------------------------------------------------------------

#: Open-Meteo's historical reanalysis archive. Keyless, and the reason the
#: twin can judge "abnormal" on day one instead of after a year of polling.
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
BASELINE_YEARS = _int_env("TWIN_BASELINE_YEARS", 5)
#: Metrics baselined per cell per calendar month.
BASELINE_METRICS = ("rain_1h", "rain_3h", "rain_24h", "temp_max", "aqi")

#: Sigma above a cell's own monthly mean before "unusual" is claimed. 2.0 is
#: roughly the 97.7th percentile of a normal distribution; rainfall is not
#: normally distributed, so the percentile columns are preferred where a
#: baseline has them and sigma is the fallback.
ANOMALY_SIGMA_WATCH = float(os.getenv("TWIN_ANOMALY_SIGMA_WATCH", "1.5"))
ANOMALY_SIGMA_ALERT = float(os.getenv("TWIN_ANOMALY_SIGMA_ALERT", "2.5"))

# --------------------------------------------------------------------------
# The agent (twin/agent/). Everything degrades to a deterministic path.
# --------------------------------------------------------------------------

AGENT_ENABLED = os.getenv("TWIN_AGENT_ENABLED", "1") == "1"
AGENT_INTERVAL_MIN = _int_env("TWIN_AGENT_INTERVAL_MIN", 10)

#: OpenAI only. gpt-5-nano is OpenAI's cheapest text model ($0.05 in / $0.40
#: out per 1M tokens, Sep 2026). It is a reasoning model, so it ignores
#: temperature and bills its hidden reasoning as output -- "minimal" effort
#: keeps that near zero. With no key the agent still runs: extraction falls
#: back to rules and briefs to templates (see agent/llm.py).
LLM_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("TWIN_LLM_API_KEY") or None
LLM_MODEL = os.getenv("TWIN_LLM_MODEL", "gpt-5-nano")
LLM_REASONING_EFFORT = os.getenv("TWIN_LLM_REASONING_EFFORT", "minimal")
LLM_MAX_TOKENS = _int_env("TWIN_LLM_MAX_TOKENS", 1500)
LLM_TIMEOUT_S = float(os.getenv("TWIN_LLM_TIMEOUT_S", "45"))

#: Risk score at or above which a cluster becomes a flag for an analyst.
FLAG_THRESHOLD = float(os.getenv("TWIN_FLAG_THRESHOLD", "55"))
#: A flag nobody acted on stops being current after this long.
FLAG_TTL_HOURS = _int_env("TWIN_FLAG_TTL_HOURS", 12)

# --------------------------------------------------------------------------
# Forecast agent (twin/forecast.py, twin/agent/forecast_graph.py)
#
# The triage agent reads what is already true. This one projects where a rain
# field will be carried by the wind, and when apparent temperature crosses an
# advisory threshold, so an analyst can warn people before it lands rather
# than after. It writes to the same twin_flag table behind the same pending
# gate, so nothing it produces reaches the public without a human click.
# --------------------------------------------------------------------------

#: "0" leaves only the triage agent running.
FORECAST_ENABLED = os.getenv("TWIN_FORECAST_ENABLED", "1").strip() != "0"


#: RAG. Local by default: no key, no per-query cost, and it works offline.
#: Chroma has no Python 3.14 wheel, hence FAISS.
RAG_DIR = os.getenv("TWIN_RAG_DIR", os.path.join("data", "twin", "rag"))
RAG_CORPUS_DIR = os.getenv("TWIN_RAG_CORPUS_DIR", os.path.join("data", "twin", "corpus"))
RAG_EMBEDDING_MODEL = os.getenv("TWIN_RAG_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
RAG_TOP_K = _int_env("TWIN_RAG_TOP_K", 4)

# --------------------------------------------------------------------------
# Public dispatch (twin/dispatch.py) -- the one path that reaches real people
# --------------------------------------------------------------------------

#: Minutes before the same flag may be dispatched again without `force`.
#: Alert fatigue is the failure mode that destroys a warning system's value.
DISPATCH_COOLDOWN_MIN = _int_env("TWIN_DISPATCH_COOLDOWN_MIN", 30)
#: Hard ceiling an analyst cannot exceed from the UI.
DISPATCH_MAX_RADIUS_KM = float(os.getenv("TWIN_DISPATCH_MAX_RADIUS_KM", "25"))
#: Extra kilometres around a flag's cells, so people just outside the hexagons
#: are still warned. An H3 res-8 cell is ~460 m across.
DISPATCH_BUFFER_KM = float(os.getenv("TWIN_DISPATCH_BUFFER_KM", "1.5"))
