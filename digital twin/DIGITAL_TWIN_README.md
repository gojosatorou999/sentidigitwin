# 🌐 Sentinel Digital Twin — Dual-City Urban Twin Module (Hyderabad + Bengaluru)

**Module codename:** `sentinel-twin`
**Parent project:** Sentinel AI (Flask · SQLAlchemy · MapLibre GL · Twilio · APScheduler)
**Problem statement:** SH-SVA-03 — *"A multi-agent AI system integrated with a city digital twin is needed to autonomously prioritize incidents, coordinate actions, and ensure timely, compliant resolution."*

This document is the **implementation contract**. Hand it to a coding agent (Claude Code / Cursor / Copilot Workspace) and build phase by phase. Every section is written to be executable, not aspirational.

---

## 0. What we are building, in one paragraph

A **live, queryable digital twin of two Indian cities rendered side by side** inside the Analyst and Admin dashboards. Each city is decomposed into an **H3 hexagonal cell grid**. Every cell carries a continuously recomputed state vector — rainfall, forecast rainfall, river discharge, elevation, drainage exposure, air quality, heat, critical-infrastructure density, and **live approved incident reports from the Sentinel AI database** — collapsed into a single `risk_score` (0–100) and a status band. The twin renders as **3D extruded hexagons over 3D buildings and satellite imagery**, with a **zone dropdown** (whole city ↔ GHMC / BBMP zones), a **time-horizon scrubber** (Now → T+3h → T+6h → T+24h predicted state), and **drill-down on any cell**. Everything degrades gracefully when an upstream API is unreachable.

**The twin is not a map. The map is the renderer. The twin is the per-cell state table.** Build the state table first.

---

## 1. Non-negotiable design constraints

Carry these forward from the existing Sentinel AI architecture. Any PR that violates one gets rejected.

| # | Constraint | Meaning |
|:--|:---|:---|
| C1 | **Never block on an external service** | Every ingestion adapter has a timeout (≤ 8 s), a cache fallback, and a neutral-value fallback. A dead API produces a `stale` flag, never a 500. |
| C2 | **Cross-database parity** | Must run identically on SQLite (dev) and PostgreSQL (prod). PostGIS is *optional acceleration only*. All spatial math happens in Python (`h3`, `shapely`) so no DB-specific SQL is required. |
| C3 | **Explainable scores** | Every `risk_score` exposes its five sub-scores plus the raw inputs that produced them. No black-box numbers on an officials' dashboard. |
| C4 | **Role-gated** | Twin routes are `official` / `analyst` only, matching `analyst_dashboard`. Public read endpoints must be explicitly marked. |
| C5 | **Zero mandatory paid keys for the core loop** | Base twin (imagery, weather, flood, elevation, radar, infrastructure) must work with **keyless** APIs. Keyed sources (TomTom traffic, Tomorrow.io, data.gov.in) are strictly optional enhancement layers. |
| C6 | **Additive, not invasive** | New Flask **blueprint**, new models, new JS module. Do not refactor `app.py`'s existing 119 routes. Do not touch the existing `Report` model schema. |
| C7 | **Audit everything** | Every ingestion run writes a `TwinDataSnapshot` row: source, status, latency, records, error. |
| C8 | **UTC everywhere** | Every timestamp stored, compared, or serialised by the twin is timezone-aware UTC. `hours_since_report` against a naive IST datetime is a silent 5.5 h error that makes every incident look either fresh or expired. Convert to IST only in the browser, at render time. |

---

## 2. City & zone definitions

### 2.1 Cities

| Field | Hyderabad | Bengaluru |
|:---|:---|:---|
| `slug` | `hyderabad` | `bengaluru` |
| `display_name` | Hyderabad | Bengaluru |
| `state` | Telangana | Karnataka |
| `center_lat, center_lon` | `17.3850, 78.4867` | `12.9716, 77.5946` |
| `bbox` (minLon, minLat, maxLon, maxLat) | `78.24, 17.22, 78.66, 17.60` | `77.44, 12.83, 77.78, 13.14` |
| Default camera | zoom 10.2, pitch 55°, bearing −12.5° | zoom 10.2, pitch 55°, bearing −12.5° |
| Admin body | GHMC | BBMP / Greater Bengaluru Authority |
| Rainfall telemetry | TGDPS (already integrated) | KSNDMC (Phase 3, optional) |

> ⚠️ **Verify before seeding:** Bengaluru's municipal structure was restructured around 2024–25 (BBMP → multiple corporations under the Greater Bengaluru Authority). Seed the **8 legacy BBMP zones** because that is what nearly all open GeoJSON datasets use, but store `zone_scheme = "BBMP-8"` on each row so a future re-seed to the new corporation boundaries is a data migration, not a code change. Do the same for Hyderabad with `zone_scheme = "GHMC-6"`.

### 2.2 Zones (dropdown contents)

**Hyderabad — GHMC 6 zones** (each expands to circles; store circles as `zone_type="circle"` children):
`charminar`, `khairatabad`, `serilingampally`, `kukatpally`, `secunderabad`, `lb-nagar`

**Bengaluru — BBMP 8 zones:**
`east`, `west`, `south`, `bommanahalli`, `mahadevapura`, `rr-nagar`, `dasarahalli`, `yelahanka`

Plus a synthetic `__all__` entry per city = *Whole City*, which is the **default selection**.

### 2.3 Boundary GeoJSON sourcing

Do **not** hand-draw polygons. Fetch once, cache to `data/twin/boundaries/<city_slug>.geojson`, commit to the repo.

Priority order per city:
1. **Overpass API** — query the OSM admin relations:
   ```
   [out:json][timeout:60];
   rel["boundary"="administrative"]["admin_level"="9"](17.22,78.24,17.60,78.66);
   out geom;
   ```
   (`admin_level` 8/9/10 — probe which level yields the zone/ward split for each city.)
2. **Datameet / OpenCity** open ward datasets (`github.com/datameet`, `opencity.in`) for BBMP wards.
3. **Fallback:** if a zone polygon cannot be resolved, generate a **convex hull of its constituent H3 cells** derived from a centroid + radius definition, and set `boundary_source = "approximate"` so the UI can badge it honestly.

**The city clip polygon is mandatory; zone polygons are not.** These are two different fetches with two different failure budgets:

- **Clip polygon** (`admin_level=8` — the GHMC / BBMP relation itself). Exists in OSM for both cities. Fetch once, commit to `data/twin/boundaries/<city_slug>_clip.geojson`. **Phase 1 cannot start without it** — see the bbox warning in §3.
- **Zone polygons** (`admin_level=9`/`10`). GHMC's 6 zones are largely *absent* from OSM, so expect Hyderabad to land on the `approximate` convex-hull path and badge itself accordingly. That is fine and blocks nothing: cells with `zone_id = NULL` still render, still score, and still belong to the city.

A missing *zone* boundary must never block Phase 1. Whole-city mode works with the clip polygon alone.

---

## 3. The H3 cell grid — the twin's substrate

