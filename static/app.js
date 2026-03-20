/* SafeRoute / SafePath – Google-Maps-like routing (safety optimized)
 *
 * NAVIGATION SYSTEM OVERVIEW:
 * --------------------------
 * 1. ROUTE GENERATION: User enters start/destination via search or map click.
 *    - Geocoding: Addresses are converted to lat/lng via /api/geocode (Nominatim)
 *    - Routing: OSRM returns multiple routes with distance (m), duration (s), geometry
 *    - Safety: Backend scores each route using 8 metrics from safety dataset
 *    - Distance & ETA come directly from OSRM API (no manual calculation)
 *
 * 2. LIVE NAVIGATION: When "Start Navigation" is pressed:
 *    - Geolocation API watches user position (updates every 1-3 seconds)
 *    - User marker moves on map; remaining distance/ETA update in real time
 *    - ETA = (API duration) × (remaining distance / total distance)
 *    - Current speed computed from position delta over time
 *    - Next waypoint from OSRM steps or fallback "Head toward destination"
 *
 * 3. ROUTE RECALCULATION: If user deviates >80m from route:
 *    - Automatically requests new route from current position to destination
 *    - Preserves selected route type (Safest/Balanced/Fastest)
 *    - Min 8 seconds between recalculations to avoid thrashing
 *
 * 4. ASYNC & PERFORMANCE: All API calls are async; no full page reloads;
 *    map updates use smooth pan/fitBounds; routes rendered via MapLibre GeoJSON.
 */

