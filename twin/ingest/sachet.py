"""NDMA SACHET -- India's official CAP 1.2 disaster alert feed (keyless).

This is the twin's only *authoritative* live source: every other feed is a
measurement (rain fell, a sensor read 180 ug/m3), whereas a SACHET item is a
statement by IMD, a state disaster management authority or NDMA that
something is expected or observed. That difference is preserved all the way
to the UI -- see ``twin/models.py::TwinExternalAlert``.

Three documents, three different shapes, and mixing them up is the classic
way to lose a day:

1. **The RSS feed** (``rss_telangana.xml``) -- a plain RSS 2.0 list. The
   ``<link>`` of each item points at the CAP document; ``<guid>`` is stable
   across polls and is what makes re-polling idempotent.
2. **The CAP alert document** -- namespaced XML
   (``urn:oasis:names:tc:emergency:cap:1.2``). Matching a bare ``<alert>``
   against it finds nothing and fails *silently*, which looks exactly like an
   empty feed.
3. **The polygon document** -- served by the same host, at a URL found in a
   ``cap:parameter`` rather than inline, and **not namespaced**. Its contents
   are space-separated ``lat,lon`` pairs.

The last point is the expensive one: **CAP polygons are lat,lon and GeoJSON is
lon,lat.** Swapping them puts a Bengaluru thunderstorm warning in the Indian
Ocean, silently, with no error anywhere.

Nothing in this module writes to the database -- ``twin/alerts.py`` owns
persistence, so every parser here stays a pure function of one XML string and
can be pinned by a unit test with no network and no session.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .. import config
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.sachet")

ATTRIBUTION = "NDMA SACHET / CAP (Government of India)"

#: The polygon document is fetched once per alert and is immutable for a given
#: identifier, so it is cached far longer than the feed that points at it.
POLYGON_CACHE = {}


class SachetCapAdapter(IngestAdapter):
    """Official alerts for one state, fully resolved (CAP + polygon).

    ``fetch_raw`` returns a dict rather than a bare list so an empty result
    ("no active alerts in Telangana right now" -- the *normal* case) keeps its
    attribution and stays distinguishable from a failed fetch, which ``run()``
    reports through the snapshot status instead.
    """

    source_key = "sachet_cap"
    #: Alerts are time-critical and the feed is small; 5 minutes matches the
    #: poll cadence in jobs.py so a poll either serves cache or goes to network
    #: exactly once.
    cache_ttl_s = 300
    max_retries = 1

    def fetch_raw(self, state=None, timeout_s=None, max_alerts=None, **_):
        if not state:
            raise ValueError("sachet fetch needs a state name")
        timeout_s = timeout_s or self.timeout_s
        max_alerts = max_alerts or config.SACHET_MAX_ALERTS

        feed_url = config.SACHET_RSS_URL % state.strip().lower()
        response = self.session.get(feed_url, timeout=timeout_s)
        response.raise_for_status()
        items = parse_rss(response.text)

        alerts = []
        for item in items[:max_alerts]:
            try:
                alert = self._resolve_item(item, timeout_s)
            except Exception as exc:  # noqa: BLE001
                # One malformed or slow CAP document must not cost the other
                # nineteen alerts in the feed. The RSS item itself already
                # carries enough to be useful, so it is kept, unresolved.
                log.warning("sachet: CAP resolve failed for %s (%s)", item.get("guid"), exc)
                alert = dict(item, resolved=False)
            alerts.append(alert)

        return {
            "state": state,
            "feed_url": feed_url,
            "alerts": alerts,
            "source": "NDMA SACHET",
            "attribution": ATTRIBUTION,
        }

    def _resolve_item(self, item, timeout_s):
        cap_url = item.get("link")
        if not cap_url:
            return dict(item, resolved=False)

        response = self.session.get(cap_url, timeout=timeout_s)
        response.raise_for_status()
        alert = parse_cap(response.text)
        alert.update({
            "source_uid": item.get("guid") or alert.get("identifier"),
            "raw_url": cap_url,
            "rss_title": item.get("title"),
            "resolved": True,
        })

        polygon = alert.get("polygon_points")
        if not polygon and alert.get("polygon_url"):
            try:
                polygon = self._fetch_polygon(alert["polygon_url"], timeout_s)
                alert["polygon_points"] = polygon
            except Exception as exc:  # noqa: BLE001
                # The polygon endpoint is a separate document behind the same
                # host, and it is currently WAF-blocked: every
                # FetchPolygonXMLFile request returns 403 while FetchXMLFile
                # returns 200. Letting that failure escape would discard a
                # perfectly good CAP alert -- severity, timing, instructions
                # and all -- over a missing shape. The alert survives as
                # district-scoped instead, which is what it always was.
                log.info("sachet: polygon unavailable for %s (%s)",
                         alert.get("identifier"), exc)
                alert["polygon_error"] = str(exc)

        alert["geometry"] = cap_polygon_to_geojson(polygon)
        alert["geometry_kind"] = "polygon" if alert["geometry"] else (
            "district" if alert.get("area_desc") or alert.get("geocodes") else None)
        return alert

    def _fetch_polygon(self, url, timeout_s):
        """One polygon document, memoised for the life of the process.

        A CAP polygon for a given identifier never changes -- an amended
        footprint is published as a new alert with a new identifier -- so the
        in-process memo is safe and removes a second HTTP round-trip per alert
        on every poll of a feed whose items mostly repeat.
        """
        if url in POLYGON_CACHE:
            return POLYGON_CACHE[url]
        response = self.session.get(url, timeout=timeout_s)
        response.raise_for_status()
        points = parse_polygon_document(response.text)
        if len(POLYGON_CACHE) > 512:
            POLYGON_CACHE.clear()
        POLYGON_CACHE[url] = points
        return points

    def neutral_value(self, state=None, **_):
        return {"state": state, "alerts": [], "source": "NDMA SACHET",
                "attribution": ATTRIBUTION, "unavailable": True}

    def record_count(self, data):
        return len((data or {}).get("alerts") or [])

    def _cache_key(self, city_id, kwargs):
        """Key on the state alone.

        The base implementation hashes every kwarg including the
        Flask-SQLAlchemy handle, whose repr embeds a memory address -- so the
        key would change on every restart and the TTL would never survive one.
        Same reasoning as CctvOsintAdapter._cache_key.
        """
        import hashlib

        raw = "sachet:%s" % (kwargs.get("state") or "").strip().lower()
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Parsers -- pure functions of one XML string
# --------------------------------------------------------------------------

def parse_rss(xml_text):
    """RSS 2.0 items -> [{guid, title, link, category, author, pub_date}].

    Returns [] for an empty or unparseable feed rather than raising: an empty
    channel is the ordinary state of a state-level disaster feed, and the
    caller cannot tell the difference from the parse alone anyway.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("sachet: unparseable RSS (%s)", exc)
        return []

    items = []
    for node in root.iter("item"):
        items.append({
            "guid": _text(node, "guid"),
            "title": _text(node, "title"),
            "link": _text(node, "link"),
            "category": _text(node, "category"),
            "author": _text(node, "author"),
            "pub_date": _text(node, "pubDate"),
        })
    return items