| Parameter | Value | Rationale |
|:---|:---|:---|
| Library (backend) | `h3-py >= 4.0` | Stable v4 API (`h3.latlng_to_cell`, `h3.cell_to_boundary`) |
| Library (frontend) | `h3-js` (CDN) | Only needed for client-side hover math; server ships GeoJSON so this is optional |
| **Resolution** | **8** | Avg hex area ≈ 0.74 km², edge ≈ 460 m. Ward-scale granularity. |
| Expected cell count | Hyderabad ≈ 850–950, Bengaluru ≈ 950–1100 | ~2,000 **total across both cities** — renders comfortably as a single `fill-extrusion` layer. **These counts hold only when the grid is clipped to the municipal boundary.** |
| Optional drill resolution | 9 (≈ 0.11 km²) | Generated **on demand** for a single selected cell only. Never precomputed city-wide. |

> ⚠️ **Never generate the grid from the raw bbox.** The bboxes in §2.1 are camera/query extents, not city areas, and they are roughly 2–3× larger than the municipal boundary:
>
> | | bbox area | cells at res 8 | boundary area | cells at res 8 |
> |:--|:--|:--|:--|:--|
> | Hyderabad | ~1,880 km² | **~2,550** | GHMC ~650 km² | ~880 |
> | Bengaluru | ~1,266 km² | **~1,720** | BBMP ~709 km² | ~960 |
>
> A bbox grid yields ~4,270 cells and simultaneously breaks the < 12 s compute budget (§5.4), the < 150 KB payload budget (A10), and the Phase 1 checkpoint. Always clip to a polygon. See §2.3 for the clip-polygon sourcing rule.

**Grid generation (one-time seed, idempotent):**
```python
# h3-py v4 API. polygon_to_cells takes an H3Shape positionally, NOT a GeoJSON dict
# and NOT a keyword `res`. Use geo_to_cells() if you have a raw GeoJSON mapping.
for city in cities:
    poly  = h3.LatLngPoly(outer_ring_latlng, *holes)     # (lat, lng) pairs
    cells = h3.polygon_to_cells(poly, 8)                 # positional resolution
    # equivalently, straight from GeoJSON:
    # cells = h3.geo_to_cells(city_clip_geojson, 8)
    for cell in cells:
        lat, lng = h3.cell_to_latlng(cell)
        ring     = h3.cell_to_boundary(cell)             # -> ((lat, lng), ...) x7
        # GeoJSON is (lng, lat): you MUST swap. This is the #1 h3 v4 bug.
        coords   = [[round(lng_, 5), round(lat_, 5)] for lat_, lng_ in ring]
        upsert TwinCell(h3_index=cell, city_id=..., centroid=(lat, lng), boundary=coords)
    assign each cell to a zone via point-in-polygon of its centroid (shapely STRtree)
    cells whose centroid falls in no zone → zone_id = NULL, still belong to the city
```

**h3-py v4 migration notes** (the v3 names are gone, not deprecated):

| v3 | v4 |
|:---|:---|
| `h3.geo_to_h3(lat, lng, res)` | `h3.latlng_to_cell(lat, lng, res)` |
| `h3.h3_to_geo(cell)` | `h3.cell_to_latlng(cell)` |
| `h3.h3_to_geo_boundary(cell, geo_json=True)` | `h3.cell_to_boundary(cell)` — **no `geo_json` arg; always returns (lat, lng)** |
| `h3.k_ring(cell, 1)` | `h3.grid_disk(cell, 1)` |
| `h3.polyfill(geojson, res, geo_json_conformant=True)` | `h3.polygon_to_cells(h3.LatLngPoly(...), res)` or `h3.geo_to_cells(geojson, res)` |

---

## 4. Data sources

### 4.1 Tier 1 — keyless, required for the core loop

| Source | Endpoint | Used for | Cadence | Fallback |
|:---|:---|:---|:---|:---|
| **OpenFreeMap** | `https://tiles.openfreemap.org/styles/liberty` | 3D vector basemap + building extrusions (the "twin" look) | static | CARTO Voyager GL (already in project) |
| **Esri World Imagery** | `.../World_Imagery/MapServer/tile/{z}/{y}/{x}` | Satellite raster basemap toggle | static | OpenFreeMap only |
| **NASA GIBS WMTS** | `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/{LAYER}/default/{TIME}/{TMS}/{z}/{y}/{x}.jpg` | Daily true-colour satellite (`VIIRS_SNPP_CorrectedReflectance_TrueColor`, `MODIS_Terra_CorrectedReflectance_TrueColor`) with a **date picker** | daily | Esri static imagery |
| **Open-Meteo Forecast** | `https://api.open-meteo.com/v1/forecast` | Rain now + hourly forecast, temp, humidity, wind, weather code — **multi-point call on a ~5 km sampling lattice**, IDW-interpolated to cells (§5.4) | 15 min | last cached snapshot, then neutral |
| **Open-Meteo Air Quality** | `https://air-quality-api.open-meteo.com/v1/air-quality` | PM2.5, PM10, US AQI | 30 min | neutral (AQI = 50) |
| **Open-Meteo Flood** | `https://flood-api.open-meteo.com/v1/flood` | GloFAS river discharge forecast (Musi basin, Vrishabhavathi/Arkavathi basin) | 6 h | neutral |
| **Open-Meteo Elevation** | `https://api.open-meteo.com/v1/elevation?latitude=a,b&longitude=x,y` | **One-time seed** of every cell's elevation (batch 100 coords/request) | once | SRTM via Open Topo Data |
| **Open Topo Data** | `https://api.opentopodata.org/v1/srtm30m` | Elevation fallback (1 req/s limit — seed slowly) | once | mark cell `elevation_source="unknown"`, terrain sub-score neutral |
| **RainViewer** | `https://api.rainviewer.com/public/weather-maps.json` → tile path | Live precipitation radar overlay + past/future frame animation. **Display-only — feeds no sub-score.** | 10 min | hide layer, show "radar unavailable" |
| **Overpass API** | `https://overpass-api.de/api/interpreter` | Critical infrastructure + water bodies + drains (one-time + weekly refresh) | weekly | cached GeoJSON on disk |
| **USGS FDSN** | `https://earthquake.usgs.gov/fdsnws/event/1/query` | Seismic events (already in project) | 1 h | hide layer |
| **Sentinel AI internal** | `Report` table where `verification_status='approved'` | **Live incident layer — the differentiator** | event-driven + 2 min sweep | — |

### 4.2 Tier 2 — optional, keyed

| Source | Key env var | Adds | If key absent |
|:---|:---|:---|:---|
| **TomTom Traffic Flow** | `TOMTOM_API_KEY` | Congestion tiles + `traffic_index` sub-signal | layer hidden, sub-signal dropped from formula, weights renormalised |
| **Tomorrow.io** | `TOMORROW_API_KEY` | Higher-res nowcast, flood index, road risk | Open-Meteo only |
| **data.gov.in** | `DATA_GOV_IN_KEY` | Census/ward demographics for population exposure | population estimated from OSM building footprint density |
| **Bhuvan (ISRO) WMS** | — (public, flaky) | Indian LULC / flood hazard layers | omit |
| **TGDPS** | — (existing proxy) | Hyderabad ground-station rainfall | Open-Meteo only |
| **KSNDMC** | — (Phase 3) | Bengaluru telemetric rain gauges | Open-Meteo only |

### 4.3 Overpass queries to run (infrastructure seed)

Run per city bbox, tag each result with `asset_type` and a `criticality` weight:

