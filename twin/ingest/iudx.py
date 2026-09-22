"""IUDX -- India Urban Data Exchange.

IUDX is the closest thing India has to a single tap for municipal sensor data:
flood sensors, air quality, traffic, streetlights, published by participating
cities through one NGSI-LD interface. Two things about it shape this adapter:

* **The catalogue is public; the data mostly is not.** Anyone may search what
  exists. Reading a resource generally needs an account and a per-resource
  token, and which resources a token opens differs per deployment.
* **Coverage is per-city and changes.** Neither modelled city can be assumed
  to have any particular resource, so hard-coding resource ids would produce
  a layer that is silently empty.

So this adapter does discovery first: it asks the catalogue what exists near
the city and stores the answer, which makes the "not configured" state
*informative* -- an operator sees "14 flood sensors published for this city,
token required" instead of an empty layer that looks broken. When a token is
configured, the same code path pulls the latest entities for the resources it
can actually read.
"""

import logging

from .. import config
from .base import IngestAdapter

log = logging.getLogger("twin.ingest.iudx")

#: Catalogue tags worth surfacing for a disaster twin, in priority order.
RELEVANT_TAGS = ("flood", "water", "rainfall", "aqm", "air quality",
                 "weather", "traffic", "drainage")


class IudxCatalogueAdapter(IngestAdapter):
    """What IUDX publishes that this twin could use. Keyless."""

    source_key = "iudx"
    #: A catalogue entry is a published dataset, not a reading.
    cache_ttl_s = 24 * 60 * 60
    max_retries = 1

    def fetch_raw(self, tags=RELEVANT_TAGS, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s

        resources, errors = [], []
        for tag in tags:
            try:
                response = self.session.get(
                    "%s/search" % config.IUDX_CATALOGUE_URL,
                    params={"property": "[tags]", "value": "[[%s]]" % tag},
                    timeout=timeout_s)
                if response.status_code != 200:
                    errors.append("%s:%s" % (tag, response.status_code))
                    continue
                for entry in (response.json().get("results") or []):
                    resources.append(_catalogue_entry(entry, tag))
            except Exception as exc:  # noqa: BLE001 - one tag must not sink the rest
                errors.append("%s:%s" % (tag, type(exc).__name__))

        # The same resource is tagged several ways; de-duplicate on id so a
        # count means "distinct datasets", which is what an operator reads it as.
        unique = {}
        for resource in resources:
            unique.setdefault(resource["id"], resource)

        return {
            "resources": list(unique.values()),
            "errors": errors,
            "token_configured": bool(config.IUDX_TOKEN),
            "source": "IUDX",
            "attribution": "India Urban Data Exchange (IUDX)",
        }

    def neutral_value(self, **_):
        return {"resources": [], "unavailable": True, "source": "IUDX",
                "token_configured": bool(config.IUDX_TOKEN),
                "attribution": "India Urban Data Exchange (IUDX)"}

    def record_count(self, data):
        return len((data or {}).get("resources") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        tags = ",".join(kwargs.get("tags") or RELEVANT_TAGS)
        return hashlib.sha1(("iudx:%s" % tags).encode("utf-8")).hexdigest()


def _catalogue_entry(entry, tag):
    location = ((entry.get("location") or {}).get("geometry") or {})
    coordinates = location.get("coordinates") or []
    lat = lon = None
    if location.get("type") == "Point" and len(coordinates) >= 2:
        lon, lat = coordinates[0], coordinates[1]

    return {
        "id": entry.get("id"),
        "label": entry.get("label") or entry.get("name"),
        "description": entry.get("description"),
        "tags": entry.get("tags") or [tag],
        "provider": entry.get("provider"),
        "instance": entry.get("instance"),
        "resource_group": entry.get("resourceGroup"),
        "lat": lat,
        "lon": lon,
        "matched_tag": tag,
    }


class IudxResourceAdapter(IngestAdapter):
    """Latest entities for one IUDX resource. Needs ``IUDX_TOKEN``."""

    source_key = "iudx"
    cache_ttl_s = 5 * 60
    max_retries = 1

    def fetch_raw(self, resource_id=None, timeout_s=None, **_):
        if not resource_id:
            raise ValueError("iudx resource fetch needs a resource_id")
        if not config.IUDX_TOKEN:
            return {"entities": [], "not_configured": True, "resource_id": resource_id,
                    "source": "IUDX", "attribution": "India Urban Data Exchange (IUDX)"}

        timeout_s = timeout_s or self.timeout_s
        response = self.session.get(
            "%s/entities" % config.IUDX_RESOURCE_URL,
            params={"id": resource_id},
            headers={"token": config.IUDX_TOKEN},
            timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()

        return {
            "resource_id": resource_id,
            "entities": payload.get("results") or [],
            "source": "IUDX",
            "attribution": "India Urban Data Exchange (IUDX)",
        }

    def neutral_value(self, resource_id=None, **_):
        return {"entities": [], "resource_id": resource_id, "unavailable": True,
                "source": "IUDX", "attribution": "India Urban Data Exchange (IUDX)"}

    def record_count(self, data):
        return len((data or {}).get("entities") or [])

    def _cache_key(self, city_id, kwargs):
        import hashlib
        return hashlib.sha1(
            ("iudx:res:%s" % kwargs.get("resource_id")).encode("utf-8")).hexdigest()


def entities_to_observations(data, kind="water_level"):
    """IUDX NGSI-LD entities -> the twin's observation shape.

    IUDX resources do not share a property vocabulary -- a flood sensor may
    call its reading ``waterLevel``, ``level`` or ``measuredDistance`` -- so a
    handful of spellings are tried and anything unrecognised is kept in
    ``metrics`` rather than discarded. An unknown field is still evidence.
    """
    observations = []
    for entity in (data or {}).get("entities") or []:
        location = (entity.get("location") or {}).get("coordinates") or []
        if len(location) < 2:
            continue

        value = None
        for key in ("waterLevel", "level", "measuredDistance", "aqi", "value"):
            if entity.get(key) is not None:
                try:
                    value = float(entity[key])
                    break
                except (TypeError, ValueError):
                    continue

        observations.append({
            "source_key": "iudx",
            "station_uid": "iudx:%s" % (entity.get("id") or entity.get("deviceId")),
            "kind": kind,
            "name": entity.get("name") or entity.get("deviceId"),
            "operator": "IUDX",
            "lat": location[1],
            "lon": location[0],
            "value": value,
            "unit": entity.get("unit"),
            "metrics": {k: v for k, v in entity.items()
                        if isinstance(v, (int, float, str)) and k not in ("id", "name")},
            "observed_at": entity.get("observationDateTime") or entity.get("time"),
        })
    return observations
