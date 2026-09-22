/**
 * Digital Twin console controller.
 *
 * Everything the standalone module kept as a 250-line inline <script> inside
 * digital_twin.html lives here instead, for one reason: the console is now
 * embedded in four different pages (the standalone /digital-twin route, the
 * analyst dashboard, the command dashboard and the coordination dashboard)
 * and an inline script would have had to be duplicated into each of them.
 *
 * Three consequences shape the code below:
 *
 * 1. **The city list comes from the API, not from Jinja.** The panes are
 *    built here from GET /api/twin/cities, so a host page includes one
 *    partial and passes no template variables at all.
 * 2. **Startup is lazy.** Two MapLibre canvases are expensive, and on the
 *    analyst dashboard the console sits below several Leaflet maps and two
 *    live iframes. It boots when it first scrolls into view, then resizes
 *    itself whenever its container changes size -- a map created inside a
 *    zero-height or hidden box renders as a grey rectangle forever otherwise.
 * 3. **Chrome stays off the canvas wherever it can.** The KPI block, the
 *    legend and the search box used to float over the map, which put five
 *    translucent panels plus three MapLibre control groups over a pane that
 *    is half a dashboard card wide. Search and the zone picker now live in
 *    the pane's header, the KPI and legend in a docked strip beneath it, and
 *    the only thing left floating is the layer panel.
 */