(function () {
  // ---------------------------
  // Helpers (formatting + DOM)
  // ---------------------------

  /** @param {string} id */
  function $(id) { return document.getElementById(id); }

  /** @param {number} m */
  function fmtKm(m) { return (m / 1000).toFixed(1) + " km"; }

  /** @param {number} s */
  function fmtMin(s) { return Math.max(1, Math.round(s / 60)) + " min"; }

  /** route_score is now 0..100 already */
  function safetyPct(score0to100) { return Math.round(score0to100); }

  /** @param {"safe"|"moderate"|"unsafe"} zone */
  function zoneColor(zone) {
    if (zone === "safe") return "#29ff9a";
    if (zone === "moderate") return "#ffd35a";
    return "#ff4d6d";
  }

  /** Route type accent colors */
  function routeColor(key) {
    if (key === "Safest Route")   return "#20e37f";
    if (key === "Balanced Route") return "#e3b62f";
    if (key === "Fastest Route")  return "#e33456";
    return "#20e37f";
  }

  /** Route type icons */
  function routeIcon(key) {
    if (key === "Safest Route")   return "🛡️";
    if (key === "Balanced Route") return "⚖️";
    if (key === "Fastest Route")  return "⚡";
    return "📍";
  }

  /** Simple debounce utility */
  function debounce(fn, waitMs) {
    let t = null;
    return function (...args) {
      if (t) window.clearTimeout(t);
      t = window.setTimeout(() => fn.apply(this, args), waitMs);
    };
  }

  // -----------------------------------
  // State (markers, selections, routes)
  // -----------------------------------

  const state = {
    start: null, // {lat,lng,label}
    end: null,   // {lat,lng,label}
    lastClick: null,
    heatEnabled: true,
    routes: [], // backend /api/routes response (array)
    aiBest: null,
    selectedRouteKey: "Safest Route",
    navigating: false,
    watchId: null,
    nav: {
      active: null, // {key, route}
      coordsLatLng: [], // [[lat,lng],...]
      cumDistM: [], // cumulative distances along coordsLatLng
      startedAtMs: null,
      lastRerouteAtMs: 0,
    },
  };

  const ui = {
    status: $("status"),
    cards: $("route-cards"),
    incidents: $("incident-list"),
    bnCurrent: $("bn-current") || { textContent: "" },
    bnDest: $("bn-dest") || { textContent: "" },
    bnReco: $("bn-reco"),
    bnEta: $("bn-eta"),
    bnRemaining: $("bn-remaining"),
    bnSpeed: $("bn-speed") || { textContent: "" },
    bnNext: $("bn-next"),
    bnScore: $("bn-score"),
    inputStart: $("input-current"),
    inputEnd: $("input-dest"),
    sugStart: $("suggest-current"),
    sugEnd: $("suggest-dest"),
  };

  /** Speed tracking for live navigation (m/s) */
  let lastNavPos = null;
  let lastNavTime = null;

  function setStatus(msg) {
    ui.status.textContent = msg;
  }

  // ---------------------------
  // Navigation math helpers
  // ---------------------------

  function toRad(d) { return d * Math.PI / 180; }

  /** Haversine distance between 2 lat/lng points in meters */
  function haversineM(aLat, aLng, bLat, bLng) {
    const R = 6371000;
    const dLat = toRad(bLat - aLat);
    const dLng = toRad(bLng - aLng);
    const s1 = Math.sin(dLat / 2);
    const s2 = Math.sin(dLng / 2);
    const aa = s1 * s1 + Math.cos(toRad(aLat)) * Math.cos(toRad(bLat)) * s2 * s2;
    return 2 * R * Math.atan2(Math.sqrt(aa), Math.sqrt(1 - aa));
  }

  /** Build cumulative distance array for route coords */
  function buildCumDist(coordsLatLng) {
    const cum = [0];
    for (let i = 1; i < coordsLatLng.length; i++) {
      const a = coordsLatLng[i - 1];
      const b = coordsLatLng[i];
      cum.push(cum[i - 1] + haversineM(a[0], a[1], b[0], b[1]));
    }
    return cum;
  }

  /** Nearest coordinate index (fast approximation) */
  function nearestIndex(coordsLatLng, lat, lng) {
    let bestI = 0;
    let bestD = Infinity;
    const step = coordsLatLng.length > 1200 ? 6 : coordsLatLng.length > 600 ? 4 : coordsLatLng.length > 250 ? 2 : 1;
    for (let i = 0; i < coordsLatLng.length; i += step) {
      const c = coordsLatLng[i];
      const d = haversineM(c[0], c[1], lat, lng);
      if (d < bestD) { bestD = d; bestI = i; }
    }
    const start = Math.max(0, bestI - step * 2);
    const end = Math.min(coordsLatLng.length - 1, bestI + step * 2);
    for (let i = start; i <= end; i++) {
      const c = coordsLatLng[i];
      const d = haversineM(c[0], c[1], lat, lng);
      if (d < bestD) { bestD = d; bestI = i; }
    }
    return { idx: bestI, distM: bestD };
  }

  // ---------------------------
  // MapLibre map initialization
  // ---------------------------

  const map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
      sources: {
        osm: {
          type: "raster",
          tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
          tileSize: 256,
          attribution: "© OpenStreetMap contributors",
        },
      },
      layers: [
        {
          id: "osm-tiles",
          type: "raster",
          source: "osm",
        },
      ],
    },
    center: [78.4867, 17.385],
    zoom: 12,
  });

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

  if (map.dragPan && typeof map.dragPan.enable === "function") map.dragPan.enable();
  if (map.keyboard && typeof map.keyboard.enable === "function") map.keyboard.enable();

  let startMarker = null;
  let endMarker = null;
  let currentMarker = null;
  let heatmapSourceId = "safety-heatmap";
  let heatmapLayerId = "safety-heatmap-layer";
  let safetySourceId = "safety-points";
  let safetyClusterLayerId = "safety-clusters";
  let safetyPointsLayerId = "safety-points-layer";
  let safetyClusterCountLayerId = "safety-cluster-count";
  let routeSourceIds = [];
  let routeLayerIds = [];
  let arrowsLayerId = "route-arrows";

  function checkUserProfile() {
    let user = localStorage.getItem("safe_user");
    if (!user) {
      document.getElementById("profileModal").style.display = "flex";
    }
  }

  function clearRoutes() {
    routeLayerIds.forEach((id) => {
      if (map.getLayer(id)) map.removeLayer(id);
    });
    routeLayerIds = [];
    routeSourceIds.forEach((id) => {
      if (map.getSource(id)) map.removeSource(id);
    });
    routeSourceIds = [];
    if (map.getLayer(arrowsLayerId)) map.removeLayer(arrowsLayerId);
    if (map.getSource("route-arrows")) map.removeSource("route-arrows");
  }

  function setStartMarker(latlng, label) {
    if (startMarker) startMarker.remove();
    const el = document.createElement("div");
    el.className = "maplibre-marker maplibre-marker--start";
    el.style.width = "16px";
    el.style.height = "16px";
    el.style.borderRadius = "50%";
    el.style.backgroundColor = "#45a3ff";
    el.style.border = "2px solid #45a3ff";
    el.style.opacity = "0.9";
    startMarker = new maplibregl.Marker({ element: el })
      .setLngLat(Array.isArray(latlng) ? [latlng[1], latlng[0]] : [latlng.lng, latlng.lat])
      .setPopup(new maplibregl.Popup({ offset: 15 }).setHTML(label || "Start"))
      .addTo(map);
  }

  function setEndMarker(latlng, label) {
    if (endMarker) endMarker.remove();
    const el = document.createElement("div");
    el.className = "maplibre-marker maplibre-marker--end";
    el.style.width = "16px";
    el.style.height = "16px";
    el.style.borderRadius = "50%";
    el.style.backgroundColor = "#ff4d6d";
    el.style.border = "2px solid #ff4d6d";
    el.style.opacity = "0.9";
    endMarker = new maplibregl.Marker({ element: el })
      .setLngLat(Array.isArray(latlng) ? [latlng[1], latlng[0]] : [latlng.lng, latlng.lat])
      .setPopup(new maplibregl.Popup({ offset: 15 }).setHTML(label || "Destination"))
      .addTo(map);
  }

  function fitToStartEnd() {
    if (!state.start || !state.end) return;
    const padding = 80;
    map.fitBounds(
      [
        [state.start.lng, state.start.lat],
        [state.end.lng, state.end.lat],
      ],
      { padding, duration: 400 }
    );
  }

  map.on("load", () => {
    checkUserProfile();
    map.on("click", (e) => {
      state.lastClick = { lat: e.lngLat.lat, lng: e.lngLat.lng };
      if (!state.start) {
        state.start = { lat: e.lngLat.lat, lng: e.lngLat.lng, label: "Start (map click)" };
        ui.inputStart.value = "Current Location (map click)";
        setStartMarker([e.lngLat.lat, e.lngLat.lng], "Start");
      } else if (!state.end) {
        state.end = { lat: e.lngLat.lat, lng: e.lngLat.lng, label: "Destination (map click)" };
        ui.inputEnd.value = "Destination (map click)";
        setEndMarker([e.lngLat.lat, e.lngLat.lng], "Destination");
        fitToStartEnd();
      }
    });

    (async function boot() {
      try {
        setStatus("Loading safety dataset…");
        await loadSafetyPoints();
        setStatus("Ready. Searching locations is enabled. Detecting live location…");
        detectLiveLocation();
      } catch (e) {
        setStatus("Failed to initialize. Check server logs and network.");
      }
    })();
  });

  // ---------------------------
  // Safety dataset visualization
  // ---------------------------

  function safetyPercentForPoint(p) {
    const crime = Number(p.crime_rate ?? 5);
    const lighting = Number(p.street_lighting ?? 5);
    const crowd = Number(p.crowd_density ?? 5);
    const police = Number(p.police_proximity ?? 5);
    const cctv = Number(p.cctv_coverage ?? 5);
    const visibility = Number(p.road_visibility ?? 5);
    const traffic = Number(p.traffic_density ?? 5);
    const incidents = Number(p.incident_reports ?? 3);

    const raw =
      0.25 * lighting +
      0.15 * crowd +
      0.10 * police +
      0.10 * cctv +
      0.10 * visibility +
      0.10 * traffic -
      0.15 * crime -
      0.05 * incidents;

    const rawMin = -1.2;
    const rawMax = 7.85;
    const pct = ((raw - rawMin) / (rawMax - rawMin)) * 100;
    return Math.round(Math.max(0, Math.min(100, pct)));
  }

  function safetyBand(pct) {
    if (pct >= 70) return { band: "safe", color: "#29ff9a" };
    if (pct >= 40) return { band: "moderate", color: "#ffd35a" };
    return { band: "unsafe", color: "#ff4d6d" };
  }

  async function loadSafetyPoints() {
    const res = await fetch("/api/safety_points");
    const data = await res.json();

    const pointsFeatures = [];
    const heatFeatures = [];

    data.points.forEach((p) => {
      const pct = (typeof p.safety_percent === "number") ? Math.round(p.safety_percent) : safetyPercentForPoint(p);
      const band = safetyBand(pct);

      pointsFeatures.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [p.lng, p.lat] },
        properties: {
          color: band.color,
          pct,
          band: band.band,
          area: p.area || p.name || "Location #" + (p.id ?? ""),
          crime_rate: p.crime_rate ?? "—",
          street_lighting: p.street_lighting ?? "—",
          crowd_density: p.crowd_density ?? "—",
        },
      });

      const intensity = Math.max(0.05, (100 - pct) / 100);
      heatFeatures.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [p.lng, p.lat] },
        properties: { intensity },
      });
    });

    const pointsGeoJSON = { type: "FeatureCollection", features: pointsFeatures };
    const heatGeoJSON = { type: "FeatureCollection", features: heatFeatures };

    if (map.getSource(safetySourceId)) map.removeSource(safetySourceId);
    if (map.getLayer(safetyClusterCountLayerId)) map.removeLayer(safetyClusterCountLayerId);
    if (map.getLayer(safetyPointsLayerId)) map.removeLayer(safetyPointsLayerId);
    if (map.getLayer(safetyClusterLayerId)) map.removeLayer(safetyClusterLayerId);

    map.addSource(safetySourceId, {
      type: "geojson",
      data: pointsGeoJSON,
      cluster: true,
      clusterMaxZoom: 14,
      clusterRadius: 45,
    });

    map.addLayer({
      id: safetyClusterLayerId,
      type: "circle",
      source: safetySourceId,
      filter: ["has", "point_count"],
      paint: {
        "circle-color": "#6cf6ff",
        "circle-radius": ["step", ["get", "point_count"], 18, 10, 22, 30, 26],
        "circle-stroke-width": 1,
        "circle-stroke-color": "#e8efff",
      },
    });

    map.addLayer({
      id: safetyClusterCountLayerId,
      type: "symbol",
      source: safetySourceId,
      filter: ["has", "point_count"],
      layout: {
        "text-field": ["get", "point_count_abbreviated"],
        "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
        "text-size": 12,
      },
      paint: { "text-color": "#05060b" },
    });

    map.addLayer({
      id: safetyPointsLayerId,
      type: "circle",
      source: safetySourceId,
      filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-color": ["get", "color"],
        "circle-radius": 7,
        "circle-stroke-width": 2,
        "circle-stroke-color": "#ffffff",
      },
    });

    map.on("click", safetyClusterLayerId, (e) => {
      const features = map.queryRenderedFeatures(e.point, { layers: [safetyClusterLayerId] });
      if (!features.length) return;
      const clusterId = features[0].properties.cluster_id;
      const source = map.getSource(safetySourceId);
      source.getClusterExpansionZoom(clusterId, (err, zoom) => {
        if (!err) map.flyTo({ center: features[0].geometry.coordinates, zoom });
      });
    });

    map.on("click", safetyPointsLayerId, (e) => {
      const props = e.features[0].properties;
      const coords = e.features[0].geometry.coordinates.slice();
      const popupHtml =
        "<b>" + props.area + "</b><br/>Safety: <b>" + props.pct + "%</b> (" + props.band + ")" +
        "<br/>Crime level: " + props.crime_rate + "<br/>Lighting level: " + props.street_lighting +
        "<br/>Crowd density: " + props.crowd_density;
      new maplibregl.Popup().setLngLat(coords).setHTML(popupHtml).addTo(map);
    });

    map.on("mouseenter", safetyClusterLayerId, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", safetyClusterLayerId, () => { map.getCanvas().style.cursor = ""; });
    map.on("mouseenter", safetyPointsLayerId, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", safetyPointsLayerId, () => { map.getCanvas().style.cursor = ""; });

    if (map.getSource(heatmapSourceId)) map.removeSource(heatmapSourceId);
    if (map.getLayer(heatmapLayerId)) map.removeLayer(heatmapLayerId);

    map.addSource(heatmapSourceId, { type: "geojson", data: heatGeoJSON });
    map.addLayer(
      {
        id: heatmapLayerId,
        type: "heatmap",
        source: heatmapSourceId,
        maxzoom: 17,
        paint: {
          "heatmap-weight": ["get", "intensity"],
          "heatmap-intensity": 1,
          "heatmap-color": [
            "interpolate",
            ["linear"],
            ["heatmap-density"],
            0, "rgba(0,0,0,0)",
            0.2, "rgba(255,77,109,0.3)",
            0.5, "rgba(255,211,90,0.5)",
            0.8, "rgba(41,255,154,0.6)",
            1, "rgba(108,246,255,0.8)",
          ],
          "heatmap-radius": 28,
          "heatmap-opacity": state.heatEnabled ? 0.7 : 0,
        },
      },
      safetyClusterLayerId
    );
  }

  // ---------------------------
  // Live location detection
  // ---------------------------

  function detectLiveLocation() {
    if (!navigator.geolocation) {
      setStatus("Geolocation not supported in this browser.");
      return;
    }

    const options = { enableHighAccuracy: true, timeout: 12000, maximumAge: 1000 };

    if (state.watchId !== null) {
      try { navigator.geolocation.clearWatch(state.watchId); } catch (_) {}
      state.watchId = null;
    }

    state.watchId = navigator.geolocation.watchPosition(
      (pos) => {
        const lat = pos.coords.latitude;
        const lng = pos.coords.longitude;

        if (currentMarker) currentMarker.remove();
        const el = document.createElement("div");
        el.style.width = "14px";
        el.style.height = "14px";
        el.style.borderRadius = "50%";
        el.style.backgroundColor = "#6cf6ff";
        el.style.border = "2px solid #6cf6ff";
        el.style.opacity = "0.9";
        currentMarker = new maplibregl.Marker({ element: el })
          .setLngLat([lng, lat])
          .addTo(map);

        if (!state.start || state.start.label === "Current Location") {
          state.start = { lat, lng, label: "Current Location" };
          ui.inputStart.value = "Current Location";
          setStartMarker([lat, lng], "Start: Current Location");
        }

        if (state.navigating && state.nav.active) {
          updateNavigation(lat, lng);
        }
      },
      () => {
        setStatus("Location permission denied. You can still search manually.");
      },
      options
    );
  }

  // ---------------------------
  // Autocomplete (Nominatim)
  // ---------------------------

  let startAbort = null;
  let endAbort = null;

  async function fetchSuggestions(q, abortController) {
    let url = "/api/autocomplete?q=" + encodeURIComponent(q);
    if (state.start && typeof state.start.lat === "number" && typeof state.start.lng === "number") {
      url += "&near_lat=" + encodeURIComponent(state.start.lat) + "&near_lng=" + encodeURIComponent(state.start.lng);
    }
    const res = await fetch(url, { signal: abortController.signal });
    const data = await res.json();
    return data;
  }

  function renderSuggestions(container, payload, onPick) {
    container.innerHTML = "";
    const results = (payload && payload.results) ? payload.results : [];
    if (!results.length) {
      container.classList.remove("suggest--open");
      return;
    }

    if (payload && payload.message) {
      const msg = document.createElement("div");
      msg.className = "suggest__msg";
      msg.textContent = payload.message;
      container.appendChild(msg);
    }

    results.forEach((item) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "suggest__item";
      const name = item.name || (item.display_name ? item.display_name.split(",")[0] : "Place");
      const addr = item.display_name || "";
      row.innerHTML =
        '<div class="suggest__name">' + name + "</div>" +
        '<div class="suggest__meta">' + addr + "</div>";
      row.addEventListener("click", () => onPick(item));
      container.appendChild(row);
    });

    container.classList.add("suggest--open");
  }

  const debouncedStartSuggest = debounce(async () => {
    const q = ui.inputStart.value.trim();
    if (q.length < 2) { renderSuggestions(ui.sugStart, { results: [] }, () => {}); return; }
    if (startAbort) startAbort.abort();
    startAbort = new AbortController();
    try {
      const payload = await fetchSuggestions(q, startAbort);
      renderSuggestions(ui.sugStart, payload, (item) => {
        ui.inputStart.value = item.display_name;
        state.start = { lat: item.lat, lng: item.lng, label: item.display_name };
        setStartMarker([item.lat, item.lng], "Start");
        renderSuggestions(ui.sugStart, { results: [] }, () => {});
        map.flyTo({ center: [item.lng, item.lat], zoom: Math.max(14, map.getZoom()) });
        fitToStartEnd();
      });
    } catch (e) {}
  }, 300);

  const debouncedEndSuggest = debounce(async () => {
    const q = ui.inputEnd.value.trim();
    if (q.length < 2) { renderSuggestions(ui.sugEnd, { results: [] }, () => {}); return; }
    if (endAbort) endAbort.abort();
    endAbort = new AbortController();
    try {
      const payload = await fetchSuggestions(q, endAbort);
      renderSuggestions(ui.sugEnd, payload, (item) => {
        ui.inputEnd.value = item.display_name;
        state.end = { lat: item.lat, lng: item.lng, label: item.display_name };
        setEndMarker([item.lat, item.lng], "Destination");
        renderSuggestions(ui.sugEnd, { results: [] }, () => {});
        map.flyTo({ center: [item.lng, item.lat], zoom: Math.max(14, map.getZoom()) });
        fitToStartEnd();
      });
    } catch (e) {}
  }, 300);

  document.addEventListener("click", (e) => {
    const t = e.target;
    if (!t.closest || (!t.closest("#start-wrap") && !t.closest("#dest-wrap"))) {
      ui.sugStart.classList.remove("suggest--open");
      ui.sugEnd.classList.remove("suggest--open");
    }
  });

  ui.inputStart.addEventListener("input", debouncedStartSuggest);
  ui.inputEnd.addEventListener("input", debouncedEndSuggest);

  ui.inputStart.addEventListener("input", () => {
    if (state.start && ui.inputStart.value.trim() !== (state.start.label || "").trim()) {
      state.start = null;
      if (startMarker) { startMarker.remove(); startMarker = null; }
    }
  });
  ui.inputEnd.addEventListener("input", () => {
    if (state.end && ui.inputEnd.value.trim() !== (state.end.label || "").trim()) {
      state.end = null;
      if (endMarker) { endMarker.remove(); endMarker = null; }
    }
  });

  // ---------------------------
  // Weights / filters (frontend)
  // ---------------------------

  function buildWeights() {
    const on = (id) => $(id).checked;
    return {
      street_lighting:  on("t-light")  ? 0.25 : 0.05,
      crowd_density:    on("t-crowd")  ? 0.15 : 0.05,
      police_proximity: on("t-police") ? 0.10 : 0.03,
      cctv_coverage:    on("t-cctv")   ? 0.10 : 0.03,
      road_visibility:  0.10,
      traffic_density:  0.10,
      crime_rate:       on("t-crime")  ? 0.15 : 0.08,
      incident_reports: 0.05,
    };
  }

  // ---------------------------
  // Routing (backend /api/routes)
  // ---------------------------

  /**
   * Build labeled routes from backend response.
   * Backend now returns route_label on each route, so we prefer that.
   * Falls back to legacy sort-based logic for backwards compat.
   */
  function buildLabeledRoutes(routes) {
    if (!routes || !routes.length) return [];

    // New backend: route_label is already set
    if (routes[0] && routes[0].route_label) {
      return routes.map((r) => ({ key: r.route_label, route: r }));
    }

    // Legacy fallback
    const safest  = [...routes].sort((a, b) => b.route_score - a.route_score)[0];
    const fastest = [...routes].sort((a, b) => a.duration_s - b.duration_s)[0];
    if (routes.length === 1) return [{ key: "Route", route: routes[0] }];
    if (routes.length === 2) return [
      { key: "Safest Route",  route: safest  },
      { key: "Fastest Route", route: fastest },
    ];
    const balanced = routes.find((r) => r !== safest && r !== fastest) || routes[1];
    return [
      { key: "Safest Route",   route: safest   },
      { key: "Balanced Route", route: balanced },
      { key: "Fastest Route",  route: fastest  },
    ];
  }

  async function geocodeAddress(query) {
    const res = await fetch("/api/geocode?q=" + encodeURIComponent(query));
    if (!res.ok) return null;
    const data = await res.json();
    return data;
  }

  async function fetchRoutesAndScores() {
    const startInput = ui.inputStart.value.trim();
    const endInput   = ui.inputEnd.value.trim();

    if (!state.start && startInput.length >= 2) {
      const geo = await geocodeAddress(startInput);
      if (geo) {
        state.start = { lat: geo.lat, lng: geo.lng, label: geo.display_name || startInput };
        setStartMarker([geo.lat, geo.lng], "Start");
      }
    }
    if (!state.end && endInput.length >= 2) {
      const geo = await geocodeAddress(endInput);
      if (geo) {
        state.end = { lat: geo.lat, lng: geo.lng, label: geo.display_name || endInput };
        setEndMarker([geo.lat, geo.lng], "Destination");
      }
    }

    if (!state.start || !state.end) {
      setStatus("Select start and destination using search (or click on map).");
      return;
    }

    setStatus("Finding routes and scoring safety…");
    clearRoutes();

    const payload = {
      start:          { lat: state.start.lat, lng: state.start.lng },
      end:            { lat: state.end.lat,   lng: state.end.lng   },
      weights:        buildWeights(),
      max_distance_m: 280,
    };

    let res;
    try {
      res = await fetch("/api/routes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      setStatus("Network error while routing. Try again in a few seconds.");
      return;
    }

    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const msg = (data && data.error) ? data.error : "Routing failed";
      setStatus(msg + ". Please try again.");
      return;
    }
    if (!data || !data.routes || !data.routes.length) {
      setStatus("No routes found. Try different locations.");
      return;
    }

    state.routes = data.routes;
    state.aiBest = data.ai_recommendation || null;

    const labeled = buildLabeledRoutes(data.routes);

    renderRouteCards(labeled);
    drawRoutes(labeled);
    state.selectedRouteKey = labeled[0].key;
    focusRoute(labeled[0]);

    fitToStartEnd();
    setStatus(
      "✅ " + labeled.length + " routes found — " +
      labeled.map(l => l.key + " (" + Math.round(l.route.route_score) + "%)").join(" · ")
    );
  }

  // -----------------------------------------------
  // UI rendering — Route Cards with full metrics
  // -----------------------------------------------

  /**
   * Render the 3 route cards with complete metrics breakdown.
   * Each card shows:
   *  - Route label + icon + zone badge
   *  - Safety score bar
   *  - Distance · ETA · Safety %
   *  - Per-mode travel times (Car / Bike / Walk / Truck)
   *  - Nearby safety point count
   */
  function renderRouteCards(labeled) {
    ui.cards.innerHTML = "";

    labeled.forEach((item, cardIdx) => {
      const r     = item.route;
      const pct   = safetyPct(r.route_score);
      const zColor = zoneColor(r.zone);
      const rColor = routeColor(item.key);
      const icon  = routeIcon(item.key);
      const isSelected = item.key === state.selectedRouteKey;

      // ── Card wrapper ──────────────────────────────────────────────────
      const card = document.createElement("div");
      card.className = "card card--route" + (isSelected ? " card--selected" : "");
      card.dataset.routeKey = item.key;
      card.style.setProperty("--route-color", rColor);
      card.style.cursor = "pointer";

      // ── Header row ───────────────────────────────────────────────────
      const head = document.createElement("div");
      head.className = "card__head";
      head.innerHTML =
        '<div class="card__title">' +
          '<span class="route-icon">' + icon + '</span>' +
          '<span>' + item.key + '</span>' +
        '</div>' +
        '<div class="chip-badge" style="border-color:' + zColor + ';color:' + zColor + '">' +
          r.zone.toUpperCase() +
        '</div>';
      card.appendChild(head);

      // ── Safety score bar ─────────────────────────────────────────────
      const barWrap = document.createElement("div");
      barWrap.className = "bar";
      barWrap.innerHTML =
        '<div class="bar__fill" style="width:' + pct + '%;background:' + rColor + '"></div>';
      card.appendChild(barWrap);

      // ── Primary metrics row ──────────────────────────────────────────
      const meta1 = document.createElement("div");
      meta1.className = "card__meta";
      meta1.innerHTML =
        '<span class="pill">📍 ' + fmtKm(r.distance_m) + '</span>' +
        '<span class="pill">⏱ ' + fmtMin(r.duration_s) + '</span>' +
        '<span class="pill pill--accent" style="border-color:' + zColor + ';color:' + zColor + '">' +
          '🛡 ' + pct + '%' +
        '</span>';
      card.appendChild(meta1);

      // ── Per-mode travel times ────────────────────────────────────────
      const modes = r.duration_by_mode_s || {};
      const meta2 = document.createElement("div");
      meta2.className = "card__meta card__meta--modes";
      meta2.innerHTML =
        '<span class="pill pill--mode">🚗 ' + fmtMin(modes.car   || r.duration_s) + '</span>' +
        '<span class="pill pill--mode">🚲 ' + fmtMin(modes.bike  || r.duration_s) + '</span>' +
        '<span class="pill pill--mode">🚶 ' + fmtMin(modes.walk  || r.duration_s) + '</span>' +
        '<span class="pill pill--mode">🚛 ' + fmtMin(modes.truck || r.duration_s) + '</span>';
      card.appendChild(meta2);

      // ── Metrics detail grid ──────────────────────────────────────────
      const detail = document.createElement("div");
      detail.className = "card__detail";
      const nearbyCount = r.nearby_count != null ? r.nearby_count : (r.worst_points ? r.worst_points.length : "—");
      const worstZone = (r.worst_points && r.worst_points.length)
        ? r.worst_points[0].zone || "—"
        : "—";

      detail.innerHTML =
        '<div class="metric-row">' +
          '<span class="metric-label">Safety points scanned</span>' +
          '<span class="metric-value">' + nearbyCount + '</span>' +
        '</div>' +
        '<div class="metric-row">' +
          '<span class="metric-label">Highest risk zone</span>' +
          '<span class="metric-value" style="color:' + zoneColor(worstZone) + '">' +
            worstZone.toUpperCase() +
          '</span>' +
        '</div>' +
        '<div class="metric-row">' +
          '<span class="metric-label">AI note</span>' +
          '<span class="metric-value metric-value--note">' + (r.ai_message || "—") + '</span>' +
        '</div>';
      card.appendChild(detail);

      // ── Select this route on click ───────────────────────────────────
      card.addEventListener("click", () => {
        state.selectedRouteKey = item.key;
        [...ui.cards.querySelectorAll(".card--route")].forEach((c) =>
          c.classList.remove("card--selected")
        );
        card.classList.add("card--selected");
        focusRoute(item);
        drawRoutes(labeled);
      });

      ui.cards.appendChild(card);
    });
  }

  function drawRoutes(labeled) {
    clearRoutes();
    // Draw non-selected routes first (thinner, under selected)
    const order = [...labeled].sort((a, b) => {
      const aS = a.key === state.selectedRouteKey ? 1 : 0;
      const bS = b.key === state.selectedRouteKey ? 1 : 0;
      return aS - bS;
    });

    order.forEach((item, idx) => {
      const r = item.route;
      const color = routeColor(item.key);
      const isSelected = item.key === state.selectedRouteKey;
      const sourceId = "route-" + idx;
      const layerId  = "route-layer-" + idx;
      if (!r.geometry || !r.geometry.coordinates) return;

      map.addSource(sourceId, {
        type: "geojson",
        data: { type: "Feature", properties: {}, geometry: r.geometry },
      });
      map.addLayer({
        id: layerId,
        type: "line",
        source: sourceId,
        layout: { "line-join": "round", "line-cap": "round" },
        paint: {
          "line-color":   color,
          "line-width":   isSelected ? 7 : 3,
          "line-opacity": isSelected ? 0.95 : 0.55,
        },
      });
      routeSourceIds.push(sourceId);
      routeLayerIds.push(layerId);
    });

    // Dashed overlay on selected route
    const selected = labeled.find((x) => x.key === state.selectedRouteKey) || labeled[0];
    if (selected && selected.route.geometry && selected.route.geometry.coordinates) {
      if (map.getSource("route-arrows")) map.removeSource("route-arrows");
      if (map.getLayer(arrowsLayerId))   map.removeLayer(arrowsLayerId);
      map.addSource("route-arrows", {
        type: "geojson",
        data: { type: "Feature", properties: {}, geometry: selected.route.geometry },
      });
      map.addLayer({
        id: arrowsLayerId,
        type: "line",
        source: "route-arrows",
        layout: { "line-join": "round", "line-cap": "round" },
        paint: {
          "line-color":     "#e8efff",
          "line-width":     1.5,
          "line-opacity":   0.35,
          "line-dasharray": [2, 2],
        },
      });
      routeLayerIds.push(arrowsLayerId);
    }
  }

  function renderIncidents(routeObj) {
    const worst = (routeObj.worst_points || []).slice(0, 8);
    if (!worst.length) {
      ui.incidents.innerHTML = '<div class="incident incident--muted">No nearby high‑risk points detected.</div>';
      return;
    }

    ui.incidents.innerHTML = "";
    worst.forEach((p) => {
      const scorePct =
        (typeof p.safety_percent === "number")
          ? Math.round(p.safety_percent)
          : (typeof p.safety_score === "number")
            ? Math.round(p.safety_score * 10)
            : null;

      const div = document.createElement("div");
      div.className = "incident";
      div.innerHTML =
        '<div class="incident__row">' +
          '<div class="incident__title">' + (p.area || ("Point #" + p.id)) + "</div>" +
          '<div class="incident__score">' + (scorePct === null ? "—" : (scorePct + "%")) + "</div>" +
        "</div>" +
        '<div class="incident__sub">Distance: ' + p.distance_to_route_m + " m • Zone: " + p.zone + "</div>";
      div.addEventListener("click", () => map.flyTo({ center: [p.lng, p.lat], zoom: 16 }));
      ui.incidents.appendChild(div);
    });
  }

  function focusRoute(item) {
    const r = item.route;
    ui.bnReco.textContent = (r.ai_message || item.key).slice(0, 80) + (r.ai_message && r.ai_message.length > 80 ? "…" : "");
    const totalDuration = r.duration_s || 0;
    const now = new Date();
    const arrive = new Date(now.getTime() + totalDuration * 1000);
    const h = arrive.getHours();
    const m = arrive.getMinutes();
    ui.bnEta.textContent = (h % 12 || 12) + ":" + String(m).padStart(2, "0") + (h >= 12 ? " PM" : " AM");
    ui.bnRemaining.textContent = fmtKm(r.distance_m) + " • " + fmtMin(totalDuration);
    ui.bnNext.textContent = extractNextInstruction(r) || "—";
    ui.bnScore.textContent = r.route_score + "% (" + (r.zone || "—").toUpperCase() + ")";
    ui.bnCurrent.textContent = state.start ? (state.start.label || state.start.lat.toFixed(4) + ", " + state.start.lng.toFixed(4)) : "—";
    ui.bnDest.textContent = state.end ? (state.end.label || state.end.lat.toFixed(4) + ", " + state.end.lng.toFixed(4)) : "—";
    renderIncidents(r);
  }

  // ---------------------------
  // Reports
  // ---------------------------

  async function submitReport() {
    if (!state.lastClick) {
      setStatus("Click the map to choose a report location first.");
      return;
    }
    const placeName = ($("report-place") && $("report-place").value) ? $("report-place").value.trim() : "";
    const desc      = $("report-desc").value || "";
    const ratingVal = $("report-rating").value || "";
    const res = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lat:        state.lastClick.lat,
        lng:        state.lastClick.lng,
        place_name: placeName,
        description: desc,
        rating:     ratingVal ? parseInt(ratingVal) : null,
      }),
    });
    const data = await res.json();
    if (data && data.ok) setStatus("Report submitted. Thank you.");
    else setStatus("Report failed: " + (data.error || "unknown"));
  }

  // ---------------------------
  // Buttons / UI events
  // ---------------------------

  $("btn-find").addEventListener("click", fetchRoutesAndScores);
  $("btn-reset").addEventListener("click", () => {
    state.start = null;
    state.end   = null;
    state.routes = [];
    state.aiBest = null;
    state.selectedRouteKey = "Route";
    stopNavigation();
    clearRoutes();
    if (startMarker)   { startMarker.remove();   startMarker   = null; }
    if (endMarker)     { endMarker.remove();      endMarker     = null; }
    ui.inputStart.value = "";
    ui.inputEnd.value   = "";
    ui.bnCurrent.textContent  = "—";
    ui.bnDest.textContent     = "—";
    ui.bnReco.textContent     = "—";
    ui.bnEta.textContent      = "—";
    ui.bnRemaining.textContent = "—";
    ui.bnSpeed.textContent    = "—";
    ui.bnNext.textContent     = "—";
    ui.bnScore.textContent    = "—";
    setStatus("Reset complete. Search for a start and destination.");
  });

  $("btn-heat").addEventListener("click", () => {
    state.heatEnabled = !state.heatEnabled;
    if (!map.getLayer(heatmapLayerId)) return;
    map.setPaintProperty(heatmapLayerId, "heatmap-opacity", state.heatEnabled ? 0.7 : 0);
  });

  $("btn-report").addEventListener("click", submitReport);
  if ($("btn-report-submit")) $("btn-report-submit").addEventListener("click", submitReport);

  $("btn-start-nav").addEventListener("click", () => {
    if (state.navigating) {
      stopNavigation();
      setStatus("Navigation stopped.");
      $("btn-start-nav").textContent = "Start Navigation";
      return;
    }
    startNavigation();
  });

  // ---------------------------
  // Navigation Mode
  // ---------------------------

  function pickSelectedRoute() {
    const labeled = buildLabeledRoutes(state.routes);
    if (!labeled.length) return null;
    const sel = labeled.find((l) => l.key === state.selectedRouteKey) || labeled[0];
    return { key: sel.key, route: sel.route };
  }

  function startNavigation() {
    const picked = pickSelectedRoute();
    if (!picked || !picked.route) {
      setStatus("Generate routes first, then choose a route card.");
      return;
    }
    if (!currentMarker) {
      setStatus("Waiting for live location… allow location permission first.");
      return;
    }

    const coords = (picked.route.geometry && picked.route.geometry.coordinates) ? picked.route.geometry.coordinates : [];
    const coordsLatLng = coords.map((c) => [c[1], c[0]]);
    state.nav.active       = picked;
    state.nav.coordsLatLng = coordsLatLng;
    state.nav.cumDistM     = buildCumDist(coordsLatLng);
    state.nav.startedAtMs  = Date.now();
    state.nav.lastRerouteAtMs = 0;
    state.navigating = true;

    ui.bnCurrent.textContent = "Tracking…";
    ui.bnDest.textContent = state.end ? (state.end.label || state.end.lat.toFixed(4) + ", " + state.end.lng.toFixed(4)) : "—";

    setStatus("Navigation started. Following: " + picked.key);
    $("btn-start-nav").textContent = "Stop Navigation";
  }

  function stopNavigation() {
    state.navigating = false;
    state.nav.active = null;
    state.nav.coordsLatLng = [];
    state.nav.cumDistM = [];
    state.nav.startedAtMs = null;
    state.nav.lastRerouteAtMs = 0;
    lastNavPos  = null;
    lastNavTime = null;
    ui.bnNext.textContent  = "—";
    ui.bnSpeed.textContent = "—";
  }

  function extractStepInstructions(routeObj) {
    const steps = [];
    let distAcc = 0;
    const legs = routeObj.legs || [];
    for (const leg of legs) {
      const legSteps = leg.steps || [];
      for (const st of legSteps) {
        distAcc += (st.distance || 0);
        const text     = (st.instruction || st.ref || "").trim();
        const name     = (st.name || "").trim();
        const m        = st.maneuver || {};
        const type     = (m.type || "").trim();
        const modifier = (m.modifier || "").trim();
        const instr    = text || [type, modifier, name].filter(Boolean).join(" ") || "Continue";
        steps.push({ instruction: instr, distanceFromStart: distAcc });
      }
    }
    return steps;
  }

  function extractNextInstruction(routeObj) {
    const steps = extractStepInstructions(routeObj);
    return steps.length ? steps[0].instruction : "Continue";
  }

  function getNextWaypointForPosition(routeObj, traveledM) {
    const steps = extractStepInstructions(routeObj);
    if (!steps.length) {
      const total = routeObj.distance_m || 9999;
      return (total - traveledM) > 200 ? "Head toward destination" : "Arrive at destination";
    }
    for (const s of steps) {
      if (s.distanceFromStart > traveledM + 30) return s.instruction.slice(0, 50);
    }
    return "Arrive at destination";
  }

  async function rerouteFrom(lat, lng) {
    const now = Date.now();
    if (now - state.nav.lastRerouteAtMs < 8000) return;
    state.nav.lastRerouteAtMs = now;
    if (!state.end) return;
    setStatus("You deviated from the route — recalculating…");
    state.start = { lat, lng, label: "Current Location" };
    ui.inputStart.value = "Current Location";
    setStartMarker([lat, lng], "Start: Current Location");
    const prevKey = state.selectedRouteKey;
    await fetchRoutesAndScores();
    const labeled = buildLabeledRoutes(state.routes);
    const sel = labeled.find((l) => l.key === prevKey) || labeled[0];
    state.selectedRouteKey = sel.key;
    [...ui.cards.querySelectorAll(".card--route")].forEach((c) => {
      c.classList.toggle("card--selected", (c.dataset.routeKey || "") === sel.key);
    });
    focusRoute(sel);
    drawRoutes(labeled);
    startNavigation();
  }

  function updateNavigation(lat, lng) {
    const active = state.nav.active;
    if (!active || !active.route || !state.nav.coordsLatLng.length) return;

    const near        = nearestIndex(state.nav.coordsLatLng, lat, lng);
    const idx         = near.idx;
    const distToRoute = near.distM;
    const total    = state.nav.cumDistM[state.nav.cumDistM.length - 1] || active.route.distance_m || 0;
    const traveled = state.nav.cumDistM[idx] || 0;
    const remaining = Math.max(0, total - traveled);

    ui.bnRemaining.textContent = (remaining >= 1000 ? fmtKm(remaining) : Math.round(remaining) + " m");

    const durTotal = active.route.duration_s || 0;
    const etaS     = durTotal > 0 && total > 0 ? (durTotal * (remaining / total)) : durTotal;
    const now      = new Date();
    const arriveAt = new Date(now.getTime() + etaS * 1000);
    const h = arriveAt.getHours();
    const m = arriveAt.getMinutes();
    ui.bnEta.textContent = (h % 12 || 12) + ":" + String(m).padStart(2, "0") + (h >= 12 ? " PM" : " AM");

    const t = Date.now();
    if (lastNavPos && lastNavTime && (t - lastNavTime) >= 2000) {
      const dt       = (t - lastNavTime) / 1000;
      const d        = haversineM(lastNavPos[0], lastNavPos[1], lat, lng);
      const speedMs  = dt > 0 ? d / dt : 0;
      const speedKmh = speedMs * 3.6;
      ui.bnSpeed.textContent = speedKmh < 0.5 ? "0 km/h" : speedKmh.toFixed(0) + " km/h";
      lastNavPos  = [lat, lng];
      lastNavTime = t;
    } else if (!lastNavPos) {
      lastNavPos  = [lat, lng];
      lastNavTime = t;
      ui.bnSpeed.textContent = "—";
    }

    ui.bnNext.textContent  = getNextWaypointForPosition(active.route, traveled);
    ui.bnScore.textContent = active.route.route_score + "% (" + (active.route.zone || "—").toUpperCase() + ")";
    ui.bnReco.textContent  = (active.route.ai_message || "").slice(0, 80) + (active.route.ai_message && active.route.ai_message.length > 80 ? "…" : "");
    ui.bnCurrent.textContent = lat.toFixed(4) + ", " + lng.toFixed(4);

    map.easeTo({ center: [lng, lat], duration: 300 });

    if (distToRoute > 80) rerouteFrom(lat, lng);
  }
})();

