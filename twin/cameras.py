"""Operator-supplied live camera streams, and the optics assumptions.

Two honest facts drive this module:

1. **No public live CCTV stream exists for Hyderabad or Bengaluru.**
   OpenStreetMap maps where cameras are, not what they see. Traffic-police
   portals publish web pages, not APIs. So the twin cannot manufacture live
   street video, and pretending otherwise would be the single most misleading
   thing it could do.

2. **Some deployments will have real feeds.** A city command centre, a campus,
   a private operator, a partnership. When that happens it must be a config
   edit, not a code change and not a deploy.

Hence a JSON file outside the code (``TWIN_CCTV_STREAMS_FILE``)::

    [
      {
        "id": "iccc-charminar-01",
        "name": "Charminar junction (east)",
        "city": "hyderabad",
        "lat": 17.3616, "lon": 78.4747,
        "direction": 90,
        "url": "https://example.gov.in/streams/charminar.m3u8",
        "type": "hls",
        "operator": "GHMC ICCC",
        "attribution": "GHMC Integrated Command and Control Centre"
      }
    ]

**The server never fetches, re-hosts or proxies any of these URLs.** The
browser plays them directly, exactly as it plays any other embedded media.
That is both the licensing-safe path and the fast one, and it means the twin
never becomes an access route to a camera network.
"""

import json
import logging
import os

from . import config

log = logging.getLogger("twin.cameras")

#: Players the console knows how to render. Anything else is rejected at load
#: rather than passed to the browser -- an unvalidated "type" would end up in
#: the DOM, and this file is exactly the kind of thing that gets edited in a
#: hurry during an incident.
STREAM_TYPES = ("hls", "mjpeg", "image", "iframe", "youtube")

#: Field of view and useful range by OSM ``camera:type``. OSM almost never
#: tags either, so these are **declared assumptions**, surfaced through the
#: API so the legend can say so rather than implying a survey.
CAMERA_OPTICS = {
    "fixed": {"fov": 60, "range_m": 45},
    "panning": {"fov": 180, "range_m": 60},   # a PTZ sweeps: show the envelope
    "dome": {"fov": 360, "range_m": 30},
    "default": {"fov": 60, "range_m": 45},
}


def configured_streams(city_slug=None, path=None):
    """Load and validate the operator's stream list. Never raises.

    A missing file is the normal case and returns []. A malformed file is
    logged and also returns [] -- during an incident, a stray comma in a
    config file must cost one panel, not the console.
    """
    path = path or config.CCTV_STREAMS_FILE
    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("cctv stream file %s is unreadable (%s)", path, exc)
        return []

    entries = raw.get("streams") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        log.warning("cctv stream file %s is not a list of streams", path)
        return []

    streams = []
    for entry in entries:
        stream = _validate(entry)
        if stream is None:
            continue
        if city_slug and stream.get("city") and stream["city"] != city_slug:
            continue
        streams.append(stream)
    return streams


def _validate(entry):
    if not isinstance(entry, dict):
        return None

    url = (entry.get("url") or "").strip()
    # http(s) only: a file:// or javascript: URL in this file would be handed
    # straight to the browser, and this is a hand-edited operational file.
    if not url.lower().startswith(("http://", "https://")):
        return None

    stream_type = (entry.get("type") or "hls").strip().lower()
    if stream_type not in STREAM_TYPES:
        log.warning("cctv stream %r has unsupported type %r", entry.get("id"), stream_type)
        return None

    try:
        lat = float(entry["lat"]) if entry.get("lat") is not None else None
        lon = float(entry["lon"]) if entry.get("lon") is not None else None
    except (TypeError, ValueError):
        lat = lon = None

    return {
        "id": str(entry.get("id") or url),
        "name": entry.get("name") or "Camera",
        "city": (entry.get("city") or "").strip().lower() or None,
        "lat": lat,
        "lon": lon,
        "direction": _direction(entry.get("direction")),
        "url": url,
        "type": stream_type,
        "operator": entry.get("operator"),
        "attribution": entry.get("attribution") or entry.get("operator"),
        "live": True,
    }


def _direction(value):
    if value is None:
        return None
    try:
        return float(value) % 360.0
    except (TypeError, ValueError):
        return None


