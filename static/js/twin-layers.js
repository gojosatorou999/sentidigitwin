/**
 * Layer definitions + paint expressions (README section 8.4).
 *
 * Pure data + small factory functions -- no map instance is touched here.
 * digital-twin.js calls these to add/update layers on a maplibregl.Map.
 *
 * The palette and the chrome deliberately match Sentinel AI's own maps (the
 * Telangana/Bengaluru flood widgets in analyst_dashboard.html): Esri World
 * Imagery under an Esri boundaries-and-places reference layer, cyan for
 * water, and the same amber/orange/red hazard ramp the rest of the app uses
 * for priority. The twin is a panel inside those dashboards, so it should
 * not read as a different product.
 */

(function (global) {
  "use strict";

  // Status band colours/opacity mirror twin/config.py STATUS_BANDS.
  // Keep the two in sync by hand -- there is no shared source of truth
  // across the Python/JS boundary for this project's scope.
  //
  // `opacity` is markedly lower than the Python table's, and that is not a
  // drift: the Python values were tuned against a flat dark vector basemap,
  // while these are composited over satellite imagery. At the old 0.35-0.80
  // the hexes painted the city out completely -- the operator lost the very
  // ground truth the imagery was added to provide.
  const STATUS_BANDS = [
    { max: 25, status: "normal", colour: "#22c55e", opacity: 0.14, height: 0 },
    { max: 50, status: "watch", colour: "#eab308", opacity: 0.3, height: 60 },
    { max: 75, status: "warning", colour: "#f97316", opacity: 0.42, height: 150 },
    { max: 101, status: "critical", colour: "#ef4444", opacity: 0.55, height: 330 },
  ];

  function hexToRgba(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }

  // fill-extrusion-opacity does NOT support data-driven (per-feature)
  // expressions in MapLibre GL -- confirmed live: addLayer throws
  // "data expressions not supported" and the whole layer fails to add.
  // fill-extrusion-color DOES support them, so the per-band opacity is
  // baked directly into an rgba() colour instead, and paint opacity stays
  // a plain constant (see twinHexesLayer below).
  function riskColourExpression() {
    return [
      "step",
      ["coalesce", ["get", "risk_score"], 0],
      hexToRgba(STATUS_BANDS[0].colour, STATUS_BANDS[0].opacity),
      25, hexToRgba(STATUS_BANDS[1].colour, STATUS_BANDS[1].opacity),
      50, hexToRgba(STATUS_BANDS[2].colour, STATUS_BANDS[2].opacity),
      75, hexToRgba(STATUS_BANDS[3].colour, STATUS_BANDS[3].opacity),
    ];
  }

  /** Extrusion height in METRES, interpolated, not stepped.
   *
   * The previous expression was `risk * heightFactor(status)`, which put a
   * risk-100 cell 1,400 m in the air -- roughly three Burj Khalifas -- above
   * a city whose tallest building is under 150 m. At that scale the hexes
   * stop being a data layer and become a wall: they occlude each other, they
   * occlude the imagery, and the tilt that makes a 3D map readable is what
   * makes it worst. Capping at 330 m keeps the tallest risk column clearly
   * above the skyline (so severity still reads instantly from an oblique
   * angle) while leaving the ground visible between columns.
   *
   * Interpolating rather than stepping also removes the visual cliff where
   * a cell at 49.9 and one at 50.1 differed by 200 m of height for a
   * difference the operator cannot act on.
   */
  function riskHeightExpression() {
    return [
      "interpolate", ["linear"], ["coalesce", ["get", "risk_score"], 0],
      0, 0,
      25, STATUS_BANDS[1].height,
      50, STATUS_BANDS[2].height,
      75, STATUS_BANDS[3].height,
      100, 420,
    ];
  }

  const PRIORITY_COLOURS = {
    critical: "#ef4444",
    high: "#f97316",
    medium: "#eab308",
    low: "#22c55e",
  };

  function incidentColourExpression() {
    return [
      "match", ["get", "priority"],
      "critical", PRIORITY_COLOURS.critical,
      "high", PRIORITY_COLOURS.high,
      "medium", PRIORITY_COLOURS.medium,
      "low", PRIORITY_COLOURS.low,
      "#94a3b8",
    ];
  }

  const ASSET_ICON_COLOUR = {
    hospital: "#ef4444",
    fire_station: "#f97316",
    police: "#3b82f6",
    power_substation: "#eab308",
    water_works: "#06b6d4",
    school: "#8b5cf6",
    transport_hub: "#64748b",
    shelter: "#22c55e",
  };

  //: OSINT camera kinds from twin/ingest/cctv.py, colour-coded.
  const CCTV_KIND_COLOUR = {
    traffic: "#38bdf8",
    public: "#a78bfa",
    outdoor: "#22d3ee",
    indoor: "#94a3b8",
    private: "#64748b",
    unknown: "#7dd3fc",
  };

  /** The layer stack, bottom to top (section 8.4 table). Each entry is a
   * declarative spec the controller turns into maplibre add{Source,Layer}
   * calls; `defaultOn` matches the toggle-default column.
   *
   * Two defaults changed when the console moved into the dashboards:
   * `satellite` is now on (it is the basemap the rest of Sentinel's maps
   * use), and `incident-labels` is off -- a hazard-type label under every
   * incident dot was the single densest thing on the canvas, and the dot's
   * colour already carries the priority. */
  const LAYER_STACK = [
    { id: "basemap", kind: "style", defaultOn: true },
    { id: "satellite", kind: "raster", defaultOn: true },
    { id: "buildings-3d", kind: "fill-extrusion", defaultOn: true },
    // Official alert zones sit under the risk grid: they are context an
    // authority published, not the twin's own measurement of the ground.
    { id: "alert-zones", kind: "fill", defaultOn: true },
    { id: "alert-zones-outline", kind: "line", defaultOn: true },
    { id: "twin-hexes", kind: "fill-extrusion", defaultOn: true },
    { id: "zone-outline", kind: "line", defaultOn: true },
    { id: "radar", kind: "raster", defaultOn: false },
    { id: "water-bodies", kind: "fill", defaultOn: false },
    { id: "water-bodies-outline", kind: "line", defaultOn: false },
    { id: "water-drains-glow", kind: "line", defaultOn: false },
    { id: "water-drains", kind: "line", defaultOn: false },
    { id: "infrastructure", kind: "circle", defaultOn: false },
    { id: "cctv-cone", kind: "fill", defaultOn: false, minzoom: 15 },
    { id: "cctv", kind: "circle", defaultOn: false },
    { id: "cctv-direction", kind: "symbol", defaultOn: false, minzoom: 14 },
    { id: "cctv-streams", kind: "circle", defaultOn: true },
    { id: "transit-stalled-halo", kind: "circle", defaultOn: false },
    { id: "transit-vehicles", kind: "circle", defaultOn: false },
    { id: "air-stations", kind: "circle", defaultOn: false },
    { id: "air-station-labels", kind: "symbol", defaultOn: false, minzoom: 11 },
    { id: "incidents", kind: "circle", defaultOn: true },
    { id: "incident-labels", kind: "symbol", defaultOn: false, minzoom: 12 },
    // Flags are the top of the stack: the whole point of one is that it is
    // the thing an analyst should look at first.
    { id: "flag-glow", kind: "line", defaultOn: true },
    { id: "flag-areas", kind: "line", defaultOn: true },
  ];

  function twinHexesLayer(sourceId) {
    return {
      id: "twin-hexes",
      type: "fill-extrusion",
      source: sourceId,
      paint: {
        "fill-extrusion-color": riskColourExpression(),
        // Constant, not data-driven -- opacity per band is baked into the
        // rgba() colour above instead (MapLibre limitation, see above).
        "fill-extrusion-opacity": 1,
        "fill-extrusion-height": riskHeightExpression(),
        "fill-extrusion-base": 0,
        "fill-extrusion-vertical-gradient": true,
      },
      metadata: { twinAnimatable: ["fill-extrusion-color", "fill-extrusion-height"] },
    };
  }

  /** A hairline on the top face of every cell worth acting on.
   *
   * With the extrusion opacity dropped far enough to keep the imagery
   * readable, adjacent cells in the same band stopped being separable -- a
   * run of `watch` cells read as one amber smear. A 1px outline at the same
   * colour restores the grid without adding any fill.
   *
   * The `>= 25` filter is the single biggest de-cluttering decision in the
   * layer stack, and it is on the outline rather than the fill for a
   * reason. On an ordinary day the great majority of a city sits in
   * `normal`, so outlining every cell drew roughly nine hundred hairlines
   * across the imagery -- the honeycomb that made the console look like a
   * dataset rather than a city. Dropping only the outline leaves those
   * cells as a faint green wash that still says "measured, and fine",
   * instead of hiding them and leaving an operator on a calm day looking at
   * an empty map wondering whether the twin is running. */
  function twinHexOutlineLayer(sourceId) {
    return {
      id: "twin-hex-outline",
      type: "line",
      source: sourceId,
      filter: [">=", ["coalesce", ["get", "risk_score"], 0], 25],
      paint: {
        "line-color": [
          "step", ["coalesce", ["get", "risk_score"], 0],
          "rgba(34,197,94,0.35)",
          25, "rgba(234,179,8,0.55)",
          50, "rgba(249,115,22,0.7)",
          75, "rgba(239,68,68,0.85)",
        ],
        "line-width": 1,
      },
    };
  }

  function twinHexesDegradedLayer(sourceId) {
    // Section 8.7: "officials must be able to see when the twin is
    // guessing". A true diagonal-hatch fill needs a sprite pattern image
    // (fill-extrusion-pattern), which is out of scope for a CDN-only,
    // no-build-step app -- a dashed amber outline on exactly the cells
    // whose degraded_inputs is non-empty is the honest, dependency-free
    // equivalent, and reuses the same hex source so it never drifts out of
    // sync with the fill layer above it.
    return {
      id: "twin-hexes-degraded",
      type: "line",
      source: sourceId,
      filter: [">", ["length", ["coalesce", ["get", "degraded_inputs"], ["literal", []]]], 0],
      paint: {
        "line-color": "#facc15",
        "line-width": 1.6,
        "line-dasharray": [2, 1.5],
        "line-opacity": 0.85,
      },
    };
  }

  function zoneOutlineLayer(sourceId) {
    // Dimmed from the original near-white 1.5px: over satellite imagery a
    // bright administrative boundary competed with the risk grid it was
    // only ever meant to provide context for.
    return {
      id: "zone-outline",
      type: "line",
      source: sourceId,
      paint: {
        "line-color": "#cbd5e1",
        "line-width": 1,
        "line-opacity": 0.4,
      },
    };
  }

  function incidentsLayer(sourceId) {
    return {
      id: "incidents",
      type: "circle",
      source: sourceId,
      paint: {
        "circle-radius": ["match", ["get", "priority"], "critical", 8, "high", 7, "medium", 6, 5],
        "circle-color": incidentColourExpression(),
        "circle-stroke-width": 1.5,
        "circle-stroke-color": "#0b1220",
        "circle-opacity": 0.92,
      },
    };
  }

  function incidentLabelsLayer(sourceId) {
    return {
      id: "incident-labels",
      type: "symbol",
      source: sourceId,
      minzoom: 12,
      layout: {
        "text-field": ["get", "hazard_type"],
        "text-size": 11,
        "text-offset": [0, 1.4],
        "text-anchor": "top",
        "text-allow-overlap": false,
        "visibility": "none",
      },
      paint: {
        "text-color": "#e2e8f0",
        "text-halo-color": "#0b1220",
        "text-halo-width": 1,
      },
    };
  }

  function infrastructureLayer(sourceId) {
    return {
      id: "infrastructure",
      type: "circle",
      source: sourceId,
      layout: { visibility: "none" },
      paint: {
        "circle-radius": 5,
        "circle-color": [
          "match", ["get", "asset_type"],
          "hospital", ASSET_ICON_COLOUR.hospital,
          "fire_station", ASSET_ICON_COLOUR.fire_station,
          "police", ASSET_ICON_COLOUR.police,
          "power_substation", ASSET_ICON_COLOUR.power_substation,
          "water_works", ASSET_ICON_COLOUR.water_works,
          "school", ASSET_ICON_COLOUR.school,
          "transport_hub", ASSET_ICON_COLOUR.transport_hub,
          "shelter", ASSET_ICON_COLOUR.shelter,
          "#94a3b8",
        ],
        "circle-stroke-width": 1,
        "circle-stroke-color": "#0b1220",
      },
    };
  }

  /** Lakes, tanks and reservoirs, as a fill.
   *
   * Bengaluru floods through its lake chain and Hyderabad through its
   * tanks, so the water *bodies* matter as much as the channels between
   * them. Filtered to polygons because the same source carries both, and a
   * fill on a LineString renders nothing while still costing a draw call. */
  function waterBodiesLayer(sourceId) {
    return {
      id: "water-bodies",
      type: "fill",
      source: sourceId,
      filter: ["==", ["geometry-type"], "Polygon"],
      layout: { visibility: "none" },
      paint: { "fill-color": "#0ea5e9", "fill-opacity": 0.35 },
    };
  }

  function waterBodiesOutlineLayer(sourceId) {
    return {
      id: "water-bodies-outline",
      type: "line",
      source: sourceId,
      filter: ["==", ["geometry-type"], "Polygon"],
      layout: { visibility: "none" },
      paint: { "line-color": "#7dd3fc", "line-width": 1, "line-opacity": 0.7 },
    };
  }

  /** Drains, canals, streams and rivers.
   *
   * Cyan over a blurred wider stroke, which is exactly how the Telangana and
   * Bengaluru flood widgets on the analyst dashboard draw their rivers
   * (#38d9ff over a blurred #00cfff) -- the twin sits on the same page, so
   * the same waterway should not be a different colour in each panel.
   * Width is interpolated by zoom and by kind: a storm drain and the
   * Musi drawn at one width is a map that hides the hierarchy an operator
   * routes around. */
  function waterDrainsLayer(sourceId) {
    return {
      id: "water-drains",
      type: "line",
      source: sourceId,
      filter: ["==", ["geometry-type"], "LineString"],
      layout: { visibility: "none", "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": [
          "match", ["get", "kind"],
          "drain", "#38bdf8",
          "canal", "#22d3ee",
          "#38d9ff",
        ],
        "line-width": [
          "interpolate", ["linear"], ["zoom"],
          10, ["match", ["get", "kind"], "river", 2, "canal", 1.4, 0.8],
          16, ["match", ["get", "kind"], "river", 6, "canal", 4, 2.2],
        ],
        "line-opacity": 0.8,
      },
    };
  }

  function waterDrainsGlowLayer(sourceId) {
    return {
      id: "water-drains-glow",
      type: "line",
      source: sourceId,
      filter: ["==", ["geometry-type"], "LineString"],
      layout: { visibility: "none" },
      paint: {
        "line-color": "#00cfff",
        "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4, 16, 12],
        "line-opacity": 0.22, "line-blur": 5,
      },
    };
  }

  // ------------------------------------------------------------------
  // OSINT surveillance cameras (twin/ingest/cctv.py)
  // ------------------------------------------------------------------

  function cctvColourExpression() {
    return [
      "match", ["get", "kind"],
      "traffic", CCTV_KIND_COLOUR.traffic,
      "public", CCTV_KIND_COLOUR.public,
      "outdoor", CCTV_KIND_COLOUR.outdoor,
      "indoor", CCTV_KIND_COLOUR.indoor,
      "private", CCTV_KIND_COLOUR.private,
      CCTV_KIND_COLOUR.unknown,
    ];
  }

  function cctvLayer(sourceId) {
    return {
      id: "cctv",
      type: "circle",
      source: sourceId,
      layout: { visibility: "none" },
      paint: {
        // Cameras cluster hard along arterial roads; at city zoom a fixed
        // radius turns a junction into one indistinct blob, so the dot
        // grows with zoom instead of dominating the overview.
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 14, 4.5, 17, 7],
        "circle-color": cctvColourExpression(),
        "circle-stroke-width": 1,
        "circle-stroke-color": "rgba(2,6,16,0.85)",
        "circle-opacity": 0.9,
      },
    };
  }

  /** The direction a camera faces, as a rotated glyph.
   *
   * A real view cone would need a polygon per camera computed client-side,
   * and with a few thousand cameras in a city bbox that is both slow and --
   * far worse here -- visually solid. A single arrowhead rotated to
   * `camera:direction` says the same thing in one glyph, needs no sprite
   * sheet (which a CDN-only build has no way to author), and disappears
   * below zoom 14 where it would only be noise.
   *
   * `text-rotation-alignment: map` is what makes the bearing meaningful:
   * without it the glyph rotates with the screen, not with north. */
  function cctvDirectionLayer(sourceId) {
    return {
      id: "cctv-direction",
      type: "symbol",
      source: sourceId,
      minzoom: 14,
      filter: ["has", "direction"],
      layout: {
        "text-field": "▲",
        "text-size": 10,
        "text-rotate": ["coalesce", ["get", "direction"], 0],
        "text-rotation-alignment": "map",
        "text-pitch-alignment": "map",
        "text-offset": [0, -0.9],
        "text-allow-overlap": true,
        "text-ignore-placement": true,
        "visibility": "none",
      },
      paint: {
        "text-color": cctvColourExpression(),
        "text-halo-color": "rgba(2,6,16,0.9)",
        "text-halo-width": 1,
        "text-opacity": 0.85,
      },
    };
  }

  // ------------------------------------------------------------------
  // Camera view cones (built client-side from the /cameras payload)
  // ------------------------------------------------------------------

  /** Field of view and range per camera type.
   *
   * OSM almost never tags either, so these are declared *assumptions* and the
   * legend says so. Mirrors twin/cameras.py::CAMERA_OPTICS -- if you change
   * one, change both, or the map and the API will disagree about what a
   * camera can see. */
  const CAMERA_OPTICS = {
    fixed: { fov: 60, range: 45 },
    panning: { fov: 180, range: 60 },   // a PTZ sweeps: draw the whole envelope
    dome: { fov: 360, range: 30 },
    default: { fov: 60, range: 45 },
  };

  /** Forward geodesic point. Accurate to well under a metre at these ranges. */
  function destination(lat, lon, bearingDeg, distanceM) {
    const R = 6371000;
    const d = distanceM / R;
    const br = (bearingDeg * Math.PI) / 180;
    const p1 = (lat * Math.PI) / 180;
    const l1 = (lon * Math.PI) / 180;
    const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(br));
    const l2 = l1 + Math.atan2(Math.sin(br) * Math.sin(d) * Math.cos(p1),
                               Math.cos(d) - Math.sin(p1) * Math.sin(p2));
    return [(l2 * 180) / Math.PI, (p2 * 180) / Math.PI];
  }

  /** A wedge polygon for one camera, or null when it cannot be drawn.
   *
   * Null in two cases, both deliberate: a camera with no mapped bearing (the
   * majority) must render as a plain dot rather than a north-facing cone that
   * claims knowledge nobody has, and a 360-degree dome has no direction to
   * draw. */
  function viewCone(camera) {
    if (camera.direction === null || camera.direction === undefined) return null;
    const optics = CAMERA_OPTICS[camera.camera_type] || CAMERA_OPTICS.default;
    if (optics.fov >= 360) return null;

    const start = camera.direction - optics.fov / 2;
    const ring = [[camera.lon, camera.lat]];
    for (let i = 0; i <= 12; i++) {
      ring.push(destination(camera.lat, camera.lon, start + (optics.fov * i) / 12, optics.range));
    }
    ring.push([camera.lon, camera.lat]);
    return {
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [ring] },
      properties: {
        kind: camera.kind, osm_id: camera.osm_id, operator: camera.operator,
        fov: optics.fov, range_m: optics.range, estimated: true,
      },
    };
  }

  /** Cones for a whole camera FeatureCollection, built in the browser.
   *
   * Never sent over the wire: a cone is ~14 coordinate pairs against a
   * camera's one, and Bengaluru's camera payload is already 765 KB raw. */
  function conesFrom(featureCollection) {
    const features = [];
    ((featureCollection && featureCollection.features) || []).forEach((feature) => {
      const coords = (feature.geometry && feature.geometry.coordinates) || [];
      if (coords.length < 2) return;
      const cone = viewCone(Object.assign({}, feature.properties,
                                          { lon: coords[0], lat: coords[1] }));
      if (cone) features.push(cone);
    });
    return { type: "FeatureCollection", features };
  }

  function cctvConeLayer(sourceId) {
    return {
      id: "cctv-cone",
      type: "fill",
      source: sourceId,
      // Below z15 a few thousand wedges is a solid smear that hides the city.
      minzoom: 15,
      layout: { visibility: "none" },
      paint: {
        "fill-color": cctvColourExpression(),
        "fill-opacity": 0.18,
        "fill-outline-color": "rgba(56,189,248,0.55)",
      },
    };
  }

  // ------------------------------------------------------------------
  // Operator-supplied live camera streams (twin/cameras.py)
  // ------------------------------------------------------------------

  /** Deliberately a different shape and colour from the OSINT dots: one is a
   * camera somebody mapped, the other is a camera you can actually watch. */
  function cctvStreamLayer(sourceId) {
    return {
      id: "cctv-streams",
      type: "circle",
      source: sourceId,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 5, 14, 8, 17, 12],
        "circle-color": "#f43f5e",
        "circle-stroke-width": 2.5,
        "circle-stroke-color": "#fff1f2",
        "circle-opacity": 0.95,
      },
    };
  }

  // ------------------------------------------------------------------
  // Official alert zones (twin/alerts.py -> /api/twin/<city>/alerts)
  // ------------------------------------------------------------------

  /** CAP severity, not risk score. An alert is an authority's statement, so
   * it keeps the authority's own vocabulary rather than being folded into the
   * twin's bands -- an operator must be able to see "IMD said Severe". */
  const CAP_SEVERITY_COLOUR = {
    Extreme: "#dc2626",
    Severe: "#ea580c",
    Moderate: "#d97706",
    Minor: "#0891b2",
    Unknown: "#64748b",
  };

  function alertColourExpression() {
    return [
      "match", ["coalesce", ["get", "severity"], "Unknown"],
      "Extreme", CAP_SEVERITY_COLOUR.Extreme,
      "Severe", CAP_SEVERITY_COLOUR.Severe,
      "Moderate", CAP_SEVERITY_COLOUR.Moderate,
      "Minor", CAP_SEVERITY_COLOUR.Minor,
      CAP_SEVERITY_COLOUR.Unknown,
    ];
  }

  function alertZoneLayer(sourceId) {
    return {
      id: "alert-zones",
      type: "fill",
      source: sourceId,
      paint: {
        // Kept low: this sits *under* the risk grid and must never be the
        // reason a critical cell is hard to read.
        "fill-color": alertColourExpression(),
        "fill-opacity": 0.14,
      },
    };
  }

  function alertZoneOutlineLayer(sourceId) {
    return {
      id: "alert-zones-outline",
      type: "line",
      source: sourceId,
      paint: {
        "line-color": alertColourExpression(),
        "line-width": 2,
        "line-opacity": 0.85,
        // Dashed for a reason: a published CAP polygon is an authority's
        // stated area, not a measured one, and the dashes keep it visually
        // distinct from the twin's own solid geometry.
        "line-dasharray": [3, 1.5],
      },
    };
  }

  // ------------------------------------------------------------------
  // Live pollution stations (twin/live.py -> /live/air)
  // ------------------------------------------------------------------

  /** CPCB national AQI bands. These are the colours Indian officials, press
   * and the public already read, so the map uses them rather than inventing
   * a ramp. */
  const AQI_BANDS = [
    [50, "#22c55e"],    // Good
    [100, "#a3e635"],   // Satisfactory
    [200, "#facc15"],   // Moderate
    [300, "#fb923c"],   // Poor
    [400, "#ef4444"],   // Very Poor
    [10000, "#7f1d1d"], // Severe
  ];

  function aqiColourExpression() {
    const expression = ["step", ["coalesce", ["get", "value"], -1], "#475569"];
    AQI_BANDS.forEach(([ceiling, colour], index) => {
      // The first stop is the "no reading" colour above; each band starts
      // where the previous one ended.
      const floor = index === 0 ? 0 : AQI_BANDS[index - 1][0];
      expression.push(floor, colour);
    });
    return expression;
  }

  function airStationLayer(sourceId) {
    return {
      id: "air-stations",
      type: "circle",
      source: sourceId,
      layout: { visibility: "none" },
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 5, 13, 9, 16, 14],
        "circle-color": aqiColourExpression(),
        "circle-stroke-width": 2,
        // A stale station is outlined, not hidden: an instrument that stopped
        // reporting is itself information, and silently dropping it would let
        // an operator read a gap as clean air.
        "circle-stroke-color": [
          "case", ["==", ["get", "status"], "stale"], "#f59e0b", "rgba(2,6,16,0.75)",
        ],
        "circle-opacity": ["case", ["==", ["get", "status"], "stale"], 0.55, 0.92],
      },
    };
  }

  function airStationLabelLayer(sourceId) {
    return {
      id: "air-station-labels",
      type: "symbol",
      source: sourceId,
      minzoom: 11,
      layout: {
        "text-field": ["to-string", ["coalesce", ["get", "value"], "--"]],
        "text-size": 11,
        "text-font": ["Noto Sans Bold"],
        "text-allow-overlap": false,
        "visibility": "none",
      },
      paint: {
        "text-color": "#f8fafc",
        "text-halo-color": "rgba(2,6,16,0.9)",
        "text-halo-width": 1.4,
      },
    };
  }

  // ------------------------------------------------------------------
  // Live transit vehicles (twin/live.py -> /live/transit)
  // ------------------------------------------------------------------

  /** Stalled vehicles are the signal; moving ones are the control group.
   *
   * A bus fleet is the densest live road-usability sensor a city has, so the
   * layer is coloured by whether each vehicle is moving rather than by route:
   * twenty red dots on one arterial while the rest of the city runs green is
   * a flooded underpass, and that pattern must be legible at a glance. */
  function transitColourExpression() {
    return [
      "case",
      ["==", ["get", "status"], "stale"], "#ef4444",
      ["<=", ["coalesce", ["get", "value"], 0], 2], "#f97316",
      ["<=", ["coalesce", ["get", "value"], 0], 10], "#facc15",
      "#38bdf8",
    ];
  }

  function transitVehicleLayer(sourceId) {
    return {
      id: "transit-vehicles",
      type: "circle",
      source: sourceId,
      layout: { visibility: "none" },
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 2.5, 13, 4.5, 16, 7],
        "circle-color": transitColourExpression(),
        "circle-stroke-width": 0.8,
        "circle-stroke-color": "rgba(2,6,16,0.8)",
        "circle-opacity": 0.9,
      },
    };
  }

  /** A halo under stopped vehicles only, so a cluster of them reads as a
   * bright patch at city zoom without every moving bus adding glare. */
  function transitStalledHaloLayer(sourceId) {
    return {
      id: "transit-stalled-halo",
      type: "circle",
      source: sourceId,
      layout: { visibility: "none" },
      filter: ["any",
        ["==", ["get", "status"], "stale"],
        ["<=", ["coalesce", ["get", "value"], 99], 2]],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 7, 13, 12, 16, 18],
        "circle-color": "#f97316",
        "circle-opacity": 0.22,
        "circle-blur": 0.8,
      },
    };
  }

  // ------------------------------------------------------------------
  // Agent flags (twin/agent -> /api/twin/<city>/flags)
  // ------------------------------------------------------------------

  /** What the agent raised, awaiting an analyst.
   *
   * Drawn as an outline rather than a fill so it reads as an annotation on
   * the city rather than as another measurement of it -- and so the risk
   * grid, which is the measured thing, stays visible underneath. */
  function flagAreaLayer(sourceId) {
    return {
      id: "flag-areas",
      type: "line",
      source: sourceId,
      paint: {
        "line-color": [
          "case", ["==", ["get", "status"], "approved"], "#22d3ee", "#e879f9",
        ],
        "line-width": 2.5,
        "line-opacity": 0.9,
      },
    };
  }

  function flagGlowLayer(sourceId) {
    return {
      id: "flag-glow",
      type: "line",
      source: sourceId,
      paint: {
        "line-color": "#e879f9",
        "line-width": 10,
        "line-blur": 8,
        "line-opacity": 0.28,
      },
      metadata: { twinPulse: "line-opacity" },
    };
  }

  function buildings3dLayer() {
    // Coalescing height fallback (section 15 risk: sparse OSM render_height
    // in Indian cities): render_height -> building:levels*3 -> 8m default.
    //
    // source is "openmaptiles" (confirmed against OpenFreeMap Liberty's own
    // style.json -- NOT a guessable name, and easy to get wrong silently
    // since a mismatched source makes MapLibre skip the layer with no
    // visible error, not fail loudly). minzoom matches the *source data's*
    // own "building" layer, which OpenFreeMap only emits from zoom 13 --
    // below that there is nothing to extrude regardless of this layer's
    // own settings, confirmed against the same style.json.
    //
    // The `building:levels` fallback branch must not use a bare `null`
    // inside `coalesce` -- MapLibre's expression evaluator does not accept
    // it as a value (confirmed live: "Expected value to be of type number,
    // but found null instead" on every style evaluation); `case`+`has`
    // avoids ever evaluating `to-number` on a genuinely absent property.
    //
    // The flat #334155 fill was replaced by a height ramp: sitting on
    // satellite imagery, a single grey made every block read as one solid
    // slab, whereas shading taller massing lighter is how the eye reads
    // built form. `vertical-gradient` darkens the base of each wall, which
    // is what separates one building from its neighbour at an oblique angle.
    const heightExpression = [
      "coalesce",
      ["get", "render_height"],
      ["case", ["has", "building:levels"],
        ["*", ["to-number", ["get", "building:levels"]], 3],
        8],
    ];
    return {
      id: "buildings-3d",
      type: "fill-extrusion",
      source: "openmaptiles",
      "source-layer": "building",
      minzoom: 13,
      paint: {
        "fill-extrusion-color": [
          "interpolate", ["linear"], heightExpression,
          0, "#1e293b",
          15, "#334155",
          45, "#475569",
          120, "#64748b",
        ],
        "fill-extrusion-opacity": 0.92,
        "fill-extrusion-height": heightExpression,
        "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
        "fill-extrusion-vertical-gradient": true,
      },
    };
  }

  // ------------------------------------------------------------------
  // Basemap rasters
  //
  // Same two Esri endpoints the analyst dashboard's flood maps use, so the
  // twin's imagery is pixel-identical to the maps beside it. The reference
  // layer is what puts place names back over the imagery -- without it the
  // operator is looking at an unlabelled aerial photo.
  // ------------------------------------------------------------------

  const BASEMAP_RASTERS = {
    satellite: {
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      opacity: 1,
      attribution: "© Esri, Maxar, Earthstar Geographics",
    },
    "satellite-labels": {
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
      opacity: 0.85,
    },
  };

  //: Per-raster opacity for the overlays the layer panel can switch on.
  //: A single 0.55 for everything made satellite imagery look fogged and
  //: rain radar look opaque; they want opposite treatments.
  const RASTER_OPACITY = {
    satellite: 1,
    "satellite-labels": 0.85,
    gibs: 0.75,
    radar: 0.5,
    traffic: 0.6,
  };

  global.TwinLayers = {
    STATUS_BANDS,
    LAYER_STACK,
    PRIORITY_COLOURS,
    ASSET_ICON_COLOUR,
    CCTV_KIND_COLOUR,
    CAMERA_OPTICS,
    CAP_SEVERITY_COLOUR,
    AQI_BANDS,
    BASEMAP_RASTERS,
    RASTER_OPACITY,
    destination,
    viewCone,
    conesFrom,
    riskColourExpression,
    hexToRgba,
    riskHeightExpression,
    incidentColourExpression,
    twinHexesLayer,
    twinHexOutlineLayer,
    twinHexesDegradedLayer,
    zoneOutlineLayer,
    incidentsLayer,
    incidentLabelsLayer,
    infrastructureLayer,
    waterBodiesLayer,
    waterBodiesOutlineLayer,
    waterDrainsLayer,
    waterDrainsGlowLayer,
    cctvLayer,
    cctvDirectionLayer,
    cctvConeLayer,
    cctvStreamLayer,
    alertZoneLayer,
    alertZoneOutlineLayer,
    airStationLayer,
    airStationLabelLayer,
    transitVehicleLayer,
    transitStalledHaloLayer,
    flagAreaLayer,
    flagGlowLayer,
    buildings3dLayer,
  };
})(window);