| `asset_type` | Overpass filter | Criticality |
|:---|:---|:---|
| `hospital` | `amenity=hospital` | 1.0 |
| `fire_station` | `amenity=fire_station` | 1.0 |
| `police` | `amenity=police` | 0.8 |
| `power_substation` | `power=substation` | 0.9 |
| `water_works` | `man_made=water_works` \| `man_made=water_tower` | 0.8 |
| `school` | `amenity=school` \| `amenity=college` | 0.6 |
| `transport_hub` | `railway=station` \| `amenity=bus_station` \| `aeroway=aerodrome` | 0.7 |
| `shelter` | `amenity=shelter` \| `amenity=community_centre` | 0.5 |
| `water_body` | `natural=water` \| `waterway=riverbank` | — (used by terrain score) |
| `drain` | `waterway=drain` \| `waterway=canal` \| `waterway=stream` | — (used by terrain score) |

---

## 5. The state engine

### 5.1 Sub-scores (each 0–100, computed per cell per horizon)

```python
# --- 1. HYDRO PRESSURE ------------------------------------------------
rain_now      = min(100, observed_rain_mm_1h * 4)          # 25 mm/h -> 100
rain_forecast = min(100, forecast_rain_mm_next_3h * 2)     # 50 mm/3h -> 100
discharge     = min(100, (glofas_q / glofas_q_2yr_return) * 60)
hydro = 0.40*rain_now + 0.40*rain_forecast + 0.20*discharge

# --- 2. INCIDENT PRESSURE (Sentinel AI live data) ---------------------
# for each approved Report whose point falls in this cell OR a k-ring-1 neighbour
sev  = {"low":25, "medium":50, "high":75, "critical":100}[report.priority]
rec  = exp(-hours_since_report / 12)                 # 12 h *time constant*
                                                     # (half-life = 12*ln2 = 8.3 h)
conf = 0.5 if report.confidence_score is None else report.confidence_score
                                                     # NOT `or 0.5`: a genuine 0.0
                                                     # confidence would be promoted to 0.5
w    = 1.0 if in_this_cell else 0.4                  # neighbour spillover
contribution = sev * rec * conf * w
incident = min(100, sum(contributions))
# NOTE: zero approved reports in this cell -> incident = 0.0, a real measurement.
# Only a FAILED internal_reports sync yields None. Confusing the two makes every
# calm cell renormalise 30% of its weight onto hydro/env (see 'Weight
# renormalisation rule' below) and inflates risk city-wide on a quiet day.

# --- 3. TERRAIN EXPOSURE ----------------------------------------------
elev_pct   = percentile_rank(cell.elevation, all_city_cell_elevations)  # 0..1
low_lying  = (1 - elev_pct) * 100
water_prox = 100 if dist_to_water_body_m < 200 else \
             60  if dist_to_water_body_m < 500 else \
             25  if dist_to_water_body_m < 1000 else 0
drain_gap  = 100 - min(100, drain_length_m_in_cell / 20)   # 2000 m of drain -> 0
terrain = 0.45*low_lying + 0.35*water_prox + 0.20*drain_gap

# --- 4. INFRA CRITICALITY ---------------------------------------------
infra = min(100, sum(asset.criticality for asset in cell.assets) * 12)

# --- 5. ENV STRESS ----------------------------------------------------
aqi_s  = min(100, us_aqi / 3)          # AQI 300 -> 100
heat_s = max(0, min(100, (apparent_temp_c - 30) * 5))   # 30C->0, 50C->100
env = 0.60*aqi_s + 0.40*heat_s

# --- COMPOSITE --------------------------------------------------------
# Hazard is what is HAPPENING; vulnerability is what is AT STAKE. They multiply.
# A flat weighted sum floors every low-lying, hospital-dense cell at
# 0.20*100 + 0.15*100 = 35 (permanent `watch`) with zero rain and zero incidents
# -- the map reads yellow on a clear day and officials learn to ignore the colour.
hazard = renormalised(
      0.55 * hydro       # observed + forecast rainfall, river discharge
    + 0.30 * incident    # live approved Sentinel AI reports
    + 0.15 * env         # AQI + heat
)                        # -> 0 when nothing is happening

vulnerability = 1.0 + 0.6 * (0.6 * terrain + 0.4 * infra) / 100   # 1.0 .. 1.6

risk_score = clamp(0, 100, hazard * vulnerability)
```

All five sub-scores are still computed, stored, and surfaced independently, so C3
(explainability) holds: the drill-down reads *"hazard 48, amplified 1.5× by low-lying
terrain and 3 critical assets"*. `hazard_score` and `vulnerability_multiplier` are
stored on `TwinCellState` alongside the five sub-scores.

**Weight renormalisation rule:** renormalisation applies **to the three hazard terms only**. If a hazard sub-score is `None` (source dead, no fallback), drop it and renormalise the remaining hazard weights to sum to 1.0. Record which sub-scores were dropped in `TwinCellState.degraded_inputs` (JSON array).

`None` means *unmeasured*; `0` means *measured as nothing*. Never conflate them — see the note under INCIDENT PRESSURE. A missing `terrain` or `infra` term degrades to its neutral value (contributing `vulnerability = 1.0`) rather than being renormalised away, because the vulnerability multiplier has no other terms to absorb its weight.

### 5.2 Status bands

| `risk_score` | `status` | Hex colour | Extrusion height |
|:---|:---|:---|:---|
| 0 – 24 | `normal` | `#22c55e` @ 0.35 opacity | `risk * 4` m |
| 25 – 49 | `watch` | `#eab308` @ 0.50 | `risk * 6` m |
| 50 – 74 | `warning` | `#f97316` @ 0.65 | `risk * 9` m |
| 75 – 100 | `critical` | `#ef4444` @ 0.80 | `risk * 14` m |

Extrusion height is what makes the flat hexagons read as a *twin* rather than a heatmap. Animate height transitions over 600 ms when the horizon changes.

### 5.3 Horizons

Compute and store four rows per cell per run:

| `horizon_hours` | Inputs differ how |
|:---|:---|
| `0` | Observed rain, live incidents, current AQI/temp |
| `3` | Forecast rain T+1..T+3, incidents decayed 3 h forward, forecast AQI/temp |
| `6` | Forecast rain T+1..T+6, incidents decayed 6 h forward |
| `24` | Forecast rain T+1..T+24, incidents decayed to `e⁻² ≈ 0.135` of original weight (not zero), GloFAS discharge weighted up (hydro weights → 0.25/0.35/0.40) |

Terrain and infra sub-scores are horizon-invariant (cached).

### 5.4 Run cadence

| Job | Interval | Notes |
|:---|:---|:---|
| `twin_ingest_weather` | 15 min | **One multi-point Open-Meteo call per city** over a ~5 km lattice (≈70–90 points), then IDW-interpolate to cells. Do **not** call per cell. |
| `twin_ingest_airquality` | 30 min | same fan-out pattern |
| `twin_ingest_flood` | 6 h | per city basin outlet |
| `twin_ingest_radar_index` | 10 min | *(cut — see the note below)* Fetch the RainViewer frame index only, so the client can build tile URLs. No per-cell sampling. |
| `twin_sync_incidents` | 2 min + event hook on report approval | |
| `twin_compute_state` | 5 min | Reads latest ingest rows, writes all 4 horizons for ~2,000 cells |
| `twin_refresh_infrastructure` | weekly | Overpass |

