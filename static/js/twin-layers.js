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
    { id: "twin-hexes", kind: "fill-extrusion", defaultOn: true },
    { id: "zone-outline", kind: "line", defaultOn: true },
    { id: "radar", kind: "raster", defaultOn: false },
    { id: "water-bodies", kind: "fill", defaultOn: false },
    { id: "water-bodies-outline", kind: "line", defaultOn: false },
    { id: "water-drains-glow", kind: "line", defaultOn: false },
    { id: "water-drains", kind: "line", defaultOn: false },
    { id: "infrastructure", kind: "circle", defaultOn: false },
    { id: "cctv", kind: "circle", defaultOn: false },
    { id: "cctv-direction", kind: "symbol", defaultOn: false, minzoom: 14 },
    { id: "incidents", kind: "circle", defaultOn: true },
    { id: "incident-labels", kind: "symbol", defaultOn: false, minzoom: 12 },
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
    BASEMAP_RASTERS,
    RASTER_OPACITY,
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
    buildings3dLayer,
  };
})(window);