def parse_cap(xml_text):
    """A CAP 1.2 alert document -> a flat dict.

    Only the first ``cap:info`` block is read. Indian CAP alerts publish one
    info block per language (English, then the state language) carrying the
    same event; taking them all would double every alert.
    """
    root = ET.fromstring(xml_text)
    ns = config.CAP_NAMESPACE

    info = root.find("cap:info", ns)
    if info is None:
        info = ET.Element("empty")

    area = info.find("cap:area", ns)
    if area is None:
        area = ET.Element("empty")

    parameters = {}
    for parameter in info.findall("cap:parameter", ns):
        name = _text(parameter, "cap:valueName", ns)
        value = _text(parameter, "cap:value", ns)
        if name:
            parameters[name.strip()] = value

    # Multi-valued by necessity: one IMD warning routinely names 23 districts,
    # each as its own cap:geocode. Collapsing these to a single value -- the
    # obvious dict assignment -- keeps only the last district and silently
    # drops the one the twin is actually looking for.
    geocodes = {}
    for geocode in area.findall("cap:geocode", ns):
        name = _text(geocode, "cap:valueName", ns)
        value = _text(geocode, "cap:value", ns)
        if name and value:
            geocodes.setdefault(name.strip(), []).append(value)

    # A polygon may be inline (cap:area/cap:polygon) or, as SACHET does it,
    # behind a "Polygon URL" parameter. Inline wins: it needs no extra fetch.
    inline_polygon = _text(area, "cap:polygon", ns)

    return {
        "identifier": _text(root, "cap:identifier", ns),
        "sender": _text(root, "cap:sender", ns),
        "sent": _text(root, "cap:sent", ns),
        "status": _text(root, "cap:status", ns),
        "msg_type": _text(root, "cap:msgType", ns),
        "references": _text(root, "cap:references", ns),
        "category": _text(info, "cap:category", ns),
        "event": _text(info, "cap:event", ns),
        "urgency": _text(info, "cap:urgency", ns),
        "severity": _text(info, "cap:severity", ns),
        "certainty": _text(info, "cap:certainty", ns),
        "effective": _text(info, "cap:effective", ns),
        "onset": _text(info, "cap:onset", ns),
        "expires": _text(info, "cap:expires", ns),
        "headline": _text(info, "cap:headline", ns),
        "description": _text(info, "cap:description", ns),
        "instruction": _text(info, "cap:instruction", ns),
        "sender_name": _text(info, "cap:senderName", ns),
        "area_desc": _text(area, "cap:areaDesc", ns),
        "geocodes": geocodes,
        "parameters": parameters,
        "polygon_url": parameters.get("Polygon URL") or parameters.get("PolygonURL"),
        "polygon_points": parse_polygon_text(inline_polygon) if inline_polygon else None,
    }