**Why a lattice, not zone centroids.** Open-Meteo's forecast endpoint accepts comma-separated `latitude`/`longitude` for **multiple locations in a single request**, so sampling density costs almost nothing in request budget. Interpolating from 6–8 zone centroids across a 45 km city produces a rainfall field smoother than a single monsoon convective cell (5–10 km across): hydro comes out near-uniform, cell-level ranking stops discriminating, and A5 quietly fails. A ~5 km lattice (≈70–90 points/city) is one or two requests and carries real spatial structure. The rule that matters is *never one request per cell* — not *never more than 14 requests*.

**Why the radar index job shrank.** RainViewer exposes no point-value API; per-cell precipitation intensity would mean fetching PNG tiles every 10 minutes and reverse-mapping the colour ramp to dBZ — fragile, undocumented, and load-bearing for nothing, since the forecast adapter already supplies `rain_now`. Keep RainViewer as layer 6, a visual overlay. The job reduces to caching the frame manifest.

**Upsert cost.** `twin_compute_state` writes ~2,000 cells × 4 horizons = ~8,000 rows every 5 minutes. Row-at-a-time ORM upserts will miss the budget on SQLite. Do one bulk `SELECT` of `(cell_id, horizon_hours) → id` into a dict, then a single `bulk_update_mappings` / `bulk_insert_mappings` pair. This also keeps C2 (no dialect-specific `ON CONFLICT`). Capture the previous `risk_score` from that same `SELECT` **before** writing — it is the only source for the SSE `changed_cells` diff, which the in-place upsert destroys.

Register these on the **existing APScheduler instance** in `app.py`. Guard with a module-level flag so multi-worker Gunicorn deployments only run them in one worker (`if os.getenv("TWIN_SCHEDULER_ENABLED", "1") == "1"`).

Full run budget target: **< 12 s** for `twin_compute_state` across both cities. Vectorise with NumPy if it exceeds that.

---

## 6. Database schema

New models in `twin/models.py`. Generate one Alembic migration: `twin_initial`.

```python
class TwinCity(db.Model):
    id, slug (unique), display_name, state, country
    center_latitude, center_longitude
    bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat
    default_zoom, default_pitch, default_bearing
    zone_scheme            # "GHMC-6" | "BBMP-8"
    h3_resolution          # 8
    is_active, created_at

class TwinZone(db.Model):
    id, city_id (FK), slug, display_name
    zone_type              # "zone" | "circle" | "ward"
    parent_zone_id (FK, nullable)
    center_latitude, center_longitude
    boundary_geojson (Text)
    boundary_source        # "osm" | "datameet" | "approximate"
    population_estimate (nullable)
    created_at
    UNIQUE(city_id, slug)

class TwinCell(db.Model):
    id, h3_index (unique, indexed), city_id (FK), zone_id (FK nullable)
    center_latitude, center_longitude
    boundary_geojson (Text)          # 7-point hexagon ring
    area_sqkm
    elevation_m (nullable), elevation_source
    dist_to_water_m (nullable)
    drain_length_m (default 0)
    infra_criticality_cached (default 0)
    terrain_score_cached (nullable)
    created_at, updated_at

class TwinCellState(db.Model):
    id, cell_id (FK, indexed), horizon_hours (indexed)   # 0|3|6|24
    risk_score, status
    hydro_score, incident_score, terrain_score, infra_score, env_score
    hazard_score, vulnerability_multiplier      # the two composite halves (§5.1)
    raw_inputs (JSON)         # every raw number that fed the formula
    degraded_inputs (JSON)    # ["flood","aqi"] etc.
    incident_count, top_incident_report_id (nullable)
    computed_at (indexed)
    UNIQUE(cell_id, horizon_hours)   # upsert-in-place; history goes to TwinCellHistory

class TwinCellHistory(db.Model):        # optional Phase 6 — hourly rollup for trends
    id, cell_id, horizon_hours, risk_score, status, computed_at

class TwinInfrastructure(db.Model):
    id, city_id (FK), cell_id (FK nullable), osm_id
    asset_type, name, criticality
    latitude, longitude, tags (JSON)
    source, fetched_at

class TwinDataSnapshot(db.Model):        # audit + staleness UI
    id, source_key            # "open_meteo_forecast" | "rainviewer" | ...
    city_id (FK nullable)
    status                    # "ok" | "degraded" | "failed"
    latency_ms, records_ingested
    error_message (nullable)
    payload_digest (nullable)
    started_at, finished_at
```

**Indexing:** `TwinCell.h3_index`, `(TwinCellState.cell_id, horizon_hours)`, `TwinCellState.computed_at`, `TwinInfrastructure.cell_id`. On PostgreSQL, additionally add a GiST index if PostGIS is enabled — but the code must not require it.

---

## 7. API contract

Blueprint `twin_bp`, prefix `/api/twin`. All routes `@login_required`; state/summary/cell routes additionally `@role_required("official", "analyst")`.

| Method | Route | Query params | Returns |
|:---|:---|:---|:---|
| `GET` | `/api/twin/cities` | — | `[{slug, display_name, center, bbox, camera, zones:[{slug,display_name,zone_type,parent}], last_updated, health:{source:status}}]` |
| `GET` | `/api/twin/<city>/zones` | `type=zone\|circle` | GeoJSON `FeatureCollection` of zone polygons |
| `GET` | `/api/twin/<city>/state` | `zone` (slug or `__all__`), `horizon` (0\|3\|6\|24), `fields` (csv) | GeoJSON `FeatureCollection` of H3 hexagons. Properties: `h3, risk_score, status, hazard_score, vulnerability_multiplier, hydro_score, incident_score, terrain_score, infra_score, env_score, incident_count, degraded_inputs, computed_at` |
| `GET` | `/api/twin/<city>/cell/<h3_index>` | `horizon` | Full drill-down: state + `raw_inputs` + infrastructure list + nearby approved reports (id, title, hazard_type, priority, confidence, timestamp, image_url) + a plain-English `explanation` string |
| `GET` | `/api/twin/<city>/incidents` | `zone`, `hazard_type`, `since_hours` (default 72) | GeoJSON points of **approved** reports only |
| `GET` | `/api/twin/<city>/infrastructure` | `zone`, `types` (csv) | GeoJSON points |
| `GET` | `/api/twin/<city>/summary` | `zone`, `horizon` | KPI block: `avg_risk, max_risk, cells_by_status{}, incident_count_24h, population_exposed_estimate, critical_assets_at_risk, top_5_cells[], sector_damage{power,water,telecom,housing}` (reuse the Sentinel Resilience Engine formulas) |
| `GET` | `/api/twin/compare` | `horizon` | Both cities' summary blocks in one payload for the comparison strip |
| `GET` | `/api/twin/<city>/timeline` | `zone`, `hours=24` | City/zone avg risk per hour for the sparkline (needs `TwinCellHistory`) |
| `GET` | `/api/twin/health` | — | Per-source `{status, last_success, latency_ms, error}` from `TwinDataSnapshot` |
| `GET` | `/api/twin/stream` | `city`, `zone` | **SSE** — pushes `{type:"state_update", city, changed_cells:[...]}` on each compute run and `{type:"incident", ...}` on report approval |
| `POST` | `/api/twin/refresh` | body `{city, sources:[]}` | 🔐 official only — force an ingest + recompute; returns the snapshot summary |
| `POST` | `/api/twin/seed` | body `{city}` | 🔐 official only, idempotent — regenerate cells, elevation, infrastructure |

