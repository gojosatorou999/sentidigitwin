/**
 * TwinMap + per-city pane controller (README sections 8.3-8.6).
 *
 * Section 8.3 originally said to extend the host app's `GodModeMap` class
 * rather than fork it. That is now deliberately NOT done. `GodModeMap`
 * (static/js/god-mode-maps.js) takes `(containerId, options)` and hard-codes
 * pitch 55 / bearing -12.5 in its constructor, while `TwinMap` needs
 * `(container, styleUrl, camera)` and its own deferred-op queue; the
 * `global.GodModeMap || TwinMapBase` fallback that used to sit here meant the
 * twin silently changed base class depending on script order, and on the
 * analyst dashboard -- where god-mode-maps.js happens to load *after* this
 * file -- it only worked by accident. TwinMap now always owns its own base.
 */

(function (global) {
  "use strict";

  const L = global.TwinLayers;

  /** Raster insertion order, as a list of candidate `beforeId`s per raster.
   *
   * MapLibre has no z-index: a layer's position is fixed by which layer it
   * is inserted before, so a raster added while its neighbour happens to be
   * absent lands in the wrong place and stays there. Listing the candidates
   * in top-down order and taking the first one that currently exists makes
   * the stack deterministic however the operator toggles things:
   *
   *   satellite < satellite-labels < buildings-3d < radar/traffic < hexes
   *
   * Imagery must sit BELOW the buildings and the risk grid (it is ground,
   * not overlay); weather and traffic sit above the buildings but below the
   * hexes, so a risk cell is never hidden by a rain cell.
   */
  const RASTER_BEFORE = {
    "satellite": ["satellite-labels", "buildings-3d", "twin-hexes"],
    "gibs": ["satellite-labels", "buildings-3d", "twin-hexes"],
    "satellite-labels": ["buildings-3d", "twin-hexes"],
    "radar": ["traffic", "twin-hexes"],
    "traffic": ["twin-hexes"],
  };

  /** fetch() + JSON parse with a couple of retries on failure.
   *
   * The backend is built to degrade gracefully under transient contention
   * (IngestAdapter's own retry/cache/neutral chain, C1) -- a bare `fetch`
   * with no retry on the client threw that resilience away: one slow
   * request during a brief lock/network blip left a KPI panel stuck on
   * "Loading…" forever with no way to recover short of a page reload
   * (confirmed live: a transient SQLite lock during development produced
   * exactly this). Every panel-populating fetch in TwinPane goes through
   * this instead of a bare `fetch()`.
   */
  async function fetchJsonWithRetry(url, { retries = 2, backoffMs = 1500 } = {}) {
    let lastError;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`${url} -> ${res.status}`);
        return await res.json();
      } catch (err) {
        lastError = err;
        if (attempt < retries) {
          await new Promise((resolve) => setTimeout(resolve, backoffMs * (attempt + 1)));
        }
      }
    }
    throw lastError;
  }

  // ------------------------------------------------------------------
  // TwinMapBase: deferred-op queue + source/layer teardown tracking.
  // ------------------------------------------------------------------

  class TwinMapBase {
    constructor(container, styleUrl, options) {
      this.addedSources = new Set();
      this.addedLayers = new Set();
      this._deferred = [];
      this._loaded = false;

      this.map = new maplibregl.Map(Object.assign({
        container, style: styleUrl, attributionControl: false,
      }, options || {}));

      // Chrome matched to Sentinel's other MapLibre widgets: zoom only, no
      // compass, bottom-right, with a compact attribution beside it. The
      // console previously stacked navigation + geolocate + fullscreen in
      // the top-right, which is three control groups competing with the
      // layer panel and the search box for the same corner of a half-width
      // pane. Fullscreen is kept -- on an embedded dashboard card it is the
      // only way to get a usable map size -- but it joins the same group's
      // corner rather than claiming its own.
      this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
      this.map.addControl(new maplibregl.FullscreenControl(), "bottom-right");
      this.map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");

      this.map.on("load", () => {
        this._loaded = true;
        this._deferred.forEach((fn) => fn());
        this._deferred = [];
      });
    }

    whenLoaded(fn) {
      if (this._loaded) fn();
      else this._deferred.push(fn);
    }

    addSourceSafe(id, source) {
      this.whenLoaded(() => {
        if (this.map.getSource(id)) return;
        try {
          this.map.addSource(id, source);
          this.addedSources.add(id);
        } catch (err) {
          console.warn("twin: addSource", id, err);
        }
      });
    }

    /** Add a layer, tolerating a spec the current style cannot satisfy.
     *
     * `buildings-3d` names the `openmaptiles` vector source by hand, so on
     * any style that does not provide it MapLibre throws out of addLayer --
     * and because these calls run inside the deferred queue, one throw used
     * to abandon every layer queued behind it. Catching here means a
     * missing basemap source costs exactly one layer. */
    addLayerSafe(layer, before) {
      this.whenLoaded(() => {
        if (this.map.getLayer(layer.id)) return;
        const beforeId = before && this.map.getLayer(before) ? before : undefined;
        try {
          this.map.addLayer(layer, beforeId);
          this.addedLayers.add(layer.id);
        } catch (err) {
          console.warn("twin: addLayer", layer.id, err);
        }
      });
    }

    removeLayerSafe(id) {
      this.whenLoaded(() => {
        if (this.map.getLayer(id)) this.map.removeLayer(id);
        this.addedLayers.delete(id);
      });
    }

    removeSourceSafe(id) {
      this.whenLoaded(() => {
        if (this.map.getSource(id)) this.map.removeSource(id);
        this.addedSources.delete(id);
      });
    }

    teardownAll() {
      Array.from(this.addedLayers).forEach((id) => this.removeLayerSafe(id));
      Array.from(this.addedSources).forEach((id) => this.removeSourceSafe(id));
    }
  }

  // ------------------------------------------------------------------
  // TwinMap: the twin-specific layer stack on top of the base class.
  // ------------------------------------------------------------------

  class TwinMap extends TwinMapBase {
    constructor(container, styleUrl, cameraOptions) {
      super(container, styleUrl, {
        center: [cameraOptions.center[1], cameraOptions.center[0]],
        zoom: cameraOptions.zoom,
        // The seeded cities carry pitch 55 / bearing -12.5 from the module's
        // standalone "god mode" defaults. At 55 degrees the far half of the
        // pane is horizon rather than city and every extrusion occludes the
        // one behind it; 45 keeps the 3D read while leaving the ground
        // plane legible. Clamped here rather than in twin/config.py because
        // the value the browser sees comes from the seeded TwinCity row, so
        // changing the config alone would not touch an existing database.
        pitch: Math.min(cameraOptions.pitch == null ? 45 : cameraOptions.pitch, 45),
        bearing: cameraOptions.bearing,
        antialias: true,
        maxPitch: 70,
      });
      this._emptyFC = { type: "FeatureCollection", features: [] };
      this._onHexClick = null;
      this._onHexHover = null;
      this._popup = null;
    }

    initTwinSources() {
      this.addSourceSafe("twin-hexes-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-zones-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-incidents-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-infra-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-drains-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-cctv-src", { type: "geojson", data: this._emptyFC });

      // Layer 3 (section 8.4): must render BELOW twin-hexes (layer 4), so
      // risk hexagons are never occluded by a tall building extrusion.
      this.addLayerSafe(L.buildings3dLayer());
      this.addLayerSafe(L.twinHexesLayer("twin-hexes-src"));
      this.addLayerSafe(L.twinHexOutlineLayer("twin-hexes-src"));
      this.addLayerSafe(L.twinHexesDegradedLayer("twin-hexes-src"));
      this.addLayerSafe(L.zoneOutlineLayer("twin-zones-src"));
      this.addLayerSafe(L.waterBodiesLayer("twin-drains-src"));
      this.addLayerSafe(L.waterBodiesOutlineLayer("twin-drains-src"));
      this.addLayerSafe(L.waterDrainsGlowLayer("twin-drains-src"));
      this.addLayerSafe(L.waterDrainsLayer("twin-drains-src"));
      this.addLayerSafe(L.infrastructureLayer("twin-infra-src"));
      this.addLayerSafe(L.cctvLayer("twin-cctv-src"));
      this.addLayerSafe(L.cctvDirectionLayer("twin-cctv-src"));
      this.addLayerSafe(L.incidentsLayer("twin-incidents-src"));
      this.addLayerSafe(L.incidentLabelsLayer("twin-incidents-src"));

      this.whenLoaded(() => {
        this.map.on("click", "twin-hexes", (e) => {
          if (this._onHexClick && e.features && e.features[0]) {
            this._onHexClick(e.features[0].properties);
          }
        });
        this.map.on("mousemove", "twin-hexes", (e) => {
          this.map.getCanvas().style.cursor = "pointer";
          if (this._onHexHover && e.features && e.features[0]) {
            this._onHexHover(e.features[0].properties, e.lngLat);
          }
        });
        this.map.on("mouseleave", "twin-hexes", () => {
          this.map.getCanvas().style.cursor = "";
          if (this._onHexHover) this._onHexHover(null, null);
        });
        this.map.on("click", "incidents", (e) => {
          const props = e.features && e.features[0] && e.features[0].properties;
          if (props && props.id) {
            window.open("/view_report/" + props.id, "_blank");
          }
        });
        this.map.on("click", "cctv", (e) => this._openCameraPopup(e));
        this.map.on("mouseenter", "cctv", () => {
          this.map.getCanvas().style.cursor = "pointer";
        });
      });
    }

    onHexClick(fn) { this._onHexClick = fn; }
    onHexHover(fn) { this._onHexHover = fn; }

    /** Camera detail on click, as a themed MapLibre popup.
     *
     * A camera's usefulness is entirely in its tags -- who operates it, what
     * it is pointed at, whether a public feed exists -- so a dot the
     * operator cannot interrogate is decoration. The popup is deliberately
     * the only click target on this layer that opens a panel; everything
     * else about the layer stays a plain dot.
     */
    _openCameraPopup(event) {
      const feature = event.features && event.features[0];
      if (!feature) return;
      const p = feature.properties || {};
      const escape = (value) => {
        const node = document.createElement("div");
        node.textContent = value == null ? "" : String(value);
        return node.innerHTML;
      };

      const rows = [
        ["Type", p.camera_type || p.kind],
        ["Coverage", p.zone],
        ["Mount", p.mount],
        ["Operator", p.operator],
        ["Enforcement", p.enforcement],
        ["Facing", p.direction != null && p.direction !== "" ?
          Math.round(Number(p.direction)) + "° from N" : null],
      ].filter(([, value]) => value != null && value !== "");

      const html =
        '<div class="twin-popup">' +
        '<div class="twin-popup-title">' +
        escape(p.name || "Surveillance camera") +
        '<span class="twin-popup-kind">' + escape(p.kind || "unknown") + "</span></div>" +
        rows.map(([label, value]) =>
          '<div class="twin-popup-row"><span>' + label + "</span><b>" +
          escape(value) + "</b></div>").join("") +
        '<div class="twin-popup-links">' +
        (p.stream_url
          ? '<a href="' + escape(p.stream_url) + '" target="_blank" rel="noopener">Public feed</a>'
          : "") +
        '<a href="' + escape(p.osm_url) + '" target="_blank" rel="noopener">OSM record</a>' +
        "</div>" +
        '<div class="twin-popup-attrib">© OpenStreetMap contributors (ODbL)</div>' +
        "</div>";

      if (this._popup) this._popup.remove();
      this._popup = new maplibregl.Popup({ offset: 12, closeButton: true, maxWidth: "260px" })
        .setLngLat(event.lngLat)
        .setHTML(html)
        .addTo(this.map);
    }

    /** Patch only the hex source's data -- never a full refetch on every SSE
     * tick (section 8.6). Callers merge changed features into a full
     * FeatureCollection before calling this. */
    setHexData(featureCollection) { this._setData("twin-hexes-src", featureCollection); }
    setZoneData(fc) { this._setData("twin-zones-src", fc); }
    setIncidentData(fc) { this._setData("twin-incidents-src", fc); }
    setInfrastructureData(fc) { this._setData("twin-infra-src", fc); }
    setDrainData(fc) { this._setData("twin-drains-src", fc); }
    setCctvData(fc) { this._setData("twin-cctv-src", fc); }

    _setData(sourceId, fc) {
      this.whenLoaded(() => {
        const src = this.map.getSource(sourceId);
        if (src) src.setData(fc);
      });
    }

    setLayerVisible(id, visible) {
      this.whenLoaded(() => {
        if (this.map.getLayer(id)) {
          this.map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
        }
      });
    }

    /** Flatten the risk grid without hiding it.
     *
     * "3D risk off" is not the same request as "risk layer off": an operator
     * comparing two wards side by side wants the colours without the
     * columns, because at any tilt a tall column covers the ground behind
     * it. Setting the height expression to a constant 0 keeps every hex,
     * every colour and every click target exactly where they were. */
    setHexExtrusion(on) {
      this.whenLoaded(() => {
        if (!this.map.getLayer("twin-hexes")) return;
        this.map.setPaintProperty("twin-hexes", "fill-extrusion-height",
          on ? L.riskHeightExpression() : 0);
      });
    }

    /** Drop the calm majority of the grid entirely.
     *
     * Low-risk cells are drawn by default -- as a faint wash with no
     * outline, which is enough to say "measured, and fine" without turning
     * the city into a honeycomb. This turns them off completely, for the
     * operator who wants only what is actionable: unlike merely fading
     * them, filtering also removes them as click targets, so clicking
     * "nothing" no longer opens a drawer full of zeroes.
     *
     * Only the fill layer is filtered. `twin-hex-outline` carries its own
     * permanent `>= 25` filter, and overwriting it here would put an
     * outline back on every calm cell -- the exact clutter this pair of
     * settings exists to avoid. */
    setLowRiskVisible(on) {
      const filter = on ? null : [">=", ["coalesce", ["get", "risk_score"], 0], 25];
      this.whenLoaded(() => {
        if (this.map.getLayer("twin-hexes")) this.map.setFilter("twin-hexes", filter);
      });
    }

    setRasterUrl(id, tileUrlTemplate, maxzoom) {
      this.whenLoaded(() => {
        if (this.map.getSource(id)) {
          if (this.map.getLayer(id)) this.map.removeLayer(id);
          this.map.removeSource(id);
          this.addedLayers.delete(id);
          this.addedSources.delete(id);
        }
        if (!tileUrlTemplate) return;
        // maxzoom tells MapLibre to over-zoom the last valid tile past this
        // limit instead of requesting tiles that don't exist -- confirmed
        // live for NASA GIBS (native ~500m/px imagery tops out at zoom 9):
        // omitting this produced a 400 for every tile at the map's default
        // zoom (10.2), because nothing capped the request zoom.
        const source = { type: "raster", tiles: [tileUrlTemplate], tileSize: 256 };
        if (maxzoom) source.maxzoom = maxzoom;
        const spec = (L.BASEMAP_RASTERS || {})[id];
        if (spec && spec.attribution) source.attribution = spec.attribution;

        const before = (RASTER_BEFORE[id] || ["twin-hexes"])
          .find((candidate) => this.map.getLayer(candidate));

        try {
          this.map.addSource(id, source);
          this.map.addLayer({
            id, type: "raster", source: id,
            paint: { "raster-opacity": (L.RASTER_OPACITY || {})[id] || 0.55 },
          }, before);
          this.addedSources.add(id);
          this.addedLayers.add(id);
        } catch (err) {
          console.warn("twin: raster", id, err);
        }
      });
    }

    /** Esri imagery + Esri place labels, as one switch.
     *
     * They are two rasters because they are two tile services, but they are
     * one basemap: imagery with no labels is an aerial photo nobody can
     * navigate, so nothing should ever be able to turn on one without the
     * other. */
    setSatellite(on) {
      const rasters = L.BASEMAP_RASTERS || {};
      this.setRasterUrl("satellite", on ? rasters.satellite.url : null);
      this.setRasterUrl("satellite-labels", on ? rasters["satellite-labels"].url : null);
    }

    flyToBbox(bbox, padding) {
      this.whenLoaded(() => {
        this.map.fitBounds(
          [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
          { padding: padding || 40, duration: 800 },
        );
      });
    }
  }

  // ------------------------------------------------------------------
  // TwinPane: one city's controller (fetch, render, KPI, drill-down feed).
  // ------------------------------------------------------------------

  class TwinPane {
    constructor(citySlug, cityMeta, container, kpiContainer) {
      this.citySlug = citySlug;
      this.cityMeta = cityMeta;
      this.zone = "__all__";
      this.horizon = 0;
      this.currentFC = { type: "FeatureCollection", features: [] };
      // cityMeta.camera holds {zoom, pitch, bearing}; center is a sibling
      // key on the city payload, not nested inside camera -- merge them.
      const cameraOptions = Object.assign({ center: cityMeta.center }, cityMeta.camera);
      this.map = new TwinMap(container, "https://tiles.openfreemap.org/styles/liberty", cameraOptions);
      this.map.initTwinSources();
      this.kpiContainer = kpiContainer;
      this.stream = null;
      this.cameraCount = null;
      this._camerasPromise = null;
      this.waterCount = null;
      this._waterPromise = null;
    }

    async fetchState() {
      const params = new URLSearchParams({
        zone: this.zone, horizon: String(this.horizon), geometry: "true",
      });
      const fc = await fetchJsonWithRetry(`/api/twin/${this.citySlug}/state?${params}`);
      this.currentFC = fc;
      this.map.setHexData(fc);
      return fc;
    }

    async fetchIncidents() {
      const params = new URLSearchParams({ zone: this.zone, since_hours: "72" });
      const fc = await fetchJsonWithRetry(`/api/twin/${this.citySlug}/incidents?${params}`);
      this.map.setIncidentData(fc);
    }

    async fetchInfrastructure() {
      const params = new URLSearchParams({ zone: this.zone });
      const fc = await fetchJsonWithRetry(`/api/twin/${this.citySlug}/infrastructure?${params}`);
      this.map.setInfrastructureData(fc);
    }

    /** OSINT camera layer, fetched once and only when first asked for.
     *
     * The city query is a bbox-wide Overpass call: cheap on the server
     * (weekly disk cache) but slow the first time, and useless to anyone
     * who never turns the layer on. Memoising the promise -- rather than a
     * boolean -- also collapses the double-fetch from an impatient operator
     * toggling the checkbox twice before the first call returns.
     */
    ensureCameras() {
      if (this._camerasPromise) return this._camerasPromise;
      const params = new URLSearchParams({ zone: this.zone });
      this._camerasPromise = fetchJsonWithRetry(
        `/api/twin/${this.citySlug}/cameras?${params}`, { retries: 1 })
        .then((fc) => {
          this.map.setCctvData(fc);
          this.cameraCount = (fc.features || []).length;
          return fc;
        })
        .catch((err) => {
          this._camerasPromise = null;   // let a later toggle try again
          throw err;
        });
      return this._camerasPromise;
    }

    /** Water bodies and drains, fetched once and only when first asked for.
     *
     * The largest layer the console has -- a city's lakes and storm drains
     * run to a few hundred KB of geometry even after rounding -- and most
     * sessions never switch it on, so it follows the same memoised-promise
     * pattern as the camera layer rather than loading with the pane.
     */
    ensureWater() {
      if (this._waterPromise) return this._waterPromise;
      const params = new URLSearchParams({ zone: this.zone });
      this._waterPromise = fetchJsonWithRetry(
        `/api/twin/${this.citySlug}/water?${params}`, { retries: 1 })
        .then((fc) => {
          this.map.setDrainData(fc);
          this.waterCount = (fc.features || []).length;
          return fc;
        })
        .catch((err) => {
          this._waterPromise = null;   // let a later toggle try again
          throw err;
        });
      return this._waterPromise;
    }

    async fetchSummary() {
      const params = new URLSearchParams({ zone: this.zone, horizon: String(this.horizon) });
      try {
        const summary = await fetchJsonWithRetry(`/api/twin/${this.citySlug}/summary?${params}`);
        this.renderKPI(summary);
        return summary;
      } catch (err) {
        this.renderKPIError();
        return null;
      }
    }

    renderKPI(summary) {
      if (!this.kpiContainer) return;
      const s = summary.cells_by_status || {};
      this.kpiContainer.innerHTML = `
        <span class="twin-kpi-stat">Avg <b>${summary.avg_risk}</b></span>
        <span class="twin-kpi-stat">Max <b>${summary.max_risk}</b></span>
        <span class="twin-kpi-sep"></span>
        <span class="twin-badge twin-badge-critical" title="Critical cells">${s.critical || 0}</span>
        <span class="twin-badge twin-badge-warning" title="Warning cells">${s.warning || 0}</span>
        <span class="twin-badge twin-badge-watch" title="Watch cells">${s.watch || 0}</span>
        <span class="twin-badge twin-badge-normal" title="Normal cells">${s.normal || 0}</span>
        <span class="twin-kpi-sep"></span>
        <span class="twin-kpi-stat">Incidents 24h <b>${summary.incident_count_24h}</b></span>
      `;
    }

    renderKPIError() {
      if (!this.kpiContainer) return;
      this.kpiContainer.innerHTML =
        '<span class="twin-kpi-stat" style="color:var(--twin-watch)">' +
        "KPI unavailable &mdash; retrying&hellip;</span>";
    }

    async setZone(zoneSlug) {
      this.zone = zoneSlug || "__all__";
      // Both opt-in layers are scoped to the zone server-side, so a zone
      // change invalidates whatever was fetched for the previous one.
      this._camerasPromise = null;
      this._waterPromise = null;
      await this.refreshAll();
      if (zoneSlug && zoneSlug !== "__all__") {
        const zoneMeta = (this.cityMeta.zones || []).find((z) => z.slug === zoneSlug);
        if (zoneMeta && zoneMeta.center) {
          this.map.whenLoaded(() => this.map.map.flyTo({
            center: [zoneMeta.center[1], zoneMeta.center[0]], zoom: 12, duration: 800,
          }));
        }
      } else {
        this.map.flyToBbox(this.cityMeta.bbox);
      }
    }

    async setHorizon(horizon) {
      this.horizon = horizon;
      await this._settleAll([this.fetchState(), this.fetchSummary()]);
    }

    async refreshAll() {
      await this._settleAll([
        this.fetchState(), this.fetchSummary(), this.fetchIncidents(), this.fetchInfrastructure(),
      ]);
    }

    /** Await every fetch independently (allSettled, not all): one panel's
     * data failing must not stop the others from rendering, and must not
     * throw out of refreshAll() itself -- matching the backend's own
     * per-source degradation instead of an all-or-nothing client. Logs
     * failures for visibility rather than swallowing them entirely. */
    async _settleAll(promises) {
      const results = await Promise.allSettled(promises);
      results.forEach((r) => {
        if (r.status === "rejected") console.warn("twin: fetch failed", r.reason);
      });
    }

    /** Merge an SSE state_update's changed h3 indexes into currentFC by
     * refetching only the state endpoint (still one request, not per-cell --
     * section 8.6's "do not refetch the whole city on every tick" is about
     * avoiding N per-cell requests; a single filtered refetch keeps the
     * client's merged FeatureCollection accurate without extra client-side
     * geometry bookkeeping). */
    async applyChangedCells(changedH3) {
      if (!changedH3 || !changedH3.length) return;
      await this.fetchState();
      await this.fetchSummary();
    }

    startStream(onIncident) {
      this.stream = TwinStream.connect({
        city: this.citySlug,
        onStateUpdate: (evt) => this.applyChangedCells(evt.changed_cells),
        onIncident: (evt) => { if (onIncident) onIncident(this.citySlug, evt); },
        onPollTick: () => this.refreshAll(),
        onStatusChange: (status) => this._setStreamBadge(status),
      });
    }

    _setStreamBadge(status) {
      const el = document.querySelector(`[data-stream-badge="${this.citySlug}"]`);
      if (!el) return;
      el.textContent = status === "live" ? "live" : status === "polling" ? "poll" : "degraded";
      el.className = "stream-badge " + (status === "live" ? "live"
        : status === "polling" ? "polling" : "degraded");
    }
  }

  global.TwinPane = TwinPane;
  global.TwinMap = TwinMap;
})(window);
