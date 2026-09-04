"""Static configuration for the Sentinel digital twin.

Everything here is data, not behaviour. Weights and bands live in one place so
they can be tuned, logged, and later back-tested against TwinCellHistory
(see DIGITAL_TWIN_README.md section 15).
"""

import os

# --------------------------------------------------------------------------
# Runtime knobs (section 11). All optional; these are the defaults.
# --------------------------------------------------------------------------

def _int_env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


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
)

OPTIONAL_SOURCES = ("tomtom_traffic", "tomorrow_io", "tgdps", "ksndmc")

ALL_SOURCES = TIER1_SOURCES + OPTIONAL_SOURCES