// ──────────────────────────────────────────
// Profile Modal + SOS (unchanged from original)
// ──────────────────────────────────────────
document.addEventListener("DOMContentLoaded", function () {

  const saveBtn = document.getElementById("saveProfile");
  if (saveBtn) {
    saveBtn.addEventListener("click", function () {
      let name  = document.getElementById("userName").value.trim();
      let phone = document.getElementById("userPhone").value.trim();
      let c1    = document.getElementById("contact1").value.trim();
      let c2    = document.getElementById("contact2").value.trim();
      let c3    = document.getElementById("contact3").value.trim();

      if (!name || !phone) { alert("Please enter your name and phone number"); return; }

      let profile = { name, phone, contacts: [c1, c2, c3] };
      localStorage.setItem("safe_user", JSON.stringify(profile));

      fetch("/api/save_profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(profile),
      })
        .then((res) => res.json())
        .then((data) => console.log("Profile saved on server", data))
        .catch((err) => console.error("Failed to save profile on server", err));

      document.getElementById("profileModal").style.display = "none";
      console.log("Profile saved", profile);
    });
  }

  const sosBtn = document.getElementById("sosBtn");
  if (sosBtn) {
    let sosIntervalId = null;
    let sirenAudio    = null;

    const flashOverlay = document.createElement("div");
    flashOverlay.style.cssText = "position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(255,0,0,0.15);pointer-events:none;z-index:9999;display:none;";
    document.body.appendChild(flashOverlay);

    function startAlarm() {
      try {
        if (!sirenAudio) { sirenAudio = new Audio("/static/siren.mp3"); sirenAudio.loop = true; }
        sirenAudio.play().catch(() => console.warn("Siren auto-play blocked"));
      } catch (e) { console.warn("Siren audio not available", e); }
      flashOverlay.style.display = "block";
      setTimeout(() => { flashOverlay.style.display = "none"; }, 2000);
    }

    function stopAlarm() {
      if (sirenAudio) { sirenAudio.pause(); sirenAudio.currentTime = 0; }
      flashOverlay.style.display = "none";
    }

    function stopTracking() {
      if (sosIntervalId) { clearInterval(sosIntervalId); sosIntervalId = null; }
      stopAlarm();
    }

    function getContactsFromServer() {
      return fetch("/api/get_contacts")
        .then((res) => res.json())
        .then((data) => Array.isArray(data.contacts) ? data.contacts.slice(0, 3) : [])
        .catch(() => []);
    }

    function createLocationLink(lat, lng) { return `https://www.google.com/maps?q=${lat},${lng}`; }

    function sendWhatsAppMessages(lat, lng, contacts, name) {
      return fetch("/api/send_whatsapp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat, lng, name, contacts }),
      }).then((r) => r.json()).catch((e) => ({ error: e.message }));
    }

    function callSosApi(lat, lng, profile, source, contacts) {
      return fetch("/api/sos_alert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: profile.name, phone: profile.phone,
          contacts: contacts || profile.contacts || [],
          lat, lng, map_link: createLocationLink(lat, lng),
          time: new Date().toISOString(), location_source: source,
        }),
      })
        .then((r) => r.json())
        .then((d) => { console.log("SOS logged:", d); alert("🚨 Emergency alert triggered! Location sent."); return d; })
        .catch((e) => { console.error("SOS log failed:", e); alert("🚨 Emergency alert attempted, server call failed."); return null; });
    }

    function askManualLocation(profile, contacts) {
      const input = prompt("Unable to fetch GPS. Enter location (e.g., '17.3850,78.4867'):");
      if (!input) { alert("SOS aborted."); stopTracking(); return; }
      const m = input.trim().match(/^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$/);
      if (m) {
        const lat = parseFloat(m[1]), lng = parseFloat(m[2]);
        if (!isNaN(lat) && !isNaN(lng)) { sendWhatsAppMessages(lat, lng, contacts, profile.name); return callSosApi(lat, lng, profile, "manual-typed", contacts); }
      }
      sendWhatsAppMessages(17.385, 78.4867, contacts, profile.name);
      return callSosApi(17.385, 78.4867, profile, "fallback-default", contacts);
    }

    sosBtn.addEventListener("click", async function () {
      const profile = JSON.parse(localStorage.getItem("safe_user"));
      if (!profile || !profile.name || !profile.phone) { alert("Please setup your safety profile first"); return; }

      let contacts = await getContactsFromServer();
      if (!contacts || !contacts.length) contacts = profile.contacts || [];
      if (!contacts || !contacts.length) { alert("No emergency contacts configured."); return; }

      startAlarm();

      const triggerOnce = (lat, lng, source) => {
        sendWhatsAppMessages(lat, lng, contacts, profile.name);
        callSosApi(lat, lng, profile, source, contacts);
      };

      navigator.geolocation.getCurrentPosition(
        (pos) => triggerOnce(pos.coords.latitude, pos.coords.longitude, "gps"),
        (err) => {
          if (err.code === 1) { alert("Location denied."); stopTracking(); return; }
          navigator.geolocation.getCurrentPosition(
            (pos) => triggerOnce(pos.coords.latitude, pos.coords.longitude, "gps-low-accuracy"),
            () => askManualLocation(profile, contacts),
            { enableHighAccuracy: false, timeout: 20000, maximumAge: 60000 }
          );
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
      );

      // Periodic location updates
      sosIntervalId = setInterval(() => {
        navigator.geolocation.getCurrentPosition(
          (pos) => {
            fetch("/api/send_whatsapp", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ lat: pos.coords.latitude, lng: pos.coords.longitude, name: profile.name, contacts }),
            }).catch(() => {});
          },
          () => {},
          { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
        );
      }, 10000);
    });
  }
});