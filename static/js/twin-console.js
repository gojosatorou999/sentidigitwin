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
        { id: "radar", label: "Rain radar", checked: false },
        { id: "traffic", label: "Traffic", checked: false },
      ],
    },
  ];

  //: Toggles whose layer does not exist until the handler creates it.
  const RASTER_LAYERS = new Set(["radar", "traffic"]);
  //: Toggles that own several layers and fetch their data on first use.
  const LAZY_LAYERS = new Set(["cctv", "water"]);
  const LAZY_LAYER_IDS = {
    cctv: ["cctv", "cctv-direction"],
    water: ["water-bodies", "water-bodies-outline",
            "water-drains-glow", "water-drains"],
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
        pane.startStream((slug_) => this.onIncidentEvent(slug_));
        this.wireCameraSync(pane);
        this.syncLayerPanel(pane);
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
        if (RASTER_LAYERS.has(layerId) || LAZY_LAYERS.has(layerId)) return;
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
      this.drawer.classList.remove("open");
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

      const showShot = (index) => {
        const shot = shots[index];
        body.querySelector("[data-street-stage]").innerHTML =
          '<img src="' + escapeHtml(shot.image_url || shot.thumb_url) + '" alt="Street-level view">' +
          '<div class="twin-street-caption"><span>' +
          escapeHtml(shot.title || shot.provider) +
          (shot.captured_at ? " · " + escapeHtml(shot.captured_at) : "") + "</span>" +
          (shot.permalink
            ? '<a href="' + escapeHtml(shot.permalink) +
              '" target="_blank" rel="noopener" style="color:#7dd3fc">source</a>'
            : "") + "</div>";
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