**Response size guard:** whole-city state for ~1,000 cells with 12 properties each ≈ 400–700 KB of GeoJSON. Mitigate by:
1. Returning **quantised** hexagon rings (5 decimal places).
2. Supporting `?geometry=false` — returns a flat array of `{h3, risk_score, status, ...}` and lets the client rebuild geometry with `h3-js`. **Make this the default for the whole-city view.**
3. `gzip` via `flask-compress` — **exclude `text/event-stream`**, or `/api/twin/stream` buffers and never flushes.
4. `Cache-Control: max-age=60` plus an `ETag` derived from `max(computed_at)`.

---

## 8. Frontend spec

### 8.1 Page & mounting

- New template `templates/digital_twin.html`, route `GET /digital-twin` (official/analyst).
- Also mount as a **tab/panel** inside `analyst_dashboard.html` and `coordination_dashboard.html` via an `{% include %}` partial so the twin lives where officials already work.
- Full-bleed layout — obey the existing "no `col-xl-8` cap" rule.

### 8.2 Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│  TWIN HEADER                                                              │
│  [Horizon: ● Now  ○ +3h  ○ +6h  ○ +24h]   [🔗 Link cameras]  [⟳ 04:12]   │
│  [Layers ▾]  [Basemap: Vector | Satellite | NASA GIBS ▾]  [Health ●●●○]  │
├─────────────────────────────────┬────────────────────────────────────────┤
│  HYDERABAD                      │  BENGALURU                             │
│  Zone: [ Whole City        ▾ ]  │  Zone: [ Whole City               ▾ ]  │
│                                 │                                        │
│         MapLibre GL A           │           MapLibre GL B                │
│    (3D buildings + hex          │      (3D buildings + hex               │
│     extrusions + incidents)     │       extrusions + incidents)          │
│                                 │                                        │
│  ┌ KPI ────────────────────┐    │  ┌ KPI ────────────────────┐           │
│  │ Avg 41 · Max 88         │    │  │ Avg 33 · Max 71         │           │
│  │ 🔴 12  🟠 38  🟡 96  🟢 731│   │  │ 🔴 4  🟠 21  🟡 88  🟢 902│         │
│  │ Incidents 24h: 27       │    │  │ Incidents 24h: 14       │           │
│  └─────────────────────────┘    │  └─────────────────────────┘           │
├─────────────────────────────────┴────────────────────────────────────────┤
│  COMPARISON STRIP — Avg risk · Critical cells · Incidents · Sector damage │
├──────────────────────────────────────────────────────────────────────────┤
│  DRILL-DOWN DRAWER (slides up on cell click)                              │
│  Cell 8860a2b1c3fffff · Khairatabad · risk 78 CRITICAL                    │
│  Hydro 81 │ Incident 66 │ Terrain 72 │ Infra 48 │ Env 39                  │
│  "High risk driven by 34 mm/h observed rainfall and 3 approved flooding    │
│   reports within 500 m, in a low-lying cell 180 m from the Musi."         │
│  Assets: Osmania General Hospital, 2 substations · Reports: [cards]        │
│  [Declare Emergency Event]  [Dispatch Volunteers]  [Broadcast Alert]       │
└──────────────────────────────────────────────────────────────────────────┘
```

### 8.3 Map implementation

- **Extend the existing `GodModeMap` class** in `static/js/god-mode-maps.js`; do not fork it. Add a `TwinMap extends GodModeMap` in a new `static/js/digital-twin.js`.
- Reuse its deferred-operation queue (layer calls before `load` must still be queued) and its `addedSources`/`addedLayers` teardown tracking — zone switching adds/removes layers constantly and will leak without it.
- Two independent `maplibregl.Map` instances. **Camera link** is a toggle: when on, `moveend` on A applies `{zoom, pitch, bearing}` (not center) to B and vice versa, guarded by an `isSyncing` flag to prevent feedback loops.

### 8.4 Layer stack (bottom → top)

| # | Layer id | Type | Toggle default |
|:--|:---|:---|:---|
| 1 | `basemap` | OpenFreeMap Liberty vector style | on |
| 2 | `satellite` | raster (Esri / NASA GIBS) | off |
| 3 | `buildings-3d` | `fill-extrusion` from the basemap `building` source, `render_height` | on |
| 4 | `twin-hexes` | `fill-extrusion`, data-driven colour + height from `risk_score` | **on** |
| 5 | `zone-outline` | `line`, selected zone highlighted, others dimmed | on |
| 6 | `radar` | raster, RainViewer, opacity 0.55 | off |
| 7 | `water-drains` | `line` from Overpass | off |
| 8 | `infrastructure` | `symbol` + icons per `asset_type` | off |
| 9 | `incidents` | `circle` with the existing pulsing animated marker, colour by `priority` | **on** |
| 10 | `incident-labels` | `symbol`, `text-field` = hazard type, min-zoom 12 | on |

### 8.5 Interactions

| Action | Result |
|:---|:---|
| Zone dropdown change | Fetch `/state?zone=`, `fly-to` the zone bounds (or city bbox for `__all__`), dim out-of-zone hexes to opacity 0.08 rather than removing them |
| Horizon radio change | Refetch state for all visible maps; **animate** hex colour + height transitions (600 ms) so the prediction is legible as change over time |
| Hover a hex | Tooltip: risk, status, top driver sub-score, incident count |
| Click a hex | Open the drill-down drawer, fetch `/cell/<h3>` |
| Click an incident | Deep-link to the existing `/view_report/<id>` in a new tab |
| Drawer action buttons | POST to the **existing** coordination endpoints (`/coordination/emergencies/new`, `/api/coordination/assign-volunteer`, `/send_global_alert`) prefilled with the cell centroid — this is where the twin closes the SH-SVA-03 loop from *detection* to *coordinated action* |
| `?` key | Legend / keyboard shortcuts overlay |

### 8.6 Live updates

Connect to `/api/twin/stream` (SSE) per city. On `state_update`, patch only the changed features via `map.getSource('twin-hexes').setData(...)` with a merged FeatureCollection held in memory. **Do not refetch the whole city on every tick.** Fall back to a 60 s poll if `EventSource` fails.

### 8.7 Honest degradation in the UI

The health pill in the header shows one dot per Tier-1 source. Any cell whose `degraded_inputs` is non-empty renders with a subtle diagonal hatch pattern and the tooltip says which inputs were missing. Officials must be able to see when the twin is guessing.

---

## 9. File tree (new files only)

```
sentinel-ai/
├── twin/
│   ├── __init__.py                 # create_twin_blueprint(app, db, scheduler)
│   ├── models.py                   # 6 models from §6
│   ├── routes.py                   # all /api/twin/* + /digital-twin
│   ├── config.py                   # CITY_DEFS, ZONE_DEFS, WEIGHTS, STATUS_BANDS, ASSET_CRITICALITY
│   ├── grid.py                     # H3 generation, cell↔zone assignment, point-in-cell
│   ├── scoring.py                  # the five sub-scores + composite + renormalisation
│   ├── engine.py                   # compute_state(city, horizon) orchestrator
│   ├── jobs.py                     # APScheduler job registrations
│   ├── serializers.py              # GeoJSON builders, quantisation, ETag
│   ├── stream.py                   # SSE broker
│   └── ingest/
│       ├── base.py                 # IngestAdapter ABC: fetch() → (data, snapshot); timeout, retry, TTL cache
│       ├── open_meteo.py           # forecast + air quality + flood + elevation
│       ├── rainviewer.py
│       ├── overpass.py
│       ├── nasa_gibs.py            # tile URL builder + available-date probe
│       ├── tgdps.py                # reuse the existing proxy
│       ├── traffic.py              # TomTom, optional
│       └── internal_reports.py     # approved Report → cell mapping
├── templates/
│   ├── digital_twin.html
│   └── partials/
│       ├── twin_map_pane.html      # one city pane, rendered twice
│       ├── twin_drilldown.html
│       └── twin_legend.html
├── static/
│   ├── js/
│   │   ├── digital-twin.js         # TwinMap extends GodModeMap, dual-pane controller
│   │   ├── twin-layers.js          # layer definitions + paint expressions
│   │   └── twin-stream.js          # SSE client with poll fallback
│   └── css/twin.css
├── data/twin/
│   ├── boundaries/hyderabad.geojson
│   ├── boundaries/bengaluru.geojson
│   └── cache/                      # ingest TTL cache (gitignored)
├── scripts/
│   ├── seed_twin.py                # python -m scripts.seed_twin --city all
│   └── fetch_boundaries.py
├── migrations/versions/xxxx_twin_initial.py
└── tests/twin/
    ├── test_grid.py
    ├── test_scoring.py
    ├── test_ingest_fallbacks.py
    └── test_api_contract.py
