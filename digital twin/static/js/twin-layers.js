/**
 * Layer definitions + paint expressions (README section 8.4).
 *
 * Pure data + small factory functions -- no map instance is touched here.
 * digital-twin.js calls these to add/update layers on a maplibregl.Map.
 */

(function (global) {
  "use strict";

  // Status band colours/opacity mirror twin/config.py STATUS_BANDS exactly.
  // Keep the two in sync by hand -- there is no shared source of truth
  // across the Python/JS boundary for this project's scope.
  const STATUS_BANDS = [
    { max: 25, status: "normal", colour: "#22c55e", opacity: 0.35, heightFactor: 4 },
    { max: 50, status: "watch", colour: "#eab308", opacity: 0.5, heightFactor: 6 },
    { max: 75, status: "warning", colour: "#f97316", opacity: 0.65, heightFactor: 9 },
    { max: 101, status: "critical", colour: "#ef4444", opacity: 0.8, heightFactor: 14 },
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

  function riskHeightExpression() {
    // risk * height_factor(status), matching twin/config.py band_for().
    return [
      "*",
      ["coalesce", ["get", "risk_score"], 0],
      ["step", ["coalesce", ["get", "risk_score"], 0],
        STATUS_BANDS[0].heightFactor,
        25, STATUS_BANDS[1].heightFactor,
        50, STATUS_BANDS[2].heightFactor,
        75, STATUS_BANDS[3].heightFactor],
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

  /** The layer stack, bottom to top (section 8.4 table). Each entry is a
   * declarative spec the controller turns into maplibre add{Source,Layer}
   * calls; `defaultOn` matches the table's toggle-default column. */
  const LAYER_STACK = [
    { id: "basemap", kind: "style", defaultOn: true },
    { id: "satellite", kind: "raster", defaultOn: false },
    { id: "buildings-3d", kind: "fill-extrusion", defaultOn: true },
    { id: "twin-hexes", kind: "fill-extrusion", defaultOn: true },
    { id: "zone-outline", kind: "line", defaultOn: true },
    { id: "radar", kind: "raster", defaultOn: false },
    { id: "water-drains", kind: "line", defaultOn: false },
    { id: "infrastructure", kind: "symbol", defaultOn: false },
    { id: "incidents", kind: "circle", defaultOn: true },
    { id: "incident-labels", kind: "symbol", defaultOn: true, minzoom: 12 },
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
        "line-width": 2,
        "line-dasharray": [2, 1.5],
        "line-opacity": 0.9,
      },
    };
  }

  function zoneOutlineLayer(sourceId) {
    return {
      id: "zone-outline",
      type: "line",
      source: sourceId,
      paint: {
        "line-color": "#e2e8f0",
        "line-width": 1.5,
        "line-opacity": 0.8,
      },
    };
  }

  function incidentsLayer(sourceId) {
    return {
      id: "incidents",
      type: "circle",
      source: sourceId,
      paint: {
        "circle-radius": ["match", ["get", "priority"], "critical", 9, "high", 7, "medium", 6, 5],
        "circle-color": incidentColourExpression(),
        "circle-stroke-width": 2,
        "circle-stroke-color": "#0b1220",
        "circle-opacity": 0.9,
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

  function waterDrainsLayer(sourceId) {
    return {
      id: "water-drains",
      type: "line",
      source: sourceId,
      paint: { "line-color": "#38bdf8", "line-width": 1.5, "line-opacity": 0.7 },
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
    return {
      id: "buildings-3d",
      type: "fill-extrusion",
      source: "openmaptiles",
      "source-layer": "building",
      minzoom: 13,
      paint: {
        "fill-extrusion-color": "#334155",
        "fill-extrusion-opacity": 0.85,
        "fill-extrusion-height": [
          "coalesce",
          ["get", "render_height"],
          ["case", ["has", "building:levels"],
            ["*", ["to-number", ["get", "building:levels"]], 3],
            8],
        ],
        "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
      },
    };
  }

  global.TwinLayers = {
    STATUS_BANDS,
    LAYER_STACK,
    PRIORITY_COLOURS,
    ASSET_ICON_COLOUR,
    riskColourExpression,
    hexToRgba,
    riskHeightExpression,
    incidentColourExpression,
    twinHexesLayer,
    twinHexesDegradedLayer,
    zoneOutlineLayer,
    incidentsLayer,
    incidentLabelsLayer,
    infrastructureLayer,
    waterDrainsLayer,
    buildings3dLayer,
  };
})(window);