(function (global) {
  "use strict";

  const STATUS_LABEL = { normal: "Normal", watch: "Watch", warning: "Warning", critical: "Critical" };

  /** The layer panel, grouped. A flat list of seven checkboxes gave equal
   * billing to "3D buildings" and "Rain radar", which answer completely
   * different questions; grouping them by what they are *about* is what
   * makes the panel scannable rather than just short. */
  const LAYER_GROUPS = [
    {
      label: "Risk",
      items: [
        { id: "hex-3d", label: "3D risk columns", checked: true, kind: "virtual" },
        { id: "low-risk", label: "Low-risk cells", checked: true, kind: "virtual" },
        { id: "incidents", label: "Verified incidents", checked: true },
        { id: "incident-labels", label: "Incident labels", checked: false },
      ],
    },
    {
      label: "City",
      items: [
        { id: "buildings-3d", label: "3D buildings", checked: true },
        { id: "water", label: "Water & drains", checked: false, kind: "lazy" },
        { id: "infrastructure", label: "Critical assets", checked: false },
        { id: "cctv", label: "CCTV (OSINT)", checked: false },
      ],
    },
    {
      label: "Live overlays",
      items: [
        { id: "alerts", label: "Official alert zones", checked: true, kind: "live" },
        { id: "air", label: "Pollution stations", checked: false, kind: "live" },
        { id: "transit", label: "Live transit", checked: false, kind: "live" },
        { id: "radar", label: "Rain radar", checked: false },
        { id: "traffic", label: "Traffic", checked: false },
      ],
    },
    {
      label: "Agent",
      items: [
        { id: "flags", label: "Flagged areas", checked: true, kind: "live" },
      ],
    },
  ];

  //: Toggles whose layer does not exist until the handler creates it.
  const RASTER_LAYERS = new Set(["radar", "traffic"]);
  //: Toggles that own several layers and fetch their data on first use.
  const LAZY_LAYERS = new Set(["cctv", "water"]);
  const LAZY_LAYER_IDS = {
    cctv: ["cctv", "cctv-direction", "cctv-cone"],
    water: ["water-bodies", "water-bodies-outline",
            "water-drains-glow", "water-drains"],
  };

  /** Toggles backed by a layer that refreshes itself on a timer.
   *
   * Separate from LAZY_LAYERS because the lifecycle differs: a lazy layer is
   * fetched once on first use and then sits there, while a live layer keeps
   * pulling for as long as it is switched on. Switching one off stops its
   * polling -- a hidden layer that keeps fetching is how an "idle" dashboard
   * ends up making a request a second, forever.
   */
  const LIVE_LAYER_IDS = {
    alerts: ["alert-zones", "alert-zones-outline"],
    air: ["air-stations", "air-station-labels"],
    transit: ["transit-vehicles", "transit-stalled-halo"],
    flags: ["flag-areas", "flag-glow"],
  };
  const LIVE_LAYERS = new Set(Object.keys(LIVE_LAYER_IDS));

  /** How often each live layer re-fetches, in milliseconds.
   *
   * Matched to how fast the underlying source actually changes rather than to
   * what feels responsive: buses move continuously, CPCB stations publish
   * hourly, and CAP alerts are issued minutes apart. Polling an hourly feed
   * every ten seconds just burns quota to redraw identical dots.
   */
  const LIVE_REFRESH_MS = {
    alerts: 60000,
    air: 120000,
    transit: 20000,
    flags: 45000,
  };

  function el(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    return tpl.content.firstElementChild;
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function round(value) {
    return value == null ? "–" : Math.round(value);
  }

  // Metres between two WGS84 points. Used to order a cell's live cameras
  // nearest-first, so the panel answers "what can see this cell" rather than
  // "what this city owns". Mirrors twin/ingest/cctv_live.py's _haversine_m.
  function haversineM(lat1, lon1, lat2, lon2) {
    if ([lat1, lon1, lat2, lon2].some((v) => v == null || isNaN(v))) return Infinity;
    const R = 6371000;
    const toRad = (d) => (d * Math.PI) / 180;
    const dPhi = toRad(lat2 - lat1);
    const dLambda = toRad(lon2 - lon1);
    const a = Math.sin(dPhi / 2) ** 2 +
      Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLambda / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  // "3m ago" / "2h ago" from an ISO8601 instant -- how live a live feed is.
  function relAge(iso) {
    const then = Date.parse(iso);
    if (isNaN(then)) return "";
    const secs = Math.max(0, (Date.now() - then) / 1000);
    if (secs < 90) return "just now";
    if (secs < 5400) return Math.round(secs / 60) + "m ago";
    if (secs < 172800) return Math.round(secs / 3600) + "h ago";
    return Math.round(secs / 86400) + "d ago";
  }

  // ------------------------------------------------------------------
  // TwinConsole
  // ------------------------------------------------------------------

  class TwinConsole {
    constructor(root) {
      this.root = root;
      this.panes = {};
      this.horizon = 0;
      this.cameraLinked = false;
      this.syncing = false;
      this.drawerCity = null;
      this.drawerH3 = null;
      this.drawerCenter = null;
      this.started = false;
      this.timers = [];
      this.trafficInfoPromise = null;

      this.q = (selector) => this.root.querySelector(selector);
      this.qa = (selector) => Array.from(this.root.querySelectorAll(selector));
    }

    // -- lifecycle -----------------------------------------------------

    async start() {
      if (this.started) return;
      this.started = true;

      this.wireHeader();
      this.wireDrawer();
      this.wireFlagPanel();
      this.wireCoveragePanel();
      this.startClock();

      // The partial loads MapLibre only when the host page has not already
      // done so, so the library may still be in flight at this point.
      try {
        await (global.TwinMapLibreReady || Promise.resolve());
      } catch (err) {
        this.showBootError("The map library could not be loaded (" + err.message + ").");
        return;
      }

      let cities;
      try {
        const response = await fetch("/api/twin/cities");
        if (!response.ok) throw new Error("cities -> " + response.status);
        cities = await response.json();
      } catch (err) {
        this.showBootError(
          "Could not reach the twin API (" + err.message + "). " +
          "The dashboard's other panels are unaffected.");
        return;
      }

      if (!cities || !cities.length) {
        this.showBootError("No twin cities are configured yet.");
        return;
      }

      const panesHost = this.q("[data-twin-panes]");
      panesHost.innerHTML = "";
      cities.forEach((city) => this.buildPane(panesHost, city));

      this.refreshComparison();
      this.refreshHealth();
      this.timers.push(setInterval(() => this.refreshComparison(), 60000));
      this.timers.push(setInterval(() => this.refreshHealth(), 30000));

      this.observeResize();
    }

    showBootError(message) {
      const boot = this.q("[data-twin-boot]");
      if (boot) {
        boot.innerHTML =
          '<i class="fas fa-triangle-exclamation" style="font-size:22px;color:#f59e0b"></i>' +
          "<div>" + escapeHtml(message) + "</div>";
      }
    }

    /** A map built while its container is hidden or zero-height never paints.
     * ResizeObserver covers every way that can happen here -- a Bootstrap tab
     * being shown, the window resizing, a card expanding -- with one hook. */
    observeResize() {
      if (typeof ResizeObserver === "undefined") {
        window.addEventListener("resize", () => this.resizeMaps());
        return;
      }
      const observer = new ResizeObserver(() => this.resizeMaps());
      observer.observe(this.root);
    }

    resizeMaps() {
      Object.values(this.panes).forEach((pane) => {
        try { pane.map.map.resize(); } catch (err) { /* map not ready yet */ }
      });
    }

    // -- pane construction ---------------------------------------------

    buildPane(host, city) {
      const slug = escapeHtml(city.slug);
      const zoneOptions = (city.zones || [])
        .filter((zone) => zone.zone_type !== "synthetic")
        .map((zone) => '<option value="' + escapeHtml(zone.slug) + '">' +
          escapeHtml(zone.display_name) + "</option>")
        .join("");

      const node = el(`
        <div class="twin-pane" data-city="${slug}">
          <div class="twin-pane-header">
            <span class="twin-pane-title">
              ${escapeHtml(city.display_name)}
              <span class="stream-badge" data-stream-badge="${slug}">connecting</span>
            </span>
            <div class="twin-pane-tools">
              <div class="twin-search">
                <i class="fas fa-magnifying-glass"></i>
                <input type="text" class="twin-search-input" autocomplete="off"
                       placeholder="Search a place&hellip;" data-search-input="${slug}">
                <div class="twin-search-results" data-search-results="${slug}"></div>
              </div>
              <select class="twin-select" data-zone-select="${slug}" title="Zone">
                <option value="__all__">Whole city</option>
                ${zoneOptions}
              </select>
            </div>
          </div>

          <div class="twin-map-container" id="twin-map-${slug}">
            <div class="twin-map-tools">
              <button type="button" class="twin-icon-btn twin-layer-toggle-btn"
                      data-layer-panel-toggle="${slug}" title="Layers" aria-label="Layers">
                <i class="fas fa-layer-group"></i>
              </button>
              <div class="twin-layer-panel" data-layer-panel="${slug}">
                ${LAYER_GROUPS.map((group) => `
                  <div class="twin-layer-group">
                    <div class="twin-layer-group-label">${escapeHtml(group.label)}</div>
                    ${group.items.map((item) => `
                      <label><input type="checkbox" data-layer-toggle="${item.id}"
                             ${item.checked ? "checked" : ""}> ${item.label}</label>`).join("")}
                  </div>`).join("")}
              </div>
            </div>

            <div class="twin-tooltip" data-tooltip="${slug}"></div>
          </div>

          <div class="twin-live-strip" data-live-strip="${slug}">
            <span class="twin-live-item twin-muted">Live sources starting&hellip;</span>
          </div>

          <div class="twin-pane-footer">
            <div class="twin-kpi" data-kpi="${slug}">
              <span class="twin-kpi-stat">Loading&hellip;</span>
            </div>
            <div class="twin-legend" title="Risk score bands">
              <span class="twin-legend-bar"></span>
              <span class="twin-legend-ticks"><i>0</i><i>25</i><i>50</i><i>75</i><i>100</i></span>
            </div>
          </div>
        </div>
      `);

      host.appendChild(node);

      const container = node.querySelector(".twin-map-container");
      const kpiEl = node.querySelector("[data-kpi]");
      const pane = new global.TwinPane(city.slug, city, container, kpiEl);
      this.panes[city.slug] = pane;

      pane.map.onHexClick((props) => this.openDrawer(city.slug, props));
      pane.map.onHexHover((props, lngLat) => this.showTooltip(city.slug, props, lngLat));

      pane.map.whenLoaded(() => {
        pane.map.flyToBbox(city.bbox);
        pane.refreshAll();
        pane.startStream((slug_) => this.onIncidentEvent(slug_), {
          onAlerts: () => this.refreshLiveLayer(pane, "alerts"),
          onTransit: () => this.refreshLiveLayer(pane, "transit"),
          onFlags: () => this.refreshLiveLayer(pane, "flags"),
        });
        this.wireCameraSync(pane);
        this.syncLayerPanel(pane);
        this.startLiveLayers(pane);
        this.wireLivePopups(pane);
        // The basemap select starts on "Satellite" -- the same Esri imagery
        // the rest of Sentinel's maps use -- so the raster has to be applied
        // once at load, not only when the operator changes the dropdown.
        this.applyBasemap(this.q("[data-twin-basemap]").value, "", [pane]);
        pane.map.map.resize();
      });

      this.wireZoneSelect(pane);
      this.wireLayerPanel(pane);
      this.wireSearchBox(pane);
    }

    // -- header --------------------------------------------------------

    wireHeader() {
      this.qa("[data-horizon]").forEach((btn) => {
        btn.addEventListener("click", () => {
          this.qa("[data-horizon]").forEach((other) => other.classList.remove("active"));
          btn.classList.add("active");
          this.horizon = parseInt(btn.dataset.horizon, 10);
          Object.values(this.panes).forEach((pane) => pane.setHorizon(this.horizon));
          this.refreshComparison();
        });
      });

      const cameraBtn = this.q("[data-twin-camera-link]");
      cameraBtn.addEventListener("click", () => {
        this.cameraLinked = !this.cameraLinked;
        cameraBtn.classList.toggle("on", this.cameraLinked);
        this.toast(this.cameraLinked
          ? "Camera link on — both cities pan together"
          : "Camera link off");
      });

      const basemapSelect = this.q("[data-twin-basemap]");
      const gibsDate = this.q("[data-twin-gibs-date]");
      basemapSelect.addEventListener("change", () =>
        this.applyBasemap(basemapSelect.value, gibsDate.value));
      gibsDate.addEventListener("change", () => {
        if (basemapSelect.value === "gibs") this.applyBasemap("gibs", gibsDate.value);
      });

      const refreshBtn = this.q("[data-twin-refresh]");
      if (refreshBtn) refreshBtn.addEventListener("click", () => this.recompute(refreshBtn));
    }

    async recompute(button) {
      button.disabled = true;
      const original = button.innerHTML;
      button.innerHTML = '<i class="fas fa-rotate fa-spin"></i>';
      try {
        const response = await fetch("/api/twin/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ city: "all" }),
        });
        if (response.status === 403) {
          this.toast("Recompute is restricted to officials.");
        } else if (!response.ok) {
          this.toast("Recompute failed (" + response.status + ").");
        } else {
          this.toast("State recomputed for all cities.");
          Object.values(this.panes).forEach((pane) => pane.refreshAll());
          this.refreshComparison();
          this.refreshHealth();
        }
      } catch (err) {
        this.toast("Recompute failed: " + err.message);
      } finally {
        button.disabled = false;
        button.innerHTML = original;
      }
    }

    startClock() {
      const clock = this.q("[data-twin-clock]");
      const tick = () => { clock.textContent = new Date().toLocaleTimeString(); };
      tick();
      this.timers.push(setInterval(tick, 1000));
    }

    // -- per-pane wiring -----------------------------------------------

    wireZoneSelect(pane) {
      const select = this.q('[data-zone-select="' + pane.citySlug + '"]');
      if (!select) return;
      select.addEventListener("change", () => {
        pane.setZone(select.value);
        // A zone change drops both cached opt-in layers; refetch only the
        // ones the operator actually has switched on.
        LAZY_LAYERS.forEach((key) => {
          const box = this.q('[data-layer-panel="' + pane.citySlug +
            '"] [data-layer-toggle="' + key + '"]');
          if (!box || !box.checked) return;
          if (key === "cctv") this.toggleCctv(pane, true, box);
          if (key === "water") this.toggleWater(pane, true, box);
        });
        this.refreshComparison();
      });
    }

    wireLayerPanel(pane) {
      const toggleBtn = this.q('[data-layer-panel-toggle="' + pane.citySlug + '"]');
      const panel = this.q('[data-layer-panel="' + pane.citySlug + '"]');
      if (toggleBtn && panel) {
        toggleBtn.addEventListener("click", (event) => {
          event.stopPropagation();
          panel.classList.toggle("open");
          toggleBtn.classList.toggle("on", panel.classList.contains("open"));
        });
        // Click-away close. Without it the panel stays open over the map for
        // the rest of the session once it has been used.
        document.addEventListener("click", (event) => {
          if (panel.contains(event.target) || toggleBtn.contains(event.target)) return;
          panel.classList.remove("open");
          toggleBtn.classList.remove("on");
        });
      }
      if (!panel) return;

      panel.querySelectorAll("[data-layer-toggle]").forEach((input) => {
        input.addEventListener("change", () => {
          const layerId = input.dataset.layerToggle;
          if (layerId === "hex-3d") { pane.map.setHexExtrusion(input.checked); return; }
          if (layerId === "low-risk") { pane.map.setLowRiskVisible(input.checked); return; }
          if (layerId === "radar") { this.toggleRadar(pane, input.checked); return; }
          if (layerId === "traffic") { this.toggleTraffic(pane, input.checked, input); return; }
          if (layerId === "cctv") { this.toggleCctv(pane, input.checked, input); return; }
          if (layerId === "water") { this.toggleWater(pane, input.checked, input); return; }
          if (LIVE_LAYERS.has(layerId)) { this.toggleLiveLayer(pane, layerId, input.checked); return; }

          pane.map.setLayerVisible(layerId, input.checked);
        });
      });
    }

    /** Push the checkbox states onto the map once, at load.
     *
     * TwinLayers now sets `layout.visibility` explicitly on the layers whose
     * checkbox starts unchecked, but the ones that start *checked* still
     * need pushing (and a browser that restored a form state across a reload
     * needs every one of them re-applied). Raster and virtual toggles are
     * skipped: their layers do not exist until their handler creates them,
     * and their defaults are already the map's initial state. */
    syncLayerPanel(pane) {
      const panel = this.q('[data-layer-panel="' + pane.citySlug + '"]');
      if (!panel) return;
      panel.querySelectorAll("[data-layer-toggle]").forEach((input) => {
        const layerId = input.dataset.layerToggle;
        if (RASTER_LAYERS.has(layerId) || LAZY_LAYERS.has(layerId)
            || LIVE_LAYERS.has(layerId)) return;
        if (layerId === "hex-3d") { pane.map.setHexExtrusion(input.checked); return; }
        if (layerId === "low-risk") { pane.map.setLowRiskVisible(input.checked); return; }
        pane.map.setLayerVisible(layerId, input.checked);
      });
    }

    /** RainViewer publishes a manifest of timestamped frames; the tile path
     * embeds the frame time, so the newest frame has to be looked up rather
     * than hard-coded. */
    async toggleRadar(pane, on) {
      if (!on) { pane.map.setRasterUrl("radar", null); return; }
      try {
        const response = await fetch("https://api.rainviewer.com/public/weather-maps.json");
        const manifest = await response.json();
        const frames = (manifest.radar && manifest.radar.past) || [];
        const latest = frames[frames.length - 1];
        if (!latest) { this.toast("No radar frames available right now."); return; }
        pane.map.setRasterUrl("radar",
          manifest.host + latest.path + "/256/{z}/{x}/{y}/2/1_1.png");
      } catch (err) {
        this.toast("Rain radar unavailable: " + err.message);
      }
    }

    toggleTraffic(pane, on, checkbox) {
      if (!on) { pane.map.setRasterUrl("traffic", null); return; }
      if (!this.trafficInfoPromise) {
        this.trafficInfoPromise = fetch("/api/twin/traffic").then((r) => r.json());
      }
      this.trafficInfoPromise.then((info) => {
        if (!info.available) {
          this.toast("Traffic layer needs a TOMTOM_API_KEY (optional).");
          checkbox.checked = false;
          return;
        }
        pane.map.setRasterUrl("traffic", info.tile_url_template);
      });
    }

    /** OSINT surveillance cameras, from OpenStreetMap via the twin API. */
    toggleCctv(pane, on, checkbox) {
      return this.toggleLazyLayer(pane, "cctv", on, checkbox, {
        fetch: () => pane.ensureCameras(),
        loaded: () => pane.cameraCount
          ? pane.cameraCount + " mapped cameras in " + pane.cityMeta.display_name +
            " (OpenStreetMap)"
          : "No surveillance cameras are mapped here in OpenStreetMap yet.",
        failed: "Camera layer unavailable",
      });
    }

    /** Lakes, tanks, canals and storm drains, from the same weekly Overpass
     * payload the terrain sub-score is derived from. */
    toggleWater(pane, on, checkbox) {
      return this.toggleLazyLayer(pane, "water", on, checkbox, {
        fetch: () => pane.ensureWater(),
        loaded: () => pane.waterCount
          ? pane.waterCount + " water bodies and drains in " + pane.cityMeta.display_name
          : "No water features are mapped here in OpenStreetMap yet.",
        failed: "Water layer unavailable",
      });
    }

    // -- live layers ---------------------------------------------------

    /** Start every live layer whose checkbox is ticked at boot.
     *
     * Alerts and flags start on: an official warning and an agent flag are
     * the two things an operator must never have to discover a toggle for.
     * Stations and vehicles start off, because they are dense and most
     * sessions are not about them.
     */
    startLiveLayers(pane) {
      pane.liveTimers = pane.liveTimers || {};
      const panel = this.q('[data-layer-panel="' + pane.citySlug + '"]');

      Object.keys(LIVE_LAYER_IDS).forEach((key) => {
        const input = panel && panel.querySelector('[data-layer-toggle="' + key + '"]');
        const on = input ? input.checked : false;
        this.toggleLiveLayer(pane, key, on);
      });
    }

    toggleLiveLayer(pane, key, on) {
      (LIVE_LAYER_IDS[key] || []).forEach((id) => pane.map.setLayerVisible(id, on));

      pane.liveTimers = pane.liveTimers || {};
      if (pane.liveTimers[key]) {
        clearInterval(pane.liveTimers[key]);
        delete pane.liveTimers[key];
      }
      if (!on) {
        this.renderLiveStrip(pane);
        return;
      }

      this.refreshLiveLayer(pane, key);
      // A hidden layer that keeps polling is how an idle dashboard ends up
      // making a request a second forever, so the interval is owned by the
      // toggle rather than by the pane.
      pane.liveTimers[key] = setInterval(
        () => this.refreshLiveLayer(pane, key), LIVE_REFRESH_MS[key] || 60000);
      this.timers.push(pane.liveTimers[key]);
    }

    async refreshLiveLayer(pane, key) {
      try {
        if (key === "alerts") {
          await pane.fetchAlerts();
        } else if (key === "flags") {
          await pane.fetchFlags();
          this.renderFlagBadge();
        } else {
          await pane.fetchLive(key);
        }
        pane.liveErrors = Object.assign({}, pane.liveErrors, { [key]: null });
      } catch (err) {
        // A live layer that fails must say so in the strip rather than
        // freezing on its last good frame: stale data presented as current is
        // the one failure this console is built to prevent.
        pane.liveErrors = Object.assign({}, pane.liveErrors, { [key]: err.message });
      }
      this.renderLiveStrip(pane);
    }

    /** The per-layer freshness strip under each map.
     *
     * This is the honesty mechanism for the whole live-data phase: every
     * layer states how old its newest reading is, or why it has none. An
     * operator can then tell the difference between "the city is quiet" and
     * "this feed stopped twenty minutes ago", which look identical on a map.
     */
    renderLiveStrip(pane) {
      const strip = this.q('[data-live-strip="' + pane.citySlug + '"]');
      if (!strip) return;

      const items = [];
      const errors = pane.liveErrors || {};
      const running = pane.liveTimers || {};

      const age = (seconds) => {
        if (seconds == null) return "no reading";
        if (seconds < 90) return "just now";
        if (seconds < 5400) return Math.round(seconds / 60) + "m old";
        return Math.round(seconds / 3600) + "h old";
      };

      if (running.alerts) {
        const count = pane.alertCount || 0;
        items.push({
          cls: count ? "warn" : "ok", icon: "fa-triangle-exclamation",
          text: count ? count + " official alert" + (count === 1 ? "" : "s") + " in force"
                      : "No official alerts in force",
          error: errors.alerts,
        });
      }
      ["air", "transit"].forEach((key) => {
        if (!running[key]) return;
        const meta = (pane.liveMeta || {})[key] || {};
        const label = key === "air" ? "AQI stations" : "vehicles";
        items.push({
          cls: meta.count ? "ok" : "muted", icon: key === "air" ? "fa-wind" : "fa-bus",
          text: meta.count
            ? meta.count + " " + label + " · " + age(meta.newestAgeSeconds)
            : "No live " + label + " (feed not configured)",
          error: errors[key],
        });
      });
      if (running.flags) {
        const pending = (pane.flags || []).filter((flag) => flag.status === "pending").length;
        items.push({
          cls: pending ? "flag" : "muted", icon: "fa-flag",
          text: pending ? pending + " flagged area" + (pending === 1 ? "" : "s") + " awaiting review"
                        : "No areas flagged",
          error: errors.flags,
        });
      }

      if (!items.length) {
        strip.innerHTML = '<span class="twin-live-item twin-muted">Live layers are switched off.</span>';
        return;
      }

      strip.innerHTML = items.map((item) => {
        const failed = !!item.error;
        return '<span class="twin-live-item ' + (failed ? "error" : item.cls) + '"' +
          (failed ? ' title="' + escapeHtml(item.error) + '"' : "") + '>' +
          '<i class="fas ' + (failed ? "fa-plug-circle-xmark" : item.icon) + '"></i>' +
          escapeHtml(failed ? "feed unreachable" : item.text) + "</span>";
      }).join("");
    }

    /** Click handlers for the live point layers.
     *
     * Each of these dots is a claim about a specific instrument or vehicle,
     * so each must be interrogable -- an unclickable dot is decoration, and
     * decoration is what the old static imagery already was.
     */
    wireLivePopups(pane) {
      const map = pane.map.map;

      const popup = (lngLat, html) => {
        if (pane._livePopup) pane._livePopup.remove();
        pane._livePopup = new maplibregl.Popup({ offset: 12, maxWidth: "280px" })
          .setLngLat(lngLat).setHTML(html).addTo(map);
      };

      map.on("click", "air-stations", (event) => {
        const props = (event.features && event.features[0] || {}).properties || {};
        let metrics = {};
        try { metrics = JSON.parse(props.metrics || "{}"); } catch (err) { metrics = {}; }
        const rows = Object.entries(metrics)
          .map(([name, value]) => '<div class="twin-popup-row"><span>' +
            escapeHtml(name.toUpperCase()) + "</span><b>" + escapeHtml(value) + "</b></div>")
          .join("");
        popup(event.lngLat,
          '<div class="twin-popup"><div class="twin-popup-title">' +
          escapeHtml(props.name || "Monitoring station") +
          '<span class="twin-popup-kind">' + escapeHtml(props.source || "") + "</span></div>" +
          '<div class="twin-popup-row"><span>Index</span><b>' +
          escapeHtml(round(Number(props.value))) + " " + escapeHtml(props.unit || "") + "</b></div>" +
          rows +
          '<div class="twin-popup-row"><span>Measured</span><b>' +
          escapeHtml(props.observed_at ? relAge(props.observed_at) : "unknown") + "</b></div>" +
          (props.status === "stale"
            ? '<div class="twin-popup-warn">This instrument has stopped reporting.</div>' : "") +
          "</div>");
      });

      map.on("click", "transit-vehicles", (event) => {
        const props = (event.features && event.features[0] || {}).properties || {};
        let metrics = {};
        try { metrics = JSON.parse(props.metrics || "{}"); } catch (err) { metrics = {}; }
        popup(event.lngLat,
          '<div class="twin-popup"><div class="twin-popup-title">' +
          escapeHtml(props.name || "Vehicle") +
          '<span class="twin-popup-kind">' + escapeHtml(metrics.route_id || "") + "</span></div>" +
          '<div class="twin-popup-row"><span>Speed</span><b>' +
          escapeHtml(round(Number(props.value))) + " km/h</b></div>" +
          '<div class="twin-popup-row"><span>Reported</span><b>' +
          escapeHtml(props.observed_at ? relAge(props.observed_at) : "unknown") + "</b></div>" +
          "</div>");
      });

      map.on("click", "cctv-streams", (event) => {
        const props = (event.features && event.features[0] || {}).properties || {};
        this.openStream(props);
      });

      ["air-stations", "transit-vehicles", "cctv-streams", "flag-areas"].forEach((layer) => {
        map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
      });
    }

    /** Shared machinery for a toggle that owns several layers and pulls its
     * data on first use.
     *
     * The first toggle can trigger a city-wide Overpass query behind a
     * weekly server-side cache, so on a cold cache it takes seconds. The
     * checkbox is disabled meanwhile rather than left looking unresponsive,
     * and a failure puts it back to unchecked -- a checked box over an empty
     * layer is the one outcome that would leave the operator believing the
     * city has no cameras and no drains.
     */
    async toggleLazyLayer(pane, key, on, checkbox, handlers) {
      const ids = LAZY_LAYER_IDS[key] || [key];
      ids.forEach((id) => pane.map.setLayerVisible(id, on));
      if (!on) return;

      if (checkbox) checkbox.disabled = true;
      try {
        await handlers.fetch();
        this.toast(handlers.loaded());
      } catch (err) {
        this.toast(handlers.failed + ": " + err.message);
        if (checkbox) checkbox.checked = false;
        ids.forEach((id) => pane.map.setLayerVisible(id, false));
      } finally {
        if (checkbox) checkbox.disabled = false;
      }
    }

    /** Address search via Nominatim -- free, keyless, and rate-limited, hence
     * the 400 ms debounce and the request-id guard that drops a response
     * whose keystroke has already been superseded. */
    wireSearchBox(pane) {
      const input = this.q('[data-search-input="' + pane.citySlug + '"]');
      const results = this.q('[data-search-results="' + pane.citySlug + '"]');
      if (!input || !results) return;

      let debounceTimer = null;
      let activeRequestId = 0;

      const renderResults = (matches) => {
        if (!matches.length) {
          results.innerHTML = '<div class="twin-search-result muted">No matches</div>';
          results.classList.add("open");
          return;
        }
        results.innerHTML = matches.map((match, index) =>
          '<div class="twin-search-result" data-idx="' + index + '">' +
          escapeHtml(match.display_name.split(",")[0]) +
          '<div class="muted">' + escapeHtml(match.display_name) + "</div></div>").join("");
        results.classList.add("open");

        results.querySelectorAll("[data-idx]").forEach((node) => {
          node.addEventListener("mousedown", () => {
            const match = matches[parseInt(node.dataset.idx, 10)];
            pane.map.whenLoaded(() => pane.map.map.flyTo({
              center: [parseFloat(match.lon), parseFloat(match.lat)],
              zoom: 15, duration: 1000,
            }));
            input.value = match.display_name.split(",")[0];
            results.classList.remove("open");
          });
        });
      };

      const runSearch = async (query) => {
        const requestId = ++activeRequestId;
        const [minLon, minLat, maxLon, maxLat] = pane.cityMeta.bbox;
        const params = new URLSearchParams({
          q: query, format: "jsonv2", limit: "5",
          viewbox: minLon + "," + maxLat + "," + maxLon + "," + minLat, bounded: "0",
        });
        try {
          const response = await fetch(
            "https://nominatim.openstreetmap.org/search?" + params,
            { headers: { Accept: "application/json" } });
          if (requestId !== activeRequestId) return;
          renderResults(response.ok ? await response.json() : []);
        } catch (err) {
          if (requestId === activeRequestId) renderResults([]);
        }
      };

      input.addEventListener("input", () => {
        clearTimeout(debounceTimer);
        const query = input.value.trim();
        if (query.length < 3) {
          results.classList.remove("open");
          results.innerHTML = "";
          return;
        }
        debounceTimer = setTimeout(() => runSearch(query), 400);
      });

      input.addEventListener("blur", () => {
        setTimeout(() => results.classList.remove("open"), 150);
      });
    }

    wireCameraSync(pane) {
      pane.map.map.on("moveend", () => {
        if (!this.cameraLinked || this.syncing) return;
        this.syncing = true;
        const view = {
          zoom: pane.map.map.getZoom(),
          pitch: pane.map.map.getPitch(),
          bearing: pane.map.map.getBearing(),
        };
        Object.values(this.panes).forEach((other) => {
          if (other !== pane) other.map.map.jumpTo(view);
        });
        this.syncing = false;
      });
    }

    // -- basemap -------------------------------------------------------

    /** Satellite / street / GIBS, applied to every pane (or a named subset
     * during pane construction, when only one map exists yet).
     *
     * These three are mutually exclusive by construction -- one <select>,
     * not three checkboxes. The layer panel deliberately no longer carries a
     * "Satellite" checkbox: when it did, the panel and the dropdown could
     * disagree about what the basemap was, and whichever was touched last
     * silently won. */
    async applyBasemap(choice, gibsDate, panes) {
      const targets = panes || Object.values(this.panes);
      const isSatellite = choice === "satellite";
      const isGibs = choice === "gibs";
      this.q("[data-twin-gibs-date]").style.display = isGibs ? "" : "none";

      targets.forEach((pane) => {
        pane.map.setSatellite(isSatellite);
        if (!isGibs) pane.map.setRasterUrl("gibs", null);
      });

      if (!isGibs) return;

      try {
        const params = new URLSearchParams();
        if (gibsDate) params.set("date", gibsDate);
        const info = await (await fetch("/api/twin/gibs?" + params)).json();
        if (!info.available) {
          this.toast("NASA GIBS imagery is not available right now.");
          this.q("[data-twin-basemap]").value = "satellite";
          targets.forEach((pane) => pane.map.setSatellite(true));
          return;
        }
        if (info.fell_back && gibsDate) this.q("[data-twin-gibs-date]").value = info.resolved_date;
        targets.forEach((pane) =>
          pane.map.setRasterUrl("gibs", info.tile_url_template, info.max_zoom));
      } catch (err) {
        this.toast("Could not load NASA GIBS imagery.");
        this.q("[data-twin-basemap]").value = "satellite";
        targets.forEach((pane) => pane.map.setSatellite(true));
      }
    }

    // -- comparison / health / tooltip ---------------------------------

    async refreshComparison() {
      try {
        const response = await fetch("/api/twin/compare?horizon=" + this.horizon);
        if (!response.ok) return;
        const data = await response.json();
        this.q("[data-twin-comparison]").innerHTML = Object.entries(data).map(
          ([slug, summary]) => {
            const byStatus = summary.cells_by_status || {};
            return '<span class="twin-comparison-item"><span class="city">' +
              escapeHtml(slug) + "</span> avg <b>" + summary.avg_risk +
              "</b> · critical <b>" + (byStatus.critical || 0) +
              "</b> · incidents 24h <b>" + summary.incident_count_24h +
              "</b> · assets at risk <b>" +
              (summary.critical_assets_at_risk || 0) + "</b></span>";
          }).join("");
      } catch (err) { /* the strip is best-effort and never blocks the maps */ }
    }

    async refreshHealth() {
      try {
        const response = await fetch("/api/twin/health");
        if (!response.ok) return;
        const data = await response.json();
        const dots = Object.entries(data.sources)
          .filter(([, source]) => source.tier === 1)
          .map(([key, source]) => {
            const cls = source.status === "ok" ? "ok"
              : (source.status === "degraded" || source.status === "failed") ? "degraded" : "";
            return '<span class="twin-health-dot ' + cls + '" title="' +
              escapeHtml(key + ": " + source.status) + '"></span>';
          }).join("");
        this.q("[data-twin-health]").innerHTML =
          '<span class="twin-header-label">Health</span>' + dots;
      } catch (err) { /* best-effort */ }
    }

    /** Hover readout, positioned at the cursor.
     *
     * It used to be pinned to the pane's top-left corner, directly underneath
     * the layer button -- so hovering a hex covered the control the operator
     * had just used, and the number they were reading was nowhere near the
     * cell it described. Following the pointer costs one project() call and
     * fixes both. */
    showTooltip(citySlug, props, lngLat) {
      const node = this.q('[data-tooltip="' + citySlug + '"]');
      if (!node) return;
      if (!props) { node.classList.remove("visible"); return; }

      let degraded = props.degraded_inputs || [];
      if (typeof degraded === "string") {
        // MapLibre flattens non-scalar feature properties to JSON strings.
        try { degraded = JSON.parse(degraded); } catch (err) { degraded = []; }
      }
      node.innerHTML =
        '<b class="status-' + escapeHtml(props.status) + '">' +
        escapeHtml(STATUS_LABEL[props.status] || props.status) +
        "</b> · risk " + Math.round(props.risk_score) +
        "<br>Incidents nearby: " + props.incident_count +
        (degraded.length
          ? '<div class="degraded-note">Degraded: ' + escapeHtml(degraded.join(", ")) + "</div>"
          : "");

      const pane = this.panes[citySlug];
      if (pane && lngLat) {
        const point = pane.map.map.project(lngLat);
        const box = pane.map.map.getCanvas().getBoundingClientRect();
        // Flip to the other side of the cursor near the pane's right/bottom
        // edge so the readout is never clipped by the pane border.
        const flipX = point.x > box.width - 200;
        const flipY = point.y > box.height - 90;
        node.style.left = (flipX ? point.x - 194 : point.x + 14) + "px";
        node.style.top = (flipY ? point.y - 84 : point.y + 14) + "px";
      }
      node.classList.add("visible");
    }

    onIncidentEvent(citySlug) {
      const pane = this.panes[citySlug];
      if (!pane) return;
      pane.fetchIncidents();
      pane.fetchState();
      pane.fetchSummary();
      this.toast("New verified incident in " + citySlug);
    }

    // -- drill-down drawer ---------------------------------------------

    wireDrawer() {
      this.drawer = this.q("[data-twin-drawer]");
      this.q("[data-twin-drawer-close]").addEventListener("click", () => this.closeDrawer());
      this.qa("[data-twin-action]").forEach((btn) => {
        btn.addEventListener("click", () => this.runAction(btn.dataset.twinAction));
      });
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") this.closeDrawer();
      });
    }

    closeDrawer() {
      this._stopLiveRefresh();
      this.drawer.classList.remove("open");
    }

    // The live-webcam snapshot poller is per-open-cell, not session-length, so
    // it lives outside this.timers and is cleared whenever the ground-truth
    // panel is reset, a new shot is shown, or the drawer closes -- otherwise a
    // click-through-ten-cells session leaves ten pollers hammering Windy.
    _stopLiveRefresh() {
      if (this._liveRefreshTimer) {
        clearInterval(this._liveRefreshTimer);
        this._liveRefreshTimer = null;
      }
    }

    async openDrawer(citySlug, hexProps) {
      this.drawerCity = citySlug;
      this.drawerH3 = hexProps.h3;
      this.drawerCenter = null;

      const pane = this.panes[citySlug];
      this.q("[data-twin-drawer-city]").textContent =
        pane ? pane.cityMeta.display_name : citySlug;
      this.q("[data-twin-drawer-h3]").textContent = hexProps.h3;
      this.q("[data-twin-drawer-zone]").textContent = "";
      this.setStatusTag(hexProps.status, hexProps.risk_score);
      this.q("[data-twin-drawer-explanation]").textContent = "Loading cell detail…";
      this.q("[data-twin-drawer-subscores]").innerHTML = "";
      this.resetStreetPanel();
      this.drawer.classList.add("open");

      try {
        const response = await fetch(
          "/api/twin/" + citySlug + "/cell/" + hexProps.h3 + "?horizon=" + this.horizon);
        if (!response.ok) throw new Error("cell -> " + response.status);
        const data = await response.json();
        this.renderDrawer(data);
        if (data.center) {
          this.drawerCenter = { lat: data.center[0], lon: data.center[1] };
          this.loadStreetView(data.center[0], data.center[1]);
          this.loadCctv(data.center[0], data.center[1]);
          this.loadLiveCams(citySlug);
        }
      } catch (err) {
        this.q("[data-twin-drawer-explanation]").textContent =
          "Could not load this cell (" + err.message + ").";
      }
    }

    setStatusTag(status, riskScore) {
      const tag = this.q("[data-twin-drawer-status]");
      tag.className = "status-tag " + (status || "");
      tag.textContent = (STATUS_LABEL[status] || status || "").toUpperCase() +
        " · " + Math.round(riskScore || 0);
    }

    renderDrawer(data) {
      const state = data.state || {};
      this.q("[data-twin-drawer-h3]").textContent = data.h3;
      this.q("[data-twin-drawer-zone]").textContent =
        data.zone_display_name ? "· " + data.zone_display_name : "";
      this.setStatusTag(state.status, state.risk_score);

      this.q("[data-twin-drawer-subscores]").innerHTML = [
        ["Hydro", state.hydro_score], ["Incident", state.incident_score],
        ["Terrain", state.terrain_score], ["Infra", state.infra_score],
        ["Environment", state.env_score],
      ].map(([label, value]) =>
        '<span class="twin-subscore">' + label + "<b>" + round(value) + "</b></span>").join("");

      this.q("[data-twin-drawer-explanation]").textContent = data.explanation || "";

      const assets = data.assets || [];
      this.q("[data-twin-drawer-assets]").innerHTML = assets.length
        ? assets.map((asset) => '<span class="twin-asset-chip">' +
            escapeHtml(asset.name || asset.asset_type) + "</span>").join("")
        : "None on record in this cell";

      // showNearbyVolunteers() repurposes this pane and its heading; put
      // both back every time a cell is rendered.
      this.q("[data-twin-reports-title]").textContent = "Verified reports (72h)";
      const reports = data.reports || [];
      this.q("[data-twin-drawer-reports]").innerHTML = reports.length
        ? reports.map((report) => '<span class="twin-report-card">' +
            escapeHtml(report.hazard_type || "report") + " · " +
            escapeHtml(report.priority || "") + "</span>").join("")
        : "None in the last 72 hours";
    }

    // -- street-level ground truth --------------------------------------

    resetStreetPanel() {
      this._stopLiveRefresh();
      this.q("[data-twin-street-provider]").textContent = "loading…";
      this.q("[data-twin-street-provider]").classList.remove("live");
      this.q("[data-twin-street-note]").textContent = "";
      this.q("[data-twin-street-body]").innerHTML =
        '<div class="twin-street-empty">Fetching street-level imagery…</div>';
      this.q("[data-twin-street-links]").innerHTML = "";
      this.q("[data-twin-cctv-count]").textContent = "";
      this.q("[data-twin-cctv-list]").innerHTML =
        '<span class="twin-muted">Looking for mapped cameras…</span>';
    }

    async loadStreetView(lat, lon) {
      const body = this.q("[data-twin-street-body]");
      const providerTag = this.q("[data-twin-street-provider]");
      const note = this.q("[data-twin-street-note]");
      const links = this.q("[data-twin-street-links]");

      let data;
      try {
        const response = await fetch(
          "/api/twin/streetview?lat=" + lat.toFixed(6) + "&lon=" + lon.toFixed(6));
        if (!response.ok) throw new Error("streetview -> " + response.status);
        data = await response.json();
      } catch (err) {
        providerTag.textContent = "unavailable";
        body.innerHTML = '<div class="twin-street-empty">Street imagery lookup failed (' +
          escapeHtml(err.message) + ").</div>";
        return;
      }

      links.innerHTML =
        '<a href="' + escapeHtml(data.google_streetview_url) + '" target="_blank" rel="noopener">' +
        '<i class="fas fa-street-view"></i> Google Street View</a>' +
        '<a href="' + escapeHtml(data.osm_url) + '" target="_blank" rel="noopener">' +
        '<i class="fas fa-map"></i> OpenStreetMap</a>';

      const shots = (data.images || []).concat(data.webcams || []);
      if (!shots.length) {
        providerTag.textContent = "no open coverage";
        note.textContent = Object.entries(data.providers || {})
          .map(([name, status]) => name + ": " + status).join("  ·  ");
        body.innerHTML =
          '<div class="twin-street-empty">No open street-level imagery within ' +
          (data.radius_m || 350) + " m of this cell.<br>" +
          "Use the Google Street View link below to inspect the location directly.</div>";
        return;
      }

      const isLive = !!(data.webcams || []).length;
      providerTag.textContent = isLive ? "live webcam" : shots[0].provider;
      providerTag.classList.toggle("live", isLive);
      note.textContent = shots[0].attribution || "";

      body.innerHTML =
        '<div class="twin-street-stage" data-street-stage></div>' +
        '<div class="twin-street-thumbs" data-street-thumbs></div>';

      const thumbs = body.querySelector("[data-street-thumbs]");
      thumbs.innerHTML = shots.map((shot, index) =>
        '<button type="button" class="twin-street-thumb' + (index === 0 ? " active" : "") +
        '" data-shot="' + index + '">' +
        '<img src="' + escapeHtml(shot.thumb_url || shot.image_url) + '" alt="" loading="lazy">' +
        (shot.distance_m != null
          ? '<span class="dist">' + Math.round(shot.distance_m) + "m</span>"
          : "") + "</button>").join("");

      const stage = body.querySelector("[data-street-stage]");

      // Caption row, shared by the snapshot and the embedded-player views so
      // the LIVE badge, age and the mode toggle stay put when swapping between
      // them.
      const captionHtml = (shot, mode) => {
        const badge = shot.live ? '<span class="twin-live-badge"><i></i>LIVE</span> ' : "";
        const when = shot.live
          ? (shot.last_updated ? relAge(shot.last_updated) : "")
          : (shot.captured_at ? escapeHtml(shot.captured_at) : "");
        const toggle = shot.player_url
          ? '<a href="#" data-live-toggle>' +
            (mode === "player" ? "▣ Snapshot" : "▶ Live player") + "</a>"
          : "";
        const source = shot.permalink
          ? '<a href="' + escapeHtml(shot.permalink) +
            '" target="_blank" rel="noopener" style="color:#7dd3fc">source</a>'
          : "";
        return '<div class="twin-street-caption"><span>' + badge +
          escapeHtml(shot.title || shot.provider) + (when ? " · " + when : "") +
          '</span><span class="twin-stage-actions">' + toggle + source + "</span></div>";
      };

      // A live webcam still updates at the source; re-request it in place with
      // a cache-buster so the tile is genuinely live rather than one frozen
      // frame. Snapshot mode is the default -- it is light and always works,
      // where the embedded player is heavier and provider-dependent.
      const showSnapshot = (shot) => {
        this._stopLiveRefresh();
        const src = shot.image_url || shot.thumb_url;
        stage.innerHTML =
          '<img data-live-img src="' + escapeHtml(src) + '" alt="Street-level view">' +
          captionHtml(shot, "snapshot");
        if (shot.live && src) {
          this._liveRefreshTimer = setInterval(() => {
            const img = stage.querySelector("[data-live-img]");
            if (!img) return;
            img.src = src + (src.indexOf("?") === -1 ? "?" : "&") + "_r=" + Date.now();
          }, 30000);
        }
        wireToggle(stage, shot, "snapshot");
      };

      // The provider's own embeddable player (Windy publishes these /embed/
      // URLs for exactly this). We embed the published player -- never proxy
      // or scrape the underlying stream.
      const showPlayer = (shot) => {
        this._stopLiveRefresh();
        stage.innerHTML =
          '<iframe class="twin-live-frame" src="' + escapeHtml(shot.player_url) +
          '" allowfullscreen loading="lazy" referrerpolicy="no-referrer" ' +
          'title="Live webcam player"></iframe>' + captionHtml(shot, "player");
        wireToggle(stage, shot, "player");
      };

      const wireToggle = (root, shot, mode) => {
        const toggle = root.querySelector("[data-live-toggle]");
        if (!toggle) return;
        toggle.addEventListener("click", (event) => {
          event.preventDefault();
          (mode === "player" ? showSnapshot : showPlayer)(shot);
        });
      };

      const showShot = (index) => {
        const shot = shots[index];
        showSnapshot(shot);
        thumbs.querySelectorAll("[data-shot]").forEach((node) =>
          node.classList.toggle("active", parseInt(node.dataset.shot, 10) === index));
        note.textContent = shot.attribution || "";
      };

      thumbs.querySelectorAll("[data-shot]").forEach((node) => {
        node.addEventListener("click", () => showShot(parseInt(node.dataset.shot, 10)));
      });
      showShot(0);
    }

    /** OSINT cameras covering this cell.
     *
     * Deliberately a separate request from the street-imagery one: the two
     * degrade independently (Overpass and KartaView have nothing to do with
     * each other), and a slow Overpass query must not hold up a photo that
     * is already cached.
     */
    async loadCctv(lat, lon) {
      const list = this.q("[data-twin-cctv-list]");
      const count = this.q("[data-twin-cctv-count]");

      let data;
      try {
        const response = await fetch(
          "/api/twin/cctv?lat=" + lat.toFixed(6) + "&lon=" + lon.toFixed(6));
        if (!response.ok) throw new Error("cctv -> " + response.status);
        data = await response.json();
      } catch (err) {
        count.textContent = "";
        list.innerHTML = '<span class="twin-muted">Camera lookup failed (' +
          escapeHtml(err.message) + ").</span>";
        return;
      }

      const cameras = data.cameras || [];
      count.textContent = cameras.length
        ? cameras.length + " within " + (data.radius_m || 400) + " m"
        : "";

      if (!cameras.length) {
        list.innerHTML =
          '<span class="twin-muted">No surveillance cameras are mapped within ' +
          (data.radius_m || 400) + " m of this cell in OpenStreetMap.</span>";
        return;
      }

      list.innerHTML = cameras.slice(0, 12).map((camera) => {
        const bits = [camera.camera_type, camera.zone, camera.operator]
          .filter(Boolean).join(" · ");
        return '<a class="twin-cctv-chip kind-' + escapeHtml(camera.kind) + '" href="' +
          escapeHtml(camera.stream_url || camera.osm_url) +
          '" target="_blank" rel="noopener" title="' + escapeHtml(bits || camera.kind) + '">' +
          '<i class="fas fa-video"></i>' +
          '<span>' + escapeHtml(camera.name || camera.kind) + "</span>" +
          (camera.distance_m != null
            ? '<em>' + Math.round(camera.distance_m) + "m</em>" : "") +
          (camera.stream_url ? '<b class="feed">feed</b>' : "") + "</a>";
      }).join("") +
        '<div class="twin-muted" style="margin-top:6px">' +
        Object.entries(data.counts_by_kind || {})
          .map(([kind, n]) => escapeHtml(kind) + " " + n).join(" · ") +
        " · © OpenStreetMap contributors (ODbL)</div>";
    }

    /** Ensure the city's feed list is loaded, then render it.
     *
     * The catalog is fetched at most once per pane and sits behind a
     * 15-minute server cache, so opening cell after cell costs nothing. A
     * failure renders as a failure rather than as "no cameras here" -- the
     * two must stay distinguishable.
     */
    async loadLiveCams(citySlug) {
      const pane = this.panes[citySlug];
      if (!pane) return;

      if (!pane.streamMeta) {
        try {
          await pane.fetchStreams();
        } catch (err) {
          const body = this.q("[data-twin-live-body]");
          if (body) {
            body.innerHTML = '<span class="twin-muted">Live feed lookup failed (' +
              escapeHtml(err.message) + ").</span>";
          }
          return;
        }
      }
      this.renderLiveCams(citySlug);
    }

    /** Live camera feeds for the open cell's city.
     *
     * This block always renders something. An empty camera list is a real
     * and expected answer for Hyderabad and Bengaluru -- no road authority
     * publishes a catalog for either -- and the one thing it must never do
     * is look identical to a broken layer. So the empty state names the
     * authorities that were weighed and says none covers this ground, which
     * is a finding an operator can act on rather than a blank panel.
     */
    renderLiveCams(citySlug) {
      const body = this.q("[data-twin-live-body]");
      const count = this.q("[data-twin-live-count]");
      const source = this.q("[data-twin-live-source]");
      if (!body) return;

      const pane = this.panes[citySlug];
      const meta = pane && pane.streamMeta;
      if (!meta) {
        source.textContent = "—";
        count.textContent = "";
        body.innerHTML = '<span class="twin-muted">Feed status not loaded yet.</span>';
        return;
      }

      const streams = (pane.streams || []);
      const total = streams.length;
      const reference = meta.reference || null;
      count.textContent = total ? total + " live" : "";
      source.textContent = !meta.enabled
        ? "providers disabled"
        : reference
          ? "reference — not local"
          : (meta.providers.length
              ? meta.providers.length + " provider" + (meta.providers.length === 1 ? "" : "s")
              : "operator file only");

      if (!total) {
        const considered = meta.providers.length
          ? "Weighed: " + meta.providers.map((p) => escapeHtml(p.name)).join(", ") + "."
          : "No registered road authority publishes a camera catalog covering this city.";
        const failures = Object.entries(meta.status || {})
          .filter(([, text]) => String(text).startsWith("failed"));
        body.innerHTML =
          '<span class="twin-muted">No live feed is available here. ' + considered +
          (failures.length
            ? " " + failures.length + " provider(s) failed this refresh."
            : " Camera <em>positions</em> are still mapped above from OpenStreetMap.") +
          " An operator feed can be added to <code>" +
          escapeHtml("data/twin/cctv_streams.json") + "</code>.</span>";
        return;
      }

      // Nearest first when the drawer knows where the cell is, so the list
      // answers "what can see *this* cell" rather than "what exists".
      //
      // Reference feeds are left in catalog order and given no distance:
      // both would be measured from a cell in this city to a camera in
      // another country, which is a real number that means nothing. The
      // server already spreads them across their catalog.
      const centre = reference ? null : this.drawerCenter;
      const ordered = centre
        ? streams.slice().sort((a, b) =>
            haversineM(centre.lat, centre.lon, a.lat, a.lon) -
            haversineM(centre.lat, centre.lon, b.lat, b.lon))
        : streams;

      const banner = reference
        ? '<div class="twin-cctv-reference" role="note">' +
          '<b><i class="fas fa-triangle-exclamation"></i> Not local — ' +
          escapeHtml(reference.region) + "</b>" +
          "<span>No authority publishes a camera catalog for this city, so " +
          "these are live feeds from " + escapeHtml(reference.name) +
          " (" + reference.catalog_count + " cameras). They are real and " +
          "current, but they are not this city's ground: they stay off the " +
          "map and are not read by scoring, flags or briefs.</span></div>"
        : "";

      body.innerHTML = banner + ordered.slice(0, reference ? 12 : 8).map((stream, index) => {
        const distance = centre && stream.lat != null
          ? Math.round(haversineM(centre.lat, centre.lon, stream.lat, stream.lon)) + "m"
          : "";
        const guessed = stream.heading_confidence === "low";
        return '<button type="button" class="twin-cctv-chip live' +
          (stream.reference ? " reference" : "") + '" data-twin-live-open="' +
          index + '" title="' + escapeHtml(stream.operator || stream.provider || "") +
          (stream.reference ? " · " + escapeHtml(stream.reference_region) +
            ", not this city" : "") +
          (guessed ? " · bearing is a guess, not surveyed" : "") + '">' +
          '<i class="fas fa-video"></i><span>' +
          escapeHtml(stream.name || "Camera") + "</span>" +
          (distance ? "<em>" + distance + "</em>" : "") +
          '<b class="feed">' + escapeHtml(stream.type) + "</b></button>";
      }).join("") +
        '<div class="twin-muted" style="margin-top:6px">' +
        (reference
          ? "Showing " + ordered.length + " of " + reference.catalog_count + " · "
          : (meta.configured ? meta.configured + " operator-supplied · " : "") +
            meta.fromProviders + " from public authorities · ") +
        escapeHtml([...new Set(ordered.map((s) => s.attribution).filter(Boolean))]
          .join(" · ")) + "</div>";

      body.querySelectorAll("[data-twin-live-open]").forEach((button) => {
        button.addEventListener("click", () => {
          this.openStream(ordered[Number(button.dataset.twinLiveOpen)]);
        });
      });
    }

    // -- coordination actions -------------------------------------------

    async runAction(action) {
      if (!this.drawerCity || !this.drawerH3) return;
      const endpoint = (global.TWIN_COORDINATION_ENDPOINTS || {})[action];
      if (!endpoint) {
        this.toast('"' + action + '" is not wired to a coordination endpoint.');
        return;
      }

      const center = this.drawerCenter || this.centroidFromLoadedCells();
      if (!center) { this.toast("Cell centre unknown; reopen the cell."); return; }

      const pane = this.panes[this.drawerCity];
      const cityName = pane ? pane.cityMeta.display_name : this.drawerCity;
      const statusText = this.q("[data-twin-drawer-status]").textContent || "";

      if (endpoint.mode === "form") {
        const params = new URLSearchParams({
          title: "Twin alert: " + cityName + " cell " + this.drawerH3,
          description: "Raised from the Urban Digital Twin. Cell " + this.drawerH3 +
            " (" + cityName + ") is at " + statusText + ". " +
            (this.q("[data-twin-drawer-explanation]").textContent || ""),
          location: cityName + " — cell " + this.drawerH3,
          latitude: center.lat.toFixed(6),
          longitude: center.lon.toFixed(6),
          severity: this.severityFromStatus(),
          hazard_type: "coastal_flooding",
          radius_km: "3",
        });
        window.open(endpoint.url + "?" + params.toString(), "_blank", "noopener");
        return;
      }

      if (endpoint.mode === "volunteers") {
        await this.showNearbyVolunteers(endpoint.url, center);
        return;
      }

      if (endpoint.mode === "broadcast") {
        if (!window.confirm(
          "Broadcast an alert to every user within 15 km of this cell?")) return;
        try {
          const response = await fetch(endpoint.url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              message: cityName + " digital twin: cell " + this.drawerH3 +
                " is at " + statusText + ". Follow local safety guidance.",
              affected_locations: [{
                latitude: center.lat, longitude: center.lon,
                name: cityName + " cell " + this.drawerH3,
                type: "Digital Twin risk cell",
                description: this.q("[data-twin-drawer-explanation]").textContent || "",
              }],
            }),
          });
          const result = await response.json();
          this.toast(result.success
            ? "Alert broadcast to " + result.users_alerted + " user(s)."
            : "Broadcast failed: " + (result.error || response.status));
        } catch (err) {
          this.toast("Broadcast failed: " + err.message);
        }
      }
    }

    // -- agent flags ---------------------------------------------------

    /** The pending-flag count in the header. */
    renderFlagBadge() {
      const badge = this.q("[data-twin-flag-badge]");
      if (!badge) return;

      const pending = Object.values(this.panes).reduce(
        (total, pane) => total + (pane.flags || [])
          .filter((flag) => flag.status === "pending").length, 0);

      badge.textContent = pending ? String(pending) : "";
      badge.classList.toggle("has-flags", pending > 0);
      const button = this.q("[data-twin-flags-toggle]");
      if (button) button.classList.toggle("alerting", pending > 0);
    }

    openFlagQueue() {
      const panel = this.q("[data-twin-flags-panel]");
      if (!panel) return;
      panel.classList.add("open");
      this.renderFlagQueue();
    }

    closeFlagQueue() {
      const panel = this.q("[data-twin-flags-panel]");
      if (panel) panel.classList.remove("open");
    }

    renderFlagQueue() {
      const list = this.q("[data-twin-flags-list]");
      if (!list) return;

      const flags = [];
      Object.values(this.panes).forEach((pane) => {
        (pane.flags || []).forEach((flag) => flags.push(
          Object.assign({ citySlug: pane.citySlug, cityName: pane.cityMeta.display_name }, flag)));
      });
      flags.sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0));

      if (!flags.length) {
        list.innerHTML =
          '<div class="twin-flag-empty">' +
          "<b>Nothing flagged right now.</b>" +
          "<span>The agent reviews live feeds on a timer and raises an area here " +
          "only when the risk formula clears the threshold. An empty queue is " +
          "the normal state of a calm city.</span></div>";
        return;
      }

      list.innerHTML = flags.map((flag) => {
        const pending = flag.status === "pending";
        const citations = (flag.citations || []).map((citation) =>
          '<a href="' + escapeHtml(citation.url || "#") + '" target="_blank" rel="noopener">' +
          escapeHtml(citation.title || citation.source || "source") + "</a>").join("");

        return '<article class="twin-flag-card ' + escapeHtml(flag.severity || "watch") + '"' +
          ' data-flag-id="' + escapeHtml(flag.id) + '"' +
          ' data-flag-city="' + escapeHtml(flag.citySlug) + '">' +
          '<header><span class="twin-flag-sev">' + escapeHtml(flag.severity || "watch") + "</span>" +
          '<span class="twin-flag-title">' + escapeHtml(flag.title || "Flagged area") + "</span>" +
          '<span class="twin-flag-score">' + escapeHtml(round(flag.risk_score)) + "</span></header>" +
          '<div class="twin-flag-meta">' +
          escapeHtml(flag.cityName) + " · " + escapeHtml(flag.cell_count || 0) + " cells · " +
          escapeHtml(flag.hazard_type || "hazard") +
          (flag.agent_mode === "llm" ? " · AI brief" : " · rule-based brief") +
          (flag.created_at ? " · " + escapeHtml(relAge(flag.created_at)) : "") + "</div>" +
          '<div class="twin-flag-brief">' + escapeHtml(flag.brief_md || "") + "</div>" +
          (citations ? '<div class="twin-flag-cites">' + citations + "</div>" : "") +
          '<footer>' +
          '<button type="button" class="twin-action-btn secondary" data-flag-action="show">' +
          '<i class="fas fa-location-crosshairs"></i> Show on map</button>' +
          (pending
            ? '<button type="button" class="twin-action-btn danger" data-flag-action="dispatch">' +
              '<i class="fas fa-tower-broadcast"></i> Alert affected people</button>' +
              '<button type="button" class="twin-action-btn" data-flag-action="reject">' +
              '<i class="fas fa-xmark"></i> Dismiss</button>'
            : '<span class="twin-flag-reviewed">' + escapeHtml(flag.status) +
              (flag.reviewed_at ? " · " + escapeHtml(relAge(flag.reviewed_at)) : "") + "</span>") +
          "</footer></article>";
      }).join("");
    }

    // -- camera coverage -------------------------------------------------

    /** Probe every registered camera authority and show what came back.
     *
     * Answers the question the per-cell panel structurally cannot: "which of
     * these feeds actually work?" The drawer only ever shows one cell's
     * ground, so an operator looking at two Indian cities sees empty panel
     * after empty panel with no way to tell a dead layer from an unserved
     * one. This lists the whole registry, live.
     *
     * Not on any refresh path: it calls every authority in the registry,
     * which is exactly what the coverage gate exists to avoid doing
     * routinely. It runs when an operator asks, and not otherwise.
     */
    async openCoverage() {
      const panel = this.q("[data-twin-coverage-panel]");
      const list = this.q("[data-twin-coverage-list]");
      if (!panel || !list) return;

      panel.classList.add("open");
      list.innerHTML = '<div class="twin-muted">Probing every registered ' +
        "authority… this calls each one once, so it takes a moment.</div>";

      let data;
      try {
        data = await fetchJsonWithRetry("/api/twin/cctv/coverage", { retries: 0 });
      } catch (err) {
        list.innerHTML = '<div class="twin-muted">Coverage probe failed (' +
          escapeHtml(err.message) + ").</div>";
        return;
      }

      const rows = data.providers || [];
      list.innerHTML =
        '<div class="twin-coverage-summary"><b>' + data.working_count + " of " +
        data.registry_count + "</b> authorities responding · <b>" +
        data.total_cameras.toLocaleString() + "</b> cameras reachable</div>" +
        '<table class="twin-coverage-table"><thead><tr>' +
        "<th>Authority</th><th>Region</th><th>Status</th>" +
        "<th>Cameras</th><th>Serves</th></tr></thead><tbody>" +
        rows.map((row) => {
          const ok = row.status === "ok";
          const covers = Object.keys(row.covers || {})
            .filter((slug) => row.covers[slug]);
          return "<tr>" +
            "<td>" + escapeHtml(row.name) +
            (row.is_reference
              ? ' <span class="twin-coverage-tag">reference</span>'
              : "") + "</td>" +
            "<td>" + escapeHtml(row.region) + "</td>" +
            '<td class="' + (ok ? "ok" : "bad") + '">' +
              escapeHtml(ok ? "live" : row.status) + "</td>" +
            '<td class="num">' + row.cameras.toLocaleString() + "</td>" +
            "<td>" + (covers.length
              ? covers.map(escapeHtml).join(", ")
              : '<span class="twin-muted">no modelled city</span>') + "</td>" +
            "</tr>";
        }).join("") +
        "</tbody></table>" +
        '<div class="twin-muted" style="margin-top:8px">' +
        escapeHtml(data.note) + "</div>";
    }

    closeCoverage() {
      const panel = this.q("[data-twin-coverage-panel]");
      if (panel) panel.classList.remove("open");
    }

    wireCoveragePanel() {
      const button = this.q("[data-twin-coverage]");
      if (button) {
        button.addEventListener("click", () => {
          const panel = this.q("[data-twin-coverage-panel]");
          if (panel && panel.classList.contains("open")) this.closeCoverage();
          else this.openCoverage();
        });
      }
      const close = this.q("[data-twin-coverage-close]");
      if (close) close.addEventListener("click", () => this.closeCoverage());
    }

    wireFlagPanel() {
      const toggle = this.q("[data-twin-flags-toggle]");
      if (toggle) toggle.addEventListener("click", () => {
        const panel = this.q("[data-twin-flags-panel]");
        if (panel && panel.classList.contains("open")) this.closeFlagQueue();
        else this.openFlagQueue();
      });

      const close = this.q("[data-twin-flags-close]");
      if (close) close.addEventListener("click", () => this.closeFlagQueue());

      const list = this.q("[data-twin-flags-list]");
      if (!list) return;
      list.addEventListener("click", (event) => {
        const button = event.target.closest("[data-flag-action]");
        if (!button) return;
        const card = button.closest("[data-flag-id]");
        if (!card) return;

        const flagId = card.dataset.flagId;
        const citySlug = card.dataset.flagCity;
        const action = button.dataset.flagAction;

        if (action === "show") return this.showFlagOnMap(citySlug, flagId);
        if (action === "reject") return this.reviewFlag(citySlug, flagId, "reject");
        if (action === "dispatch") return this.confirmDispatch(citySlug, flagId, button);
      });

      const modalClose = this.q("[data-twin-stream-close]");
      if (modalClose) modalClose.addEventListener("click", () => this.closeStream());
    }

    showFlagOnMap(citySlug, flagId) {
      const pane = this.panes[citySlug];
      if (!pane) return;
      const flag = (pane.flags || []).find((f) => String(f.id) === String(flagId));
      if (!flag || !flag.center) return;
      pane.map.whenLoaded(() => {
        pane.map.map.flyTo({ center: [flag.center.lon, flag.center.lat], zoom: 13.5, duration: 900 });
      });
    }

    async reviewFlag(citySlug, flagId, decision, note) {
      try {
        const response = await fetch("/api/twin/flags/" + encodeURIComponent(flagId) + "/review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, note: note || "" }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || response.status);
        this.toast(decision === "reject" ? "Flag dismissed." : "Flag approved.");
        await this.refreshLiveLayer(this.panes[citySlug], "flags");
        this.renderFlagQueue();
      } catch (err) {
        this.toast("Could not update the flag: " + err.message);
      }
    }

    /** Preview who would be reached, then dispatch on confirmation.
     *
     * Two steps on purpose. This is the one control in the console that
     * contacts real members of the public, and an operator is entitled to
     * know how many people that is *before* deciding, not after.
     */
    async confirmDispatch(citySlug, flagId, button) {
      button.disabled = true;
      try {
        const response = await fetch(
          "/api/twin/flags/" + encodeURIComponent(flagId) + "/dispatch/preview");
        const preview = await response.json();
        if (!response.ok) throw new Error(preview.error || response.status);

        const lines = [
          "Alert " + preview.recipients + " people within " +
            preview.radius_km + " km of this flagged area?",
          preview.whatsapp_reachable + " of them have WhatsApp linked; the rest " +
            "get an in-app notification.",
        ];
        if (preview.cooldown_active) {
          lines.push("NOTE: this area was alerted " + preview.cooldown_minutes_ago +
            " minutes ago. Sending again may cause alert fatigue.");
        }
        if (!preview.recipients) {
          lines.push("Nobody is registered in range -- the alert would reach no one.");
        }
        if (!window.confirm(lines.join("\n\n"))) return;

        const sent = await fetch("/api/twin/flags/" + encodeURIComponent(flagId) + "/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ radius_km: preview.radius_km }),
        });
        const result = await sent.json();
        if (!sent.ok) throw new Error(result.error || sent.status);

        this.toast("Alert sent to " + result.recipients + " people (" +
          result.whatsapp_sent + " via WhatsApp).");
        await this.refreshLiveLayer(this.panes[citySlug], "flags");
        this.renderFlagQueue();
      } catch (err) {
        this.toast("Dispatch failed: " + err.message);
      } finally {
        button.disabled = false;
      }
    }

    // -- live camera streams -------------------------------------------

    /** Play an operator-supplied camera feed.
     *
     * The browser fetches the stream directly from whoever publishes it. The
     * Sentinel server never proxies, re-hosts or stores a frame of it -- that
     * keeps the twin a map of camera infrastructure rather than an access
     * route into it, and it is also the only version that performs.
     *
     * HLS needs hls.js everywhere except Safari, which plays .m3u8 natively.
     * It is loaded from a CDN on first use only, so a console that never
     * opens a stream never pays for it.
     */
    openStream(stream) {
      const modal = this.q("[data-twin-stream-modal]");
      const body = this.q("[data-twin-stream-body]");
      const title = this.q("[data-twin-stream-title]");
      if (!modal || !body) return;

      // The modal is the one place a feed fills the screen with no list
      // around it, so the "not local" label has to travel with it -- a
      // reference frame shown full-size and unlabelled is the exact
      // misreading this whole path is built to prevent.
      title.textContent = (stream.reference ? "[" + stream.reference_region + "] " : "") +
        (stream.name || "Live camera");
      this.q("[data-twin-stream-attrib]").textContent =
        (stream.reference
          ? "Reference feed — not this city. " + stream.reference_region + " · "
          : "") +
        (stream.attribution || stream.operator || "Operator-supplied feed");
      modal.classList.add("open");
      body.innerHTML = '<div class="twin-street-empty">Connecting to the feed&hellip;</div>';

      if (stream.type === "iframe" || stream.type === "youtube") {
        body.innerHTML = '<iframe src="' + escapeHtml(stream.url) +
          '" allow="autoplay; fullscreen" referrerpolicy="no-referrer"></iframe>';
        return;
      }
      if (stream.type === "image" || stream.type === "mjpeg") {
        // A snapshot camera needs a cache-buster; a video stream must not
        // have one. Getting this backwards either freezes the image forever
        // or restarts the video on every repaint.
        const bust = stream.type === "image" ? "?_t=" + Date.now() : "";
        body.innerHTML = '<img src="' + escapeHtml(stream.url + bust) +
          '" alt="' + escapeHtml(stream.name || "camera") + '" referrerpolicy="no-referrer">';
        if (stream.type === "image") {
          this._streamRefresh = setInterval(() => {
            const img = body.querySelector("img");
            if (img) img.src = stream.url + "?_t=" + Date.now();
          }, 5000);
        }
        return;
      }

      this._playHls(body, stream);
    }

    async _playHls(body, stream) {
      body.innerHTML = '<video controls autoplay muted playsinline></video>';
      const video = body.querySelector("video");

      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = stream.url;   // Safari plays HLS natively
        return;
      }

      try {
        if (!global.Hls) {
          await new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js";
            script.onload = resolve;
            script.onerror = () => reject(new Error("hls.js could not be loaded"));
            document.head.appendChild(script);
          });
        }
        const hls = new global.Hls({ liveDurationInfinity: true });
        hls.loadSource(stream.url);
        hls.attachMedia(video);
        this._hls = hls;
      } catch (err) {
        body.innerHTML = '<div class="twin-street-empty">This feed could not be played (' +
          escapeHtml(err.message) + "). <a href=\"" + escapeHtml(stream.url) +
          '" target="_blank" rel="noopener">Open it directly</a>.</div>';
      }
    }

    closeStream() {
      const modal = this.q("[data-twin-stream-modal]");
      if (modal) modal.classList.remove("open");
      // Tear the player down rather than hiding it: a hidden <video> keeps
      // downloading a live stream, and a hidden iframe keeps its socket open.
      if (this._hls) { try { this._hls.destroy(); } catch (err) { /* already gone */ } this._hls = null; }
      if (this._streamRefresh) { clearInterval(this._streamRefresh); this._streamRefresh = null; }
      const body = this.q("[data-twin-stream-body]");
      if (body) body.innerHTML = "";
    }

    severityFromStatus() {
      const tag = this.q("[data-twin-drawer-status]");
      if (tag.classList.contains("critical")) return "critical";
      if (tag.classList.contains("warning")) return "high";
      if (tag.classList.contains("watch")) return "medium";
      return "low";
    }

    /** Fallback centroid: the /cell payload normally supplies `center`, but
     * if that request failed the hexagon's own geometry is still on the map. */
    centroidFromLoadedCells() {
      const pane = this.panes[this.drawerCity];
      if (!pane) return null;
      const feature = (pane.currentFC.features || [])
        .find((f) => f.properties.h3 === this.drawerH3);
      if (!feature) return null;
      const ring = feature.geometry.coordinates[0];
      const sum = ring.reduce((acc, [lon, lat]) => [acc[0] + lon, acc[1] + lat], [0, 0]);
      return { lat: sum[1] / ring.length, lon: sum[0] / ring.length };
    }

    async showNearbyVolunteers(url, center) {
      const target = this.q("[data-twin-drawer-reports]");
      this.q("[data-twin-reports-title]").textContent = "Volunteers near this cell";
      target.innerHTML = "Searching…";
      try {
        const response = await fetch(
          url + "?lat=" + center.lat.toFixed(6) + "&lng=" + center.lon.toFixed(6) +
          "&radius_km=25");
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json();
        target.innerHTML = data.volunteers.length
          ? data.volunteers.slice(0, 10).map((volunteer) =>
              '<span class="twin-asset-chip">' + escapeHtml(volunteer.name) + " · " +
              volunteer.distance_km + " km" +
              (volunteer.is_verified ? " ✓" : "") + "</span>").join("") +
            '<div class="twin-muted" style="margin-top:6px">' + data.count +
            ' within 25 km. Assign them from the <a href="/coordination/volunteers/assign" ' +
            'style="color:#7dd3fc">volunteer console</a>.</div>'
          : '<span class="twin-muted">No registered volunteers within 25 km of this cell.</span>';
      } catch (err) {
        target.innerHTML = '<span class="twin-muted">Volunteer lookup failed (' +
          escapeHtml(err.message) + ").</span>";
      }
    }

    // -- toast ----------------------------------------------------------

    toast(message) {
      const node = this.q("[data-twin-toast]");
      node.textContent = message;
      node.classList.add("show");
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => node.classList.remove("show"), 3200);
    }
  }

  // ------------------------------------------------------------------
  // Boot: one console per page, started when it first becomes visible.
  // ------------------------------------------------------------------

  function boot() {
    const root = document.querySelector("[data-twin-console]");
    if (!root || root.dataset.twinBooted) return;
    root.dataset.twinBooted = "1";

    const console_ = new TwinConsole(root);
    global.twinConsole = console_;

    if (root.dataset.autostart === "now" || typeof IntersectionObserver === "undefined") {
      console_.start();
      return;
    }

    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        console_.start();
      }
    }, { rootMargin: "200px" });
    observer.observe(root);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  global.TwinConsole = TwinConsole;
})(window);