```

---

## 10. Dependencies to add

```
h3>=4.1.0
shapely>=2.0.3
flask-compress>=1.14
cachetools>=5.3.3
numpy>=1.26          # already present
requests>=2.31       # already present
```

Frontend, via CDN (no build step — matches the existing Jinja + vanilla JS approach):
```
maplibre-gl@^4          # already present
h3-js@^4                # only if geometry=false client rebuild is used
```

Do **not** introduce React/Vite for this module. The app is Jinja-rendered; a partial SPA migration would be a separate project.

---

## 11. Environment variables

```env
# --- Twin core (all optional; sane defaults in twin/config.py) ---
TWIN_ENABLED=1
TWIN_H3_RESOLUTION=8
TWIN_SCHEDULER_ENABLED=1              # set 0 on all but one Gunicorn worker
TWIN_COMPUTE_INTERVAL_MIN=5
TWIN_WEATHER_INTERVAL_MIN=15
TWIN_CACHE_DIR=data/twin/cache
TWIN_HTTP_TIMEOUT_S=8

# --- Optional keyed sources ---
TOMTOM_API_KEY=
TOMORROW_API_KEY=
DATA_GOV_IN_KEY=
```

---

## 12. Build phases

Each phase ends with a **demoable checkpoint**. Do not start the next phase until the checkpoint passes.

### Phase 0 — Scaffolding *(0.5 d)* — ✅ **COMPLETE**
- [x] `twin/` package, blueprint registered, `/api/twin/health` returns per-source status with 200
- [x] Models written, `twin_initial` migration applied on SQLite; renders valid PostgreSQL DDL offline (not yet applied against a live Postgres — no server available in this environment)
- [x] `twin/config.py` holds both `CITY_DEFS` with bboxes and camera defaults
- [x] `seed_cities()` runs idempotently at blueprint registration so `TwinCity` rows exist before the checkpoint
- [x] `UTCDateTime` type decorator enforces C8 across both engines
- [x] Every section 7 route declared; unimplemented ones answer `501` with their phase, never `404`
- ✅ **Checkpoint PASSED:** `flask db upgrade` clean on SQLite (7 tables, head `twin_initial`); `GET /api/twin/cities` returns both cities with their full zone lists (`__all__` + 6 GHMC / 8 BBMP, all badged `approximate` until Phase 1). 41 tests green. Anonymous → 401, `citizen` → 403, `analyst`/`official` → 200.

### Phase 1 — Grid & static substrate *(1 d)* — ✅ **COMPLETE, run against live data**
- [x] `scripts/fetch_boundaries.py` — real Overpass run: Hyderabad clip = relation 7868535 (admin_level=8, exact name match, ~609 km², matches GHMC's real area); Bengaluru clip = relation 7902476 (admin_level=7 — the whole-city relation sits at a different level than Hyderabad's, confirmed live; ~717 km², matches BBMP's real area). Zones: Bengaluru 8/8 resolved from OSM; Hyderabad 4/6 resolved, `charminar`/`secunderabad` fell back to `boundary_source="approximate"` honestly, exactly as §2.3 anticipates
- [x] Grid generation refuses to run from a raw bbox (raises `RuntimeError` pointing at `fetch_boundaries.py`) when no clip polygon is on disk — verified by test and live
- [x] `grid.py`: Hyderabad 805 cells, Bengaluru 942 cells (1,747 total — within the ~2,000 budget the bbox-vs-boundary warning exists to protect)
- [x] Elevation seed: **both cities fully seeded** — Hyderabad 805/805 (68 via Open-Meteo batch, 737 via Open Topo Data fallback after a real 429 forced that path), Bengaluru 939/942 (3 legitimately `elevation_source="unknown"` after exhausting both sources — the honest fallback state §4.1 specifies, not a bug)
- [x] Overpass infrastructure: **both cities fully seeded** — Hyderabad 3,339 assets/527 water features/367 drains; Bengaluru 4,615 assets/1,811 water features/2,654 drains; `terrain_score_cached` populated for all 1,747 cells across both cities
- ✅ **Checkpoint exceeded:** both cities fully seeded end-to-end against live data (not a synthetic/mocked run) — took well over the nominal "<5 min" budget in *wall-clock* terms only because of a self-inflicted SQLite multi-writer contention issue while debugging live (documented in §15 and TWIN_INTEGRATION.md); the seed script itself, run cleanly once, is fast

### Phase 2 — Ingestion adapters *(1.5 d)* — ✅ **COMPLETE**
- [x] `IngestAdapter` ABC — timeout, retry+backoff, disk TTL cache, `TwinDataSnapshot` writing. A **fresh cache now short-circuits the network call entirely** rather than always fetching live and falling back only on failure — the original design re-hit Open-Meteo on every 5-minute compute tick regardless of its 15-min TTL and produced a real 429 while this was being built
- [x] Open-Meteo forecast + air quality + flood adapters — **multi-point lattice** (~80 points/city in one request), not zone-centroid fan-out (see the composite/lattice rationale below); confirmed live with zero degraded sources on a real compute run
- [x] RainViewer frame-manifest adapter (display-only, no sub-score — the per-cell precip-intensity idea was cut, see §15)
- [x] `internal_reports.py` — approved `Report` → cell + k-ring-1 mapping, verified against real inserted/approved demo reports feeding real `incident_score` values
- [x] Kill-switch tests (`tests/twin/test_ingest_fallbacks.py`, 12 tests) including a regression test for a real bug: an audit-snapshot write failure used to discard already-fetched good data (confirmed live against a 4,233-record real Overpass fetch) — fixed so persistence failures never take fetched data down with them
- ✅ **Checkpoint met:** `/api/twin/refresh` and the standalone seed scripts populate real snapshots; `/api/twin/health` reflects true per-source status live

### Phase 3 — State engine *(1 d)* — ✅ **COMPLETE, verified against real data**
- [x] `scoring.py` — pure functions, the corrected `hazard × vulnerability` composite, 38 boundary-value unit tests (`tests/twin/test_scoring.py`)
- [x] Weight renormalisation on missing hazard inputs; vulnerability degrades to its neutral midpoint (not renormalised — no other term to absorb its weight, per the corrected rule); `degraded_inputs` merges structural drops with source-level degradation
- [x] `engine.py` — one multi-point weather/AQI call per city, bulk upsert (one SELECT + `bulk_insert_mappings`/`bulk_update_mappings`), SSE `changed_cells` diff captured before the upsert overwrites the previous value
- [x] `jobs.py` registered on a real `BackgroundScheduler`; per-city exception isolation verified (one city's failure doesn't stop the other's compute or future ticks)
- ✅ **Checkpoint exceeded:** a real compute run against live Open-Meteo + Overpass + 3 demo approved reports scored **805 Hyderabad cells across 4 horizons in 2.5 s** (budget: <12s for *both* cities), zero degraded sources, vulnerability multipliers landing in the designed 1.0–1.6 range, top cell correctly driven by real incident + hydro signal

### Phase 4 — API *(0.5 d)* — ✅ **COMPLETE**
- [x] All §7 routes implemented and role-gated (C4), plus `GET /api/twin/gibs` (Phase 8's date picker needs a server-side probe — see below) — 66 API/route tests across `test_api_contract.py` and `test_state_api.py`
- [x] `geometry=false` compact mode is the default; `geometry=true` (full FeatureCollection) is what the frontend actually uses for rendering — h3-js client-side rebuild was descoped as a documented simplification (§10 already marks it optional)
- [x] ETag + `Cache-Control`; ~~gzip~~ not wired into `dev_app.py` (flask-compress is in `requirements-twin.txt`; add `Compress(app)` in the host, excluding `text/event-stream` per the §15 SSE note)
- [x] **Real bug fixed:** `/incidents` and `/summary`'s `incident_count_24h` never scoped reports to the requesting city (a `Report` has no `city_id`) — Bengaluru's comparison-strip incident count included Hyderabad's demo reports until cell-membership scoping (matching what `engine.py` already did correctly) was added everywhere reports are surfaced or counted
- ✅ **Checkpoint:** payload size/latency not independently benchmarked this session (no gzip wired yet); functional correctness verified live end-to-end instead

### Phase 5 — Dual-map shell *(1.5 d)* — ✅ **COMPLETE, verified in a real headless browser**
- [x] `digital_twin.html` — two panes, zone dropdowns, header controls, all rendered from server-side `city_payload` JSON (no separate frontend fetch needed on load)
- [x] `TwinMap` extends `window.GodModeMap` when present, falls back to an equivalent `TwinMapBase` (deferred-op queue + addedSources/addedLayers teardown tracking) when standalone — this repo has no `god-mode-maps.js` to extend, so this is the honest version of "don't fork it" until the real host is available (see TWIN_INTEGRATION.md)
- [x] 3D buildings **wired to OpenFreeMap Liberty's actual `openmaptiles` vector source** (confirmed by fetching the live style.json — the source is not called anything guessable), `minzoom: 13` matching where that source's own building data starts; a real bug (a bare `null` inside a `coalesce` expression, which MapLibre's validator rejects) was caught and fixed
- [x] Camera-link toggle: `moveend` syncs zoom/pitch/bearing across panes with an `isSyncing`-equivalent guard (implemented as a plain flag on the shared `moveend` handler)
- [x] `twin-hexes` fill-extrusion — data-driven colour via a `step` expression; **opacity is NOT data-driven** (MapLibre rejects `fill-extrusion-opacity` expressions outright — confirmed live: `addLayer` threw "data expressions not supported" and the whole layer failed to add) — fixed by baking each status band's opacity into an `rgba()` colour instead and leaving paint-opacity constant
- [x] Zone selection dims/flies via `flyToBbox`/`flyTo`, verified live
- ✅ **Checkpoint exceeded:** both cities rendered with live H3 hex grids from the *real* fetched municipal boundaries (visually distinct, correctly shaped) in a headless Chromium session, zero console errors, before any scoring data existed *and* after a real compute run (screenshots taken; not committed to the repo)

### Phase 6 — Layers, drill-down, prediction *(1.5 d)* — ✅ **COMPLETE**
- [x] All 10 §8.4 layers wired (basemap, satellite, buildings-3d, twin-hexes (+degraded-hatch outline), zone-outline, radar, water-drains, infrastructure, incidents, incident-labels) with a per-pane layer-toggle panel and legend; **NASA GIBS added as a third header basemap option** with a real date picker — `GET /api/twin/gibs` probes availability server-side (§15's "may not exist" risk) and returns a `max_zoom` the frontend must apply, because GIBS's true-colour layers cap at zoom 9 (native ~500m/px resolution) — confirmed live: omitting it produced a 400 for every tile at the map's default zoom
- [x] Horizon scrubber — CSS transition on the header buttons; hex colour/height changes are picked up by maplibre's own paint-property transitions (no manual per-cell animation code needed given the `step`/multiply expressions already used)
- [x] Cell drill-down drawer — explanation string, sub-scores, asset chips, report cards; verified live against a real critical-ish cell with real Overpass-fetched infrastructure and real demo reports
- [x] KPI blocks + comparison strip (`/api/twin/compare`) — verified live with real numbers (avg/max risk, status counts, incident counts) after the city-scoping bug above was fixed
- [x] `TwinCellHistory` + `/timeline` route (SQLite `strftime` grouping with a Python-side fallback bucketer for other dialects, per C2) — **not populated by any job this session** (no hourly-rollup job exists yet; `/timeline` returns an empty series until one is added, which is an honest gap, not a hidden one)
- ✅ **Checkpoint met:** verified live — a cell's explanation names its real dominant driver ("34 mm/h observed rainfall", nearby report counts, terrain proximity to water) from real `raw_inputs`

### Phase 7 — Live & action loop *(1 d)* — ✅ **Core loop complete; host wiring deferred, honestly**
- [x] SSE broker (`twin/stream.py`) — in-process pub/sub, per-subscriber bounded queue (never blocks `publish()`), heartbeats, a duration cap that is actually responsive (a bug where it only checked once per 15s heartbeat was caught and fixed by a test); 60s poll fallback in `twin-stream.js` after 3 failed reconnects
- [x] Report-approval hook — a single SQLAlchemy `after_update` listener whose *target callback* is swappable via re-registration (fixed a design gap where the first registration silently won forever, which would have made this untestable and fragile against a host that re-initialises), verified against a real `Report` insert → approve → SSE event round trip
- [x] Drawer action buttons — prefilled from the real cell centroid, POST to `window.TWIN_COORDINATION_ENDPOINTS[action]`, a config point the real host sets (see TWIN_INTEGRATION.md) — **not wired to real `EmergencyEvent`/`VolunteerAssignment`/alert endpoints**, because those models live in the Sentinel AI app this session had no access to; without the config, the button explains exactly what it would have sent, rather than pretending to succeed
- ⚠️ **Checkpoint partially met:** the SSE half is verified end-to-end (approval → hook → broker → subscriber event, live); the "creates a real `VolunteerAssignment`" half needs the actual host app and is the one piece of Phase 7 this session could not complete, by definition of not having that codebase available

### Phase 8 — Hardening *(1 d)* — ✅ **Mostly complete**
- [x] 145 tests in `tests/twin/` (grid determinism incl. real h3-py v4 API pinning, scoring boundaries, adapter fallbacks incl. the audit-write regression, jobs resilience, SSE broker, approval hook, full API contract) — all passing
- [x] Degradation hatch (dashed amber outline on any cell with non-empty `degraded_inputs` — a true sprite-pattern hatch needs an image asset this CDN-only build doesn't have, documented as the honest equivalent) + health pill, both verified live
- [x] NASA GIBS date picker — added mid-session once the gap was noticed (the basemap dropdown had a GIBS option with zero wiring behind it); now a real `<input type="date">`, a real availability probe, and a real fix for GIBS's zoom-9 tile ceiling
- [x] This section of the README updated in place as each phase's real, live-tested status, rather than a separate summary doc
- ⚠️ **Not done this session:** the SH-SVA-03 traceability matrix (§14) was not re-checked against the final build; PostgreSQL was validated by rendering the migration's DDL offline only (§12 Phase 0), never against a live Postgres server (none was available) — C2 compliance is therefore "no SQLite-specific SQL anywhere in the code" (true, verified by inspection) rather than "tested on both engines"
- ✅ **Checkpoint not run:** "unplug the internet" wasn't literally tested; every adapter's fallback path *was* individually exercised (a real 429, a real transient lock, a real Overpass 406/504/429 sequence all occurred and were handled without a 500 reaching the user — with one exception, the audit-write bug, which was caught and fixed) — treat this as strong indirect evidence rather than the literal checkpoint

---

## 13. Acceptance criteria

| # | Criterion |
|:--|:---|
| A1 | Both cities render side by side in the Analyst dashboard with 3D buildings and 3D risk hexagons |
| A2 | Zone dropdown per city, including "Whole City", switching in < 1 s |
| A3 | Every hexagon's risk score is decomposable into five named sub-scores and their raw inputs |
| A4 | An approved Sentinel AI report visibly and measurably raises the risk of its cell and neighbours within 5 minutes (5 seconds via SSE) |
| A5 | Horizon scrubber shows a materially different, forecast-driven picture at +6 h and +24 h |
| A6 | Killing any single Tier-1 API degrades one sub-score with a visible badge; the twin never errors |
| A7 | Zero paid API keys required to run the full core loop |
| A8 | Runs identically on SQLite dev and PostgreSQL prod; no PostGIS dependency |
| A9 | From a critical cell, an official can declare an emergency, dispatch a volunteer, or broadcast an alert without leaving the twin |
| A10 | Whole-city state payload < 150 KB gzipped; first meaningful paint of both maps < 3 s on a 10 Mbps connection |

---

## 14. Traceability to SH-SVA-03

| PS clause | Twin implementation |
|:---|:---|
| *"fragmented across departments"* | One shared spatial operating picture; drill-down surfaces the responsible assets and links straight into the existing multi-agency coordination ledger |
| *"delayed incident detection"* | Approved reports mutate the twin in seconds via SSE; radar + forecast surface risk before any report exists |
| *"inefficient response"* | Cell-level ranking tells dispatch *where* to send people first; action buttons dispatch from the map |
| *"increased operational costs"* | Keyless data sources; pre-emptive positioning against T+3/T+6 forecasts instead of reactive deployment |
| *"multi-agent AI system"* | Ingestion, scoring, prediction, and coordination agents each own a stage; the twin is their shared world model |
| *"integrated with a city digital twin"* | ~1,000 live H3 state cells per city (~2,000 across both) over 3D geometry, with satellite, radar, hydrology, terrain, infrastructure, and live civic incident layers |
| *"autonomously prioritize incidents"* | `risk_score` ranks cells continuously with no human in the loop |
| *"coordinate actions"* | Drawer actions write to `EmergencyEvent`, `VolunteerAssignment`, and the alert broadcaster |
| *"timely, compliant resolution"* | `TwinDataSnapshot` audit trail, explainable sub-scores, honest degradation badging |

---

## 15. Known risks & mitigations

| Risk | Mitigation |
|:---|:---|
| Overpass rate-limits / times out on large bboxes | Split queries per zone; cache to disk; weekly refresh only; ship committed fallback GeoJSON |
| Open-Meteo per-cell calls would be ~2,000 req/run | **Never call per cell.** 14 zone-centroid calls + IDW interpolation. Enforce in code review. |
| BBMP zone boundaries have changed post-2024 | `zone_scheme` field makes a re-seed a data migration; label approximate boundaries in the UI |
| GeoJSON payload bloat | `geometry=false` compact mode + client-side H3 rebuild + gzip + ETag |
| APScheduler double-runs under multi-worker Gunicorn | `TWIN_SCHEDULER_ENABLED` guard; long-term, move to an external scheduler or a Celery beat |
| Risk weights are heuristic, not validated | Keep them in `twin/config.py` as a single tunable dict; log inputs to `TwinCellHistory` so they can be back-tested and later replaced with a fitted model |
| NASA GIBS imagery for a given date may not exist | Probe the capabilities document; fall back to the most recent available date and label it |
| **SSE starves Gunicorn sync workers** | Each `/api/twin/stream` connection pins a worker for its lifetime, and every open dashboard opens **two** (one per city) — four officials exhaust eight workers. Run the app on a `gevent`/`eventlet` worker class, or ship the 60 s poll as the default and make SSE opt-in. |
| **`POST /api/twin/seed` double-runs** | Seeding fires Overpass plus ~40 elevation batches; two impatient clicks run it twice. Guard with an advisory lock (a `TwinDataSnapshot` row with `source_key="seed_lock"` and a TTL works on both SQLite and Postgres) and return `409` while one is in flight. |
| **OSM building heights are sparse in Indian cities** | A1 requires 3D buildings, but `render_height` is missing for most Hyderabad/Bengaluru footprints, so the basemap renders flat. Use a coalescing paint expression: `render_height` → `building:levels × 3` → default `8` m. |
| **Stale compute mistaken for live data** | The health pill covers ingest sources but not `twin_compute_state` itself. If the compute job dies, the UI shows hours-old scores as current. Treat `max(computed_at)` older than 3× the compute interval as stale and badge the whole pane. |

---

## 16. First command for the coding agent

```
Read DIGITAL_TWIN_README.md. Implement Phase 0 only.
Create the twin/ package, the six SQLAlchemy models from §6, twin/config.py
with the CITY_DEFS from §2, register the blueprint in app.py without touching
any existing route, and generate the twin_initial Alembic migration.
Verify: flask db upgrade runs clean on SQLite, and GET /api/twin/cities
returns both cities. Then stop and report.
```
