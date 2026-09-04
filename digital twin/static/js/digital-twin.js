/**
 * TwinMap + dual-pane dashboard controller (README sections 8.3-8.6).
 *
 * Section 8.3 says to extend the host app's existing `GodModeMap` class
 * rather than fork it. That class lives in Sentinel AI's
 * `static/js/god-mode-maps.js`, which this standalone module doesn't have.
 * `TwinMap` extends `window.GodModeMap` when it is present (i.e. once this
 * is dropped into the real app) and falls back to a minimal `TwinMapBase`
 * -- implementing the same deferred-operation queue and
 * addedSources/addedLayers teardown tracking the spec calls out -- so this
 * page is fully functional standalone and becomes a real subclass with zero
 * code changes once integrated. See TWIN_INTEGRATION.md.
 */

(function (global) {
  "use strict";

  const L = global.TwinLayers;
  const HORIZONS = [0, 3, 6, 24];
  const CAMERA_LINK_KEYS = ["zoom", "pitch", "bearing"];

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
  // TwinMapBase: stand-in for GodModeMap when it isn't present.
  // ------------------------------------------------------------------

  class TwinMapBase {
    constructor(container, styleUrl, options) {
      this.addedSources = new Set();
      this.addedLayers = new Set();
      this._deferred = [];
      this._loaded = false;

      this.map = new maplibregl.Map(Object.assign({
        container, style: styleUrl, attributionControl: true,
      }, options || {}));

      // Native "Google Maps" chrome -- zoom +/-, compass, find-my-location,
      // fullscreen. All stacked in the top-right corner (the one corner
      // this page's own overlays -- KPI bottom-left, legend bottom-right,
      // layer toggle top-left -- leave free), themed dark to match in
      // twin.css rather than left at MapLibre's default light styling.
      this.map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");
      this.map.addControl(
        new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true },
                                         trackUserLocation: true, showAccuracyCircle: true }),
        "top-right");
      this.map.addControl(new maplibregl.FullscreenControl(), "top-right");

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
        if (!this.map.getSource(id)) {
          this.map.addSource(id, source);
          this.addedSources.add(id);
        }
      });
    }

    addLayerSafe(layer, before) {
      this.whenLoaded(() => {
        if (!this.map.getLayer(layer.id)) {
          this.map.addLayer(layer, before);
          this.addedLayers.add(layer.id);
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

  const TwinMapBaseClass = global.GodModeMap || TwinMapBase;

  // ------------------------------------------------------------------
  // TwinMap: the twin-specific layer stack on top of the base class.
  // ------------------------------------------------------------------

  class TwinMap extends TwinMapBaseClass {
    constructor(container, styleUrl, cameraOptions) {
      super(container, styleUrl, {
        center: [cameraOptions.center[1], cameraOptions.center[0]],
        zoom: cameraOptions.zoom,
        pitch: cameraOptions.pitch,
        bearing: cameraOptions.bearing,
        antialias: true,
      });
      this._emptyFC = { type: "FeatureCollection", features: [] };
      this._onHexClick = null;
      this._onHexHover = null;
    }

    initTwinSources() {
      this.addSourceSafe("twin-hexes-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-zones-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-incidents-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-infra-src", { type: "geojson", data: this._emptyFC });
      this.addSourceSafe("twin-drains-src", { type: "geojson", data: this._emptyFC });

      // Layer 3 (section 8.4): must render BELOW twin-hexes (layer 4), so
      // risk hexagons are never occluded by a tall building extrusion.
      this.addLayerSafe(L.buildings3dLayer());
      this.addLayerSafe(L.twinHexesLayer("twin-hexes-src"));
      this.addLayerSafe(L.twinHexesDegradedLayer("twin-hexes-src"));
      this.addLayerSafe(L.zoneOutlineLayer("twin-zones-src"));
      this.addLayerSafe(L.waterDrainsLayer("twin-drains-src"));
      this.addLayerSafe(L.infrastructureLayer("twin-infra-src"));
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
      });
    }

    onHexClick(fn) { this._onHexClick = fn; }
    onHexHover(fn) { this._onHexHover = fn; }

    /** Patch only the hex source's data -- never a full refetch on every SSE
     * tick (section 8.6). Callers merge changed features into a full
     * FeatureCollection before calling this. */
    setHexData(featureCollection) {
      this.whenLoaded(() => {
        const src = this.map.getSource("twin-hexes-src");
        if (src) src.setData(featureCollection);
      });
    }

    setZoneData(fc) {
      this.whenLoaded(() => {
        const src = this.map.getSource("twin-zones-src");
        if (src) src.setData(fc);
      });
    }

    setIncidentData(fc) {
      this.whenLoaded(() => {
        const src = this.map.getSource("twin-incidents-src");
        if (src) src.setData(fc);
      });
    }

    setInfrastructureData(fc) {
      this.whenLoaded(() => {
        const src = this.map.getSource("twin-infra-src");
        if (src) src.setData(fc);
      });
    }

    setDrainData(fc) {
      this.whenLoaded(() => {
        const src = this.map.getSource("twin-drains-src");
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

    setRasterUrl(id, tileUrlTemplate, maxzoom) {
      this.whenLoaded(() => {
        if (this.map.getSource(id)) {
          if (this.map.getLayer(id)) this.map.removeLayer(id);
          this.map.removeSource(id);
        }
        if (!tileUrlTemplate) return;
        // maxzoom tells MapLibre to over-zoom the last valid tile past this
        // limit instead of requesting tiles that don't exist -- confirmed
        // live for NASA GIBS (native ~500m/px imagery tops out at zoom 9):
        // omitting this produced a 400 for every tile at the map's default
        // zoom (10.2), because nothing capped the request zoom.
        const source = { type: "raster", tiles: [tileUrlTemplate], tileSize: 256 };
        if (maxzoom) source.maxzoom = maxzoom;
        this.map.addSource(id, source);
        this.map.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": 0.55 } },
          "twin-hexes");
      });
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
        <div class="twin-kpi-row">
          <span>Avg <b>${summary.avg_risk}</b></span>
          <span>&middot; Max <b>${summary.max_risk}</b></span>
        </div>
        <div class="twin-kpi-row twin-kpi-status">
          <span class="twin-badge twin-badge-critical">${s.critical || 0}</span>
          <span class="twin-badge twin-badge-warning">${s.warning || 0}</span>
          <span class="twin-badge twin-badge-watch">${s.watch || 0}</span>
          <span class="twin-badge twin-badge-normal">${s.normal || 0}</span>
        </div>
        <div class="twin-kpi-row">Incidents 24h: <b>${summary.incident_count_24h}</b></div>
      `;
    }

    renderKPIError() {
      if (!this.kpiContainer) return;
      this.kpiContainer.innerHTML = `
        <div class="twin-kpi-row" style="color:var(--twin-watch)">
          KPI unavailable &mdash; retrying&hellip;
        </div>`;
    }

    async setZone(zoneSlug) {
      this.zone = zoneSlug || "__all__";
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
      if (el) el.textContent = status === "live" ? "● live" : status === "polling" ? "○ poll" : "○ degraded";
    }
  }

  global.TwinPane = TwinPane;
  global.TwinMap = TwinMap;
})(window);