def parse_polygon_document(xml_text):
    """The (un-namespaced) polygon document -> [(lat, lon), ...].

    Note the missing namespace: this document is ``<alert><polygon>``, plain,
    while the alert document it belongs to is ``<cap:alert>``. Registering the
    CAP namespace here would match nothing.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("sachet: unparseable polygon document (%s)", exc)
        return None

    texts = [node.text for node in root.iter() if _local_name(node.tag) == "polygon"]
    points = []
    for text in texts:
        points.extend(parse_polygon_text(text) or [])
    return points or None


def parse_polygon_text(text):
    """``"12.86,77.83 12.85,77.83 ..."`` -> [(lat, lon), ...] or None.

    Pairs that are not two finite numbers are skipped rather than failing the
    whole polygon; a ring with fewer than three distinct points is rejected
    outright, because a degenerate polygon fills zero H3 cells and would read
    on the map as "the alert covers nothing".
    """
    if not text:
        return None

    points = []
    for chunk in str(text).replace("\n", " ").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        points.append((lat, lon))

    return points if len(points) >= 3 else None


def cap_polygon_to_geojson(points):
    """[(lat, lon), ...] -> a closed GeoJSON Polygon in (lon, lat) order.

    **This is the coordinate flip.** CAP is lat,lon; GeoJSON is lon,lat.
    Everything downstream -- shapely, H3, MapLibre -- expects GeoJSON order,
    so the swap happens here, once, rather than being re-derived at each call
    site (which is how half of one gets forgotten).
    """
    if not points or len(points) < 3:
        return None

    ring = [[round(lon, 6), round(lat, 6)] for lat, lon in points]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4:
        return None
    return {"type": "Polygon", "coordinates": [ring]}


def parse_cap_datetime(value):
    """CAP timestamps carry a real offset (``+05:30``). Never assume UTC.

    Returns a timezone-aware UTC datetime, or None. A naive value is read as
    IST rather than UTC, because every publisher in these feeds is Indian and
    treating 20:06 IST as 20:06 UTC moves an alert 5.5 hours into the future --
    long enough for an active warning to look expired.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                    "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None

    if parsed.tzinfo is None:
        from datetime import timedelta
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Mapping CAP vocabulary onto the twin's scoring vocabulary
# --------------------------------------------------------------------------

def priority_for(severity):
    """cap:severity -> the priority vocabulary twin/scoring.py consumes."""
    return config.CAP_SEVERITY_PRIORITY.get(
        (severity or "").strip().lower(), "low")


def confidence_for(certainty):
    """cap:certainty -> a 0..1 confidence multiplier."""
    return config.CAP_CERTAINTY_CONFIDENCE.get(
        (certainty or "").strip().lower(), config.CAP_CERTAINTY_CONFIDENCE["unknown"])


def hazard_type_for(alert):
    """A coarse hazard family, from the CAP event text and category.

    Deliberately keyword-driven rather than model-driven: this feeds a
    deterministic score and an operator-facing label, and a misfiring
    classifier here would be invisible and unaccountable. Unknown events fall
    through to "other" and keep their original event text on the card.
    """
    text = ("%s %s" % (alert.get("event") or "", alert.get("headline") or "")).lower()
    category = (alert.get("category") or "").strip().lower()

    if any(word in text for word in ("flood", "inundat", "waterlog", "deluge")):
        return "flood"
    if any(word in text for word in ("rain", "thunderstorm", "squall", "cyclone",
                                     "depression", "storm", "hail")):
        return "rain"
    if any(word in text for word in ("heat", "warm night", "hot weather")):
        return "heat"
    if any(word in text for word in ("earthquake", "seismic", "tremor")):
        return "earthquake"
    if any(word in text for word in ("fire", "wildfire")):
        return "fire"
    if any(word in text for word in ("air quality", "pollution", "smog")):
        return "air_quality"
    if category == "geo":
        return "earthquake"
    if category == "met":
        return "rain"
    return "other"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _text(node, path, ns=None):
    found = node.find(path, ns) if ns else node.find(path)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