def live_streams(db, city_slug=None, bbox=None, path=None, timeout_s=None):
    """Every live camera for this ground: operator file first, then providers.

    The file is the operator's own list and always wins -- a deployment that
    has hand-listed its ICCC feed must never see that entry displaced by a
    public catalog that happens to map the same pole.

    Returns ``(streams, meta)``. ``meta`` carries the provider status per
    authority so the console can say *why* a panel is empty: "no authority
    publishes cameras here" and "the authority that does is down" are
    different facts, and an operator has to be able to tell them apart.
    """
    from .ingest.cctv_live import CctvLiveAdapter, providers_for

    configured = configured_streams(city_slug=city_slug, path=path)
    meta = {
        "configured_count": len(configured),
        "config_file": config.CCTV_STREAMS_FILE,
        "providers_considered": [p.as_dict() for p in providers_for(bbox)],
        "provider_status": {},
        "provider_count": 0,
        "status": "ok",
    }

    if not config.CCTV_LIVE_ENABLED or not meta["providers_considered"]:
        # Nothing covers this ground. Offer a reference feed rather than a
        # blank panel -- but only when the operator has no feed of their own
        # here, so a real local camera is never joined by a foreign one.
        #
        # ``bbox`` being required is the other half of providers_for(None)
        # returning (): an unscoped "cameras, anywhere" request must still
        # fetch nothing. Without this the reference feed would have quietly
        # reintroduced the catalog pull that the unscoped case exists to
        # prevent, which is exactly what it did when first wired up.
        if bbox and not configured:
            reference, reference_meta = _reference_streams(db, timeout_s)
            if reference:
                meta["reference"] = reference_meta
                return reference, meta
        return configured, meta

    data, snapshot = CctvLiveAdapter().run(
        db, bbox=bbox, timeout_s=timeout_s or config.CCTV_LIVE_TIMEOUT_S)
    data = data or {}
    meta["provider_status"] = data.get("provider_status") or {}
    meta["status"] = snapshot.status if snapshot is not None else "unknown"
    meta["capped"] = bool(data.get("capped"))

    seen = {stream["id"] for stream in configured}
    merged = list(configured)
    for camera in data.get("cameras") or []:
        if camera["id"] in seen:
            continue
        seen.add(camera["id"])
        merged.append(camera)

    meta["provider_count"] = len(merged) - len(configured)
    return merged, meta


def _spread(cameras, limit):
    """``limit`` cameras sampled evenly across the catalog, in order.

    Taking the first N instead would be a worse demonstration than it looks:
    TD publishes its catalog alphabetically, so the first 24 rows are 24
    cameras on Aberdeen Praya Road and in the Aberdeen Tunnel -- one
    neighbourhood, largely one scene. Striding gives tunnels, trunk roads,
    the harbourfront and the New Territories, which is what shows an operator
    that the layer works rather than that one street is visible.
    """
    if limit <= 0:
        return []
    if len(cameras) <= limit:
        return list(cameras)

    step = len(cameras) / float(limit)
    return [cameras[min(int(i * step), len(cameras) - 1)] for i in range(limit)]


def _reference_streams(db, timeout_s=None):
    """Cameras from another authority, flagged as not being this city's.

    Returns ``(streams, meta)``, or ``([], None)`` when no reference feed is
    configured or the stand-in provider fails. See the "Reference feed" block
    in twin/config.py for why this exists and what it is not allowed to be.

    Every stream is stamped ``reference=True`` here rather than at the edges.
    A caller that forgets to check the flag still cannot mistake one for
    local ground, because ``lat``/``lon`` are dropped: a reference camera has
    no position *in this city*, and publishing the Hong Kong coordinate would
    invite exactly the "pin on the map" reading this must not have.
    """
    from .ingest.cctv_live import CctvLiveAdapter, reference_provider

    provider = reference_provider()
    if provider is None:
        return [], None

    try:
        data, snapshot = CctvLiveAdapter().run(
            db, bbox=provider.coverage,
            timeout_s=timeout_s or config.CCTV_LIVE_TIMEOUT_S)
    except Exception as exc:                      # noqa: BLE001 - never break the panel
        log.warning("cctv reference feed %s failed (%s)", provider.key, exc)
        return [], None

    cameras = (data or {}).get("cameras") or []
    if not cameras:
        return [], None

    streams = []
    for camera in _spread(cameras, config.CCTV_REFERENCE_MAX):
        stream = dict(camera)
        stream["reference"] = True
        stream["reference_region"] = provider.region
        stream["reference_provider"] = provider.name
        stream["lat"] = None
        stream["lon"] = None
        stream["distance_m"] = None
        streams.append(stream)

    return streams, {
        "provider": provider.key,
        "name": provider.name,
        "region": provider.region,
        "count": len(streams),
        "catalog_count": len(cameras),
        "status": snapshot.status if snapshot is not None else "unknown",
        "note": ("No authority publishes a camera catalog for this city, so "
                 "these feeds are from %s. They are live and real, but they "
                 "are not this city's ground: they are shown so the layer can "
                 "be seen working, are kept off the map, and are not read by "
                 "scoring, flags or briefs." % provider.region),
    }
