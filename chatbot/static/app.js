/* Nicosia Digital Twin: map + chat client. Streams a turn from /api/chat (SSE) and applies the
   map and chart commands the assistant sends. */
"use strict";

const BASEMAPS = {
  liberty: "https://tiles.openfreemap.org/styles/liberty",
  positron: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
const FONT_REGULAR = ["Noto Sans Regular"];
const FONT_BOLD = ["Noto Sans Bold"];
const RAMP = ["#16a34a", "#84cc16", "#eab308", "#f97316", "#dc2626"];
const SUGGESTIONS = [
  "Which street lights are on right now?",
  "Where can I park near Ledras Street?",
  "Plan a 3-hour walking tour of museums in the old town, starting at Eleftheria Square",
  "How are air quality and noise across the city?",
  "Show me where people walked the most yesterday",
  "What can I reach on foot in 10 and 20 minutes from Eleftheria Square?",
  "Which bins are full? Show them in 3D",
  "Is it a good afternoon for a walk?",
  "Replay the street lights over the last 24 hours as a time-lapse, with a chart of lit lamps",
  "Draw a radial 24-hour clock of how parking occupancy changed since yesterday",
  "Show the dangerous buildings in the old town in 3D",
  "Which communities have the most dangerous buildings? Chart it and map it",
  "How tall are the buildings around Eleftheria Square? Show the skyline in 3D",
  "Are there dangerous high-rise buildings in Strovolos?",
  "Where am I, and are there dangerous buildings near me?",
  "Walk me to the Cyprus Museum, avoiding the dangerous buildings",
];
const TOOL_LABELS = {
  city_overview: "Reading the city's sensors", find_devices: "Finding devices",
  get_history: "Replaying history",
  get_device: "Reading a device", get_alarms: "Checking alarms", get_traffic: "Counting traffic",
  get_speed: "Checking traffic speed", get_weather: "Checking the weather",
  geocode: "Looking up a place", find_places: "Searching places", plan_route: "Planning the route",
  reachable_area: "Working out the reachable area", show_on_map: "Drawing on the map",
  find_dangerous_buildings: "Reading the dangerous-building registers",
  where_am_i: "Working out where you are",
  get_buildings_3d: "Reading the DLS 3D buildings",
  map_fly_to: "Moving the map", map_callouts: "Adding callouts", map_clear: "Clearing the map",
  show_chart: "Drawing a chart", show_visual: "Drawing a custom visual", web_search: "Searching the web",
};
// Libraries a custom visual may load, pinned. The sandbox's CSP allows exactly these URLs.
const VISUAL_LIBS = {
  d3: "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js",
  plot: "https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6.17/dist/plot.umd.min.js",
  echarts: "https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js",
  three: "https://cdn.jsdelivr.net/npm/three@0.159.0/build/three.min.js",
};
const VISUAL_THEME = {
  text: "#16181d", muted: "#5f6672", grid: "#e3e5ea", accent: "#2563eb", ramp: RAMP,
  categorical: ["#2563eb", "#f97316", "#16a34a", "#dc2626", "#7c3aed", "#0ea5e9", "#eab308", "#db2777", "#64748b", "#0f766e"],
};
const SPEEDS = [1, 2, 4];

const $ = (id) => document.getElementById(id);
const state = {
  conversationId: null,
  busy: false,
  layers: new Map(), // ref -> {spec, data, ids: [], stats}
  callouts: [],
  pulses: new Set(), // halo layer ids
  dashLayers: new Set(),
  timelapse: null, // {ref, frame, playing, speed, timer}
  charts: [], // {view, link} for charts linked to the map
  timeCharts: [], // {view, min, max, last} charts over time, with a rule at `_t`
  visuals: [], // {iframe, title, card} sandboxed custom visuals
  visualErrors: [], // runtime errors to report with the next message
  hl3d: new Set(), // extrusion layers that light up the hovered building
};
// The DLS 3D city: z16 tiles of buildings streamed from /api/buildings3d as the view moves.
const CITY = {
  on: false, features: [], ids: new Set(), tiles: new Set(), inflight: new Set(), queue: [],
};
const CITY_SRC = "city3d::src";
const CITY_MIN_ZOOM = 14.5;
const CITY_RADIUS = 3; // tiles around the centre (each ~500 m), so pitched views stay bounded
const CITY_HEIGHTS = [0, "#e5e7eb", 6, "#cbd5e1", 12, "#93c5fd", 20, "#3b82f6", 35, "#1d4ed8", 60, "#312e81"];
const REGISTERS_REF = "eoa_registers";

// ---------------------------------------------------------------------------------------------
// Map

const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
const initialBase = prefersDark ? "dark" : "liberty";
$("basemap").value = initialBase;

const map = new maplibregl.Map({
  container: "map",
  style: BASEMAPS[initialBase],
  center: [33.3615, 35.1725],
  zoom: 14.2,
  pitch: initialBase === "liberty" ? 35 : 0,
  maxPitch: 75,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-left");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

// ---------------------------------------------------------------------------------------------
// "Where am I": the user's position, from the device (the locate button) or set by
// right-clicking the map. It goes to the assistant with each message, only once shared.

const me = { loc: null, marker: null };

function setMyLocation(lat, lon, accuracy, source) {
  me.loc = { lat, lon, accuracy_m: Number.isFinite(accuracy) ? Math.round(accuracy) : null, source };
  if (!me.marker) {
    const el = document.createElement("div");
    el.className = "me-dot";
    el.title = "You are here";
    me.marker = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map);
  } else {
    me.marker.setLngLat([lon, lat]);
  }
  renderMyLocation();
}

function forgetMyLocation() {
  me.loc = null;
  me.marker?.remove();
  me.marker = null;
  renderMyLocation();
}

function renderMyLocation(error) {
  const el = $("me-status");
  if (error) { el.textContent = error; return; }
  if (!me.loc) { el.textContent = "Right-click the map to set where you are"; return; }
  el.innerHTML = `You: ${escapeHtml(me.loc.source)}${me.loc.accuracy_m ? ` (±${me.loc.accuracy_m} m)` : ""}, shared with the assistant <button type="button" class="linkish" id="me-forget">forget</button>`;
  $("me-forget").addEventListener("click", forgetMyLocation);
}

const geolocate = new maplibregl.GeolocateControl({
  positionOptions: { enableHighAccuracy: true }, trackUserLocation: false, showUserLocation: false,
  fitBoundsOptions: { maxZoom: 16.5 },
});
map.addControl(geolocate, "top-left");
geolocate.on("geolocate", (pos) => setMyLocation(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy, "device location"));
geolocate.on("error", () => renderMyLocation("Couldn't read your location here: right-click the map where you are instead"));

map.on("contextmenu", (e) => {
  const box = document.createElement("div");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "me-btn";
  button.textContent = "I'm here";
  box.appendChild(button);
  const popup = new maplibregl.Popup({ closeButton: false, offset: 6 }).setLngLat(e.lngLat).setDOMContent(box).addTo(map);
  button.addEventListener("click", () => { setMyLocation(e.lngLat.lat, e.lngLat.lng, NaN, "set on the map"); popup.remove(); });
});
// A page opened in a hidden tab measures a 0-size container; keep the canvas in step.
new ResizeObserver(() => map.resize()).observe($("map"));
map.on("load", () => map.resize());

// Layers can be added once the style itself has loaded. isStyleLoaded() also waits for every
// tile, which on a slow tile server held back layers (and time-lapses) for a long time.
let styleReady = false;
map.on("style.load", () => { styleReady = true; });
function whenStyleReady(fn) {
  styleReady ? fn() : map.once("style.load", fn);
}

$("basemap").addEventListener("change", (e) => {
  styleReady = false;
  map.setStyle(BASEMAPS[e.target.value]);
  map.once("style.load", () => {
    if (CITY.on) addCityLayer();
    for (const entry of state.layers.values()) addToMap(entry);
    syncHighlight();
  });
});

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function numericStats(features, field) {
  const vals = features.map((f) => Number(f.properties?.[field])).filter(Number.isFinite);
  if (!vals.length) return null;
  return { min: Math.min(...vals), max: Math.max(...vals) };
}

function rampColor(t) {
  const x = Math.max(0, Math.min(1, t)) * (RAMP.length - 1);
  const i = Math.min(RAMP.length - 2, Math.floor(x));
  const a = hexToRgb(RAMP[i]), b = hexToRgb(RAMP[i + 1]), f = x - i;
  return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",")})`;
}
function hexToRgb(h) { return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)); }

function squareAround([lon, lat], meters) {
  const dLat = meters / 111320;
  const dLon = meters / (111320 * Math.cos((lat * Math.PI) / 180));
  return [[[lon - dLon, lat - dLat], [lon + dLon, lat - dLat], [lon + dLon, lat + dLat], [lon - dLon, lat + dLat], [lon - dLon, lat - dLat]]];
}

const boundPopups = new Set(); // map.on handlers outlive style changes, so bind once per id
function bindPopup(layerId) {
  if (boundPopups.has(layerId)) return;
  boundPopups.add(layerId);
  map.on("click", layerId, (e) => {
    const f = e.features?.[0];
    if (!f) return;
    const text = f.properties.popup || f.properties.label || f.properties.id;
    new maplibregl.Popup({ offset: 10 }).setLngLat(e.lngLat)
      .setHTML(escapeHtml(text).replace(/\n/g, "<br>")).addTo(map);
  });
  map.on("mouseenter", layerId, () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; linkHover("id", null, "map"); });
  map.on("mousemove", layerId, (e) => linkHover("id", e.features?.[0]?.properties?.id ?? null, "map"));
}

function addLayerSafe(def, ids) {
  map.addLayer(def);
  ids.push(def.id);
}

function addToMap(entry) {
  const { spec, data } = entry;
  const ref = spec.ref;
  const src = `${ref}::src`;
  entry.ids = [];
  if (map.getSource(src)) removeFromMap(ref);
  const points = ["==", ["geometry-type"], "Point"];
  const vf = spec.value_field;
  // A time-lapse keeps one scale across all frames, so heights and heat compare over time.
  entry.stats = vf ? (data.frames ? frameStats(data.frames, vf) : numericStats(data.features, vf)) : null;

  if (spec.style === "columns_3d") {
    map.addSource(src, { type: "geojson", data: columnsData(entry) });
    addLayerSafe({
      id: `${ref}::columns`, type: "fill-extrusion", source: src,
      paint: { "fill-extrusion-color": ["get", "_c"], "fill-extrusion-height": ["get", "_h"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.9 },
    }, entry.ids);
    bindPopup(`${ref}::columns`);
    if (spec.fit === false && map.getPitch() < 45) map.easeTo({ pitch: 55, duration: 900 });
    return;
  }

  map.addSource(src, { type: "geojson", data });

  if (spec.style === "buildings_3d") {
    // Real footprints at their DLS heights, plot outlines on the ground, and markers that
    // carry the layer when zoomed out (where the buildings are too small to see).
    addExtrusions(entry, src, 0);
    addLayerSafe({
      id: `${ref}::circle`, type: "circle", source: src, filter: points,
      paint: {
        "circle-color": ["get", "color"],
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 2.2, 12, 3.6, 15, 5, 17, 3.5],
        "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.1,
        "circle-opacity": ["interpolate", ["linear"], ["zoom"], 15.5, 0.95, 17.5, 0.25],
        "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 15.5, 1, 17.5, 0.25],
      },
    }, entry.ids);
    bindPopup(`${ref}::circle`);
    return;
  }

  if (spec.style === "heatmap") {
    const st = entry.stats;
    const weight = st && st.max > st.min
      ? ["interpolate", ["linear"], ["to-number", ["get", vf], st.min], st.min, 0.05, st.max, 1]
      : 1;
    addLayerSafe({
      id: `${ref}::heat`, type: "heatmap", source: src, filter: points,
      paint: {
        "heatmap-weight": weight,
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 11, 0.9, 16, 1.8],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 11, 18, 15, 38, 18, 70],
        "heatmap-opacity": 0.85,
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(37,99,235,0)", 0.15, "#3b82f6", 0.35, "#22c55e", 0.55, "#eab308", 0.75, "#f97316", 1, "#dc2626"],
      },
    }, entry.ids);
    addLayerSafe({
      id: `${ref}::dots`, type: "circle", source: src, filter: points, minzoom: 15.5,
      paint: { "circle-radius": 4, "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 1 },
    }, entry.ids);
    bindPopup(`${ref}::dots`);
    return;
  }

  if (spec.style === "area") {
    // Building layers draw their plots here, not the 3D footprints.
    const polys = ["all", ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
      ["!=", ["coalesce", ["get", "_kind"], ""], "footprint"]];
    addLayerSafe({ id: `${ref}::fill`, type: "fill", source: src, filter: polys, paint: { "fill-color": ["get", "color"], "fill-opacity": 0.16 } }, entry.ids);
    addLayerSafe({ id: `${ref}::outline`, type: "line", source: src, filter: polys, paint: { "line-color": ["get", "color"], "line-width": 2.2 } }, entry.ids);
  }

  if (spec.style === "route") {
    const lines = ["==", ["geometry-type"], "LineString"];
    // A route avoiding dangerous buildings comes with the usual route: thinner, grey, still.
    const alt = ["==", ["get", "state"], "usual route"];
    addLayerSafe({ id: `${ref}::casing`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#ffffff", "line-width": ["case", alt, 6.5, 10], "line-opacity": 0.85 } }, entry.ids);
    addLayerSafe({ id: `${ref}::line`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": ["get", "color"], "line-width": ["case", alt, 3.5, 5.5] } }, entry.ids);
    addLayerSafe({ id: `${ref}::dash`, type: "line", source: src, filter: ["all", lines, ["!", alt]], layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#ffffff", "line-width": 2.5, "line-dasharray": [0, 4, 3] } }, entry.ids);
    state.dashLayers.add(`${ref}::dash`);
    // Stops are numbered; register buildings along the way are smaller red "!" markers.
    addLayerSafe({ id: `${ref}::stops`, type: "circle", source: src, filter: points, paint: { "circle-radius": ["case", ["==", ["to-string", ["get", "n"]], "!"], 8.5, 11], "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 2 } }, entry.ids);
    addLayerSafe({ id: `${ref}::num`, type: "symbol", source: src, filter: points, layout: { "text-field": ["to-string", ["get", "n"]], "text-font": FONT_BOLD, "text-size": 12, "text-allow-overlap": true }, paint: { "text-color": "#fff" } }, entry.ids);
    bindPopup(`${ref}::stops`);
    if (spec.labels !== false) addLabels(entry, src, points, 1.6);
    return;
  }

  // A building layer shown as markers still stands up in 3D once zoomed in to street level.
  if (data.features.some((f) => f.properties?._kind === "footprint")) addExtrusions(entry, src, 15);

  if (spec.style === "pulse") {
    const id = `${ref}::halo`;
    addLayerSafe({ id, type: "circle", source: src, filter: points, paint: { "circle-radius": 10, "circle-color": ["get", "color"], "circle-opacity": 0.4, "circle-blur": 0.4 } }, entry.ids);
    state.pulses.add(id);
  }
  addLayerSafe({
    id: `${ref}::circle`, type: "circle", source: src, filter: points,
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, spec.style === "pulse" ? 5 : 3, 14, spec.style === "pulse" ? 8 : 5.5, 17, spec.style === "pulse" ? 12 : 9],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.6, "circle-opacity": 0.95,
    },
  }, entry.ids);
  bindPopup(`${ref}::circle`);
  if (spec.labels) addLabels(entry, src, points, 1.1);
}

// The 3D part of a building layer: plot outlines, footprints extruded at their DLS heights, and
// a highlight extrusion for the hovered building. From `minzoom` (0 for 'buildings_3d').
function addExtrusions(entry, src, minzoom) {
  const ref = entry.spec.ref;
  entry.extruded = true;
  addLayerSafe({
    id: `${ref}::plots`, type: "line", source: src, minzoom, filter: ["==", ["get", "_kind"], "parcel"],
    paint: { "line-color": ["get", "color"], "line-width": ["interpolate", ["linear"], ["zoom"], 14, 0.5, 18, 2.4], "line-opacity": 0.9 },
  }, entry.ids);
  addLayerSafe({
    id: `${ref}::extrude`, type: "fill-extrusion", source: src, minzoom, filter: ["==", ["get", "_kind"], "footprint"],
    paint: { "fill-extrusion-color": ["get", "color"], "fill-extrusion-height": ["get", "height_m"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.93 },
  }, entry.ids);
  addLayerSafe({
    id: `${ref}::hl3d`, type: "fill-extrusion", source: src, minzoom, filter: hl3dFilter(hlValue),
    paint: { "fill-extrusion-color": "#ff2d55", "fill-extrusion-height": ["+", ["get", "height_m"], 0.6], "fill-extrusion-opacity": 0.96 },
  }, entry.ids);
  state.hl3d.add(`${ref}::hl3d`);
  bindPopup(`${ref}::extrude`);
  syncCityClaims();
}

function frameStats(frames, field) {
  const vals = (frames.values[field] || []).flat().filter(Number.isFinite);
  return vals.length ? { min: Math.min(...vals), max: Math.max(...vals) } : null;
}

function columnsData(entry) {
  const vf = entry.spec.value_field;
  const st = entry.stats || { min: 0, max: 1 };
  const span = st.max - st.min || 1;
  const pts = entry.data.features.filter((f) => f.geometry?.type === "Point");
  // Column width follows the layer's extent: 6 m in a street, up to 45 m city-wide.
  entry.half ??= Math.max(6, Math.min(45, extentMeters(pts) / 220));
  const half = entry.half;
  return {
    type: "FeatureCollection",
    features: pts.map((f) => {
      const v = Number(f.properties[vf]);
      const t = Number.isFinite(v) ? (v - st.min) / span : 0;
      return {
        type: "Feature",
        geometry: { type: "Polygon", coordinates: squareAround(f.geometry.coordinates, half) },
        properties: { ...f.properties, _h: Number.isFinite(v) ? half * 2 + t * half * 40 : 0, _c: rampColor(t) },
      };
    }),
  };
}

function addLabels(entry, src, filter, offset) {
  addLayerSafe({
    id: `${entry.spec.ref}::labels`, type: "symbol", source: src, filter,
    layout: { "text-field": ["get", "label"], "text-font": FONT_REGULAR, "text-size": 11.5, "text-offset": [0, offset], "text-anchor": "top", "text-max-width": 12 },
    paint: { "text-color": "#1f2937", "text-halo-color": "#ffffff", "text-halo-width": 1.6 },
  }, entry.ids);
}

function removeFromMap(ref) {
  const entry = state.layers.get(ref);
  for (const id of entry?.ids || []) {
    if (map.getLayer(id)) map.removeLayer(id);
    state.pulses.delete(id);
    state.dashLayers.delete(id);
    state.hl3d.delete(id);
  }
  if (map.getSource(`${ref}::src`)) map.removeSource(`${ref}::src`);
}

function removeLayer(ref) {
  removeFromMap(ref);
  state.layers.delete(ref);
  if (state.timelapse?.ref === ref) stopTimelapse();
  if (ref === REGISTERS_REF) $("tg-dangerous").checked = false;
  syncCityClaims();
  renderLegend();
  syncHighlight();
}

function clearMap() {
  for (const ref of [...state.layers.keys()]) removeFromMap(ref);
  state.layers.clear();
  $("tg-dangerous").checked = false;
  stopTimelapse();
  clearCallouts();
  syncCityClaims();
  renderLegend();
  syncHighlight();
}

function coordsOf(geom, out) {
  if (!geom) return out;
  const walk = (c) => (typeof c[0] === "number" ? out.push(c) : c.forEach(walk));
  walk(geom.coordinates);
  return out;
}

function extentMeters(features) {
  const pts = [];
  features.forEach((f) => coordsOf(f.geometry, pts));
  if (pts.length < 2) return 0;
  const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1]);
  const dLat = (Math.max(...lats) - Math.min(...lats)) * 111320;
  const dLon = (Math.max(...lons) - Math.min(...lons)) * 111320 * Math.cos((lats[0] * Math.PI) / 180);
  return Math.hypot(dLat, dLon);
}

function fitTo(data, pitch = map.getPitch()) {
  const pts = [];
  data.features.forEach((f) => coordsOf(f.geometry, pts));
  if (!pts.length) return;
  if (pts.length === 1) { map.flyTo({ center: pts[0], zoom: 16.5, pitch, essential: true }); return; }
  const b = new maplibregl.LngLatBounds(pts[0], pts[0]);
  pts.forEach((p) => b.extend(p));
  map.fitBounds(b, { padding: { top: 60, bottom: 70, left: 60, right: 60 }, maxZoom: 17, duration: 1400, pitch });
}

async function showLayer(spec, prefetched = null) {
  let data = prefetched;
  if (!data) {
    const res = await fetch(spec.url);
    if (!res.ok) throw new Error(`layer ${spec.ref}: HTTP ${res.status}`);
    data = await res.json();
  }
  if (state.layers.has(spec.ref)) removeFromMap(spec.ref);
  const entry = { spec, data, ids: [] };
  state.layers.set(spec.ref, entry);
  const tilts = spec.style === "columns_3d" || spec.style === "buildings_3d";
  const pitch = tilts ? Math.max(55, map.getPitch()) : map.getPitch();
  const askedAt = spec.askedAt ?? performance.now();
  const draw = () => {
    addToMap(entry);
    // A camera move asked for alongside this layer (tools run in parallel, so just before it)
    // or after it, while it loaded, wins over fitting to it.
    if (spec.fit !== false && !(state.flownAt > askedAt - 3000)) fitTo(data, pitch);
    else if (spec.style === "buildings_3d" && map.getZoom() >= 15 && map.getPitch() < 45) map.easeTo({ pitch: 55, duration: 900 });
    renderLegend();
    syncHighlight();
    if (data.frames) startTimelapse(spec.ref);
  };
  whenStyleReady(draw);
}

// ---------------------------------------------------------------------------------------------
// The DLS 3D city: every building in view at its real height (from the Department of Lands and
// Surveys), coloured by height. It replaces the base map's own 3D buildings while on. Buildings
// that a 'buildings_3d' layer redraws are hidden here (no z-fighting); an old-town block that
// only holds a register plot turns into a translucent ghost, so the plot shows inside it.

function cityPaint(opacity) {
  return {
    "fill-extrusion-color": ["interpolate", ["linear"], ["get", "h"], ...CITY_HEIGHTS],
    "fill-extrusion-height": ["get", "h"], "fill-extrusion-base": 0, "fill-extrusion-opacity": opacity,
  };
}

function addCityLayer() {
  if (!map.getSource(CITY_SRC)) {
    map.addSource(CITY_SRC, { type: "geojson", data: { type: "FeatureCollection", features: CITY.features } });
  }
  // Below any layer the assistant drew, so its buildings and markers stay on top.
  const before = map.getStyle().layers.find((l) => l.id.includes("::") && !l.id.startsWith("city3d::"))?.id;
  if (!map.getLayer("city3d::fill")) map.addLayer({ id: "city3d::fill", type: "fill-extrusion", source: CITY_SRC, minzoom: CITY_MIN_ZOOM - 0.5, paint: cityPaint(0.92) }, before);
  if (!map.getLayer("city3d::ghost")) map.addLayer({ id: "city3d::ghost", type: "fill-extrusion", source: CITY_SRC, minzoom: CITY_MIN_ZOOM - 0.5, filter: ["in", ["id"], ["literal", []]], paint: cityPaint(0.28) }, before);
  setBaseBuildings(false);
  syncCityClaims();
  bindCityPopup();
}

function removeCityLayer() {
  for (const id of ["city3d::ghost", "city3d::fill"]) if (map.getLayer(id)) map.removeLayer(id);
  if (map.getSource(CITY_SRC)) map.removeSource(CITY_SRC);
  setBaseBuildings(true);
}

// The base map's own (OpenStreetMap) 3D buildings.
function setBaseBuildings(visible) {
  for (const l of map.getStyle()?.layers || []) {
    if (l.type === "fill-extrusion" && !l.id.includes("::")) map.setLayoutProperty(l.id, "visibility", visible ? "visible" : "none");
  }
}

function syncCityClaims() {
  if (!map.getLayer("city3d::fill")) return;
  const claimed = new Set(), ghosts = new Set();
  for (const { extruded, data } of state.layers.values()) {
    if (!extruded) continue;
    for (const f of data.features) {
      if (f.properties?.bid !== undefined) claimed.add(f.properties.bid);
      if (f.properties?.ghost_bid !== undefined) ghosts.add(f.properties.ghost_bid);
    }
  }
  map.setFilter("city3d::fill", ["!", ["in", ["id"], ["literal", [...claimed, ...ghosts]]]]);
  map.setFilter("city3d::ghost", ["in", ["id"], ["literal", [...ghosts].filter((g) => !claimed.has(g))]]);
}

function cityTilesInView() {
  if (map.getZoom() < CITY_MIN_ZOOM) return [];
  const n = 2 ** 16;
  const tx = (lon) => Math.floor(((lon + 180) / 360) * n);
  const ty = (lat) => Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n);
  const b = map.getBounds(), c = map.getCenter();
  const cx = tx(c.lng), cy = ty(c.lat);
  const out = [];
  for (let x = Math.max(tx(b.getWest()), cx - CITY_RADIUS); x <= Math.min(tx(b.getEast()), cx + CITY_RADIUS); x++) {
    for (let y = Math.max(ty(b.getNorth()), cy - CITY_RADIUS); y <= Math.min(ty(b.getSouth()), cy + CITY_RADIUS); y++) {
      if (!CITY.tiles.has(`${x}/${y}`)) out.push([x, y, (x - cx) ** 2 + (y - cy) ** 2]);
    }
  }
  return out.sort((p, q) => p[2] - q[2]); // nearest the centre first
}

function pumpCity() {
  while (CITY.on && CITY.queue.length && CITY.inflight.size < 6) {
    const [x, y] = CITY.queue.shift();
    const key = `${x}/${y}`;
    if (CITY.tiles.has(key) || CITY.inflight.has(key)) continue;
    CITY.inflight.add(key);
    fetch(`/api/buildings3d/16/${x}/${y}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((fc) => {
        CITY.tiles.add(key);
        const add = fc.features.filter((f) => !CITY.ids.has(f.id));
        for (const f of add) { CITY.ids.add(f.id); CITY.features.push(f); }
        if (add.length) map.getSource(CITY_SRC)?.updateData({ add });
      })
      .catch(() => { /* leave it unloaded; the next move retries it */ })
      .finally(() => { CITY.inflight.delete(key); cityStatus(); pumpCity(); });
  }
  cityStatus();
}

function refreshCity() {
  if (!CITY.on) return;
  CITY.queue = cityTilesInView().map(([x, y]) => [x, y]);
  pumpCity();
}

function cityStatus() {
  const el = $("city-status");
  if (!CITY.on) { el.textContent = ""; return; }
  if (map.getZoom() < CITY_MIN_ZOOM) el.textContent = "Zoom in to see the 3D buildings";
  else if (CITY.inflight.size || CITY.queue.length) el.textContent = `Loading 3D buildings (${CITY.inflight.size + CITY.queue.length} areas left)`;
  else el.textContent = `${CITY.features.length.toLocaleString()} DLS buildings loaded`;
}

function setCity3d(on) {
  CITY.on = on;
  $("tg-city3d").checked = on;
  whenStyleReady(() => {
    if (on) {
      addCityLayer();
      if (map.getPitch() < 40 && map.getZoom() >= CITY_MIN_ZOOM) map.easeTo({ pitch: 55, duration: 900 });
      refreshCity();
    } else {
      CITY.queue = [];
      removeCityLayer();
    }
    cityStatus();
    renderLegend();
  });
}

map.on("moveend", refreshCity);
map.on("zoomend", cityStatus);

let cityPopupBound = false;
function bindCityPopup() {
  if (cityPopupBound) return;
  cityPopupBound = true;
  map.on("click", "city3d::fill", (e) => {
    // An assistant layer under the pointer has its own popup.
    if (map.queryRenderedFeatures(e.point).some((f) => f.layer.id.includes("::") && !f.layer.id.startsWith("city3d::"))) return;
    const f = e.features?.[0];
    if (!f) return;
    const p = f.properties;
    const lines = [`${p.t || "Building"} · DLS 3D #${f.id}`,
      `${p.h} m to the roof, eave ${p.e} m${p.f ? `, ${p.f} floors` : ""}`,
      [p.r, p.v ? `${Math.round(p.v).toLocaleString()} m³` : "", p.a ? `${Math.round(p.a).toLocaleString()} m² footprint` : ""].filter(Boolean).join(" · ")];
    new maplibregl.Popup({ offset: 10 }).setLngLat(e.lngLat).setHTML(lines.filter(Boolean).map(escapeHtml).join("<br>")).addTo(map);
  });
  map.on("mouseenter", "city3d::fill", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "city3d::fill", () => (map.getCanvas().style.cursor = ""));
}

$("tg-city3d").addEventListener("change", (e) => setCity3d(e.target.checked));

async function showRegisters(on) {
  if (!on) { removeLayer(REGISTERS_REF); return; }
  const status = $("city-status");
  status.textContent = "Loading the dangerous-building registers...";
  try {
    const res = await fetch("/api/dangerous");
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const data = await res.json();
    await showLayer({
      ref: REGISTERS_REF, url: "/api/dangerous", style: "buildings_3d", title: data.meta.title,
      fit: false, labels: false, legend: data.meta.legend, count: data.meta.count, value_field: null,
    }, data);
    cityStatus();
  } catch (e) {
    $("tg-dangerous").checked = false;
    status.textContent = `Couldn't load the registers: ${e.message}`;
  }
}
$("tg-dangerous").addEventListener("change", (e) => showRegisters(e.target.checked));

// ---------------------------------------------------------------------------------------------
// Time-lapse: a layer with `frames` replays per-frame states and values on its features.

function applyFrame(k) {
  const tl = state.timelapse;
  const entry = tl && state.layers.get(tl.ref);
  if (!entry) return;
  const fr = entry.data.frames;
  k = Math.max(0, Math.min(fr.times.length - 1, Math.round(k)));
  tl.frame = k;
  const legend = entry.spec.legend || {};
  entry.data.features.forEach((f, j) => {
    const p = f.properties;
    const st = fr.state_names[fr.states[k][j]];
    p.state = st;
    p.color = legend[st] || "#a3a3a3";
    const parts = [];
    for (const [field, rows] of Object.entries(fr.values)) {
      const v = rows[k][j];
      if (v === null || v === undefined) delete p[field];
      else { p[field] = v; parts.push(`${field.replace(/_/g, " ")} ${fmt(v)}`); }
    }
    p.popup = `${p.name || p.label}\n${st.replace(/_/g, " ")}${parts.length ? ` · ${parts.join(", ")}` : ""}\n${fr.times[k]}`;
  });
  const src = map.getSource(`${tl.ref}::src`);
  if (src) src.setData(entry.spec.style === "columns_3d" ? columnsData(entry) : entry.data);
  $("tl-slider").value = k;
  $("tl-time").textContent = fr.times[k];
  $("tl-clock").textContent = fr.times[k];
  for (const c of state.charts) if (c.link.key === "frame" && c.link.layer === tl.ref) setChartHighlight(c, k);
  setTimeAll(frameMs(entry, k));
  for (const v of state.visuals) postToVisual(v, k, "frame");
}

function startTimelapse(ref) {
  stopTimelapse();
  const entry = state.layers.get(ref);
  state.timelapse = { ref, frame: 0, playing: false, speed: 1, timer: null };
  $("tl-slider").max = entry.data.frames.times.length - 1;
  $("tl-title").textContent = entry.spec.title;
  $("tl-speed").textContent = "1×";
  $("timeline").hidden = false;
  $("tl-clock").hidden = false;
  document.querySelector(".map-wrap").classList.add("has-timeline");
  applyFrame(0);
  playTimelapse();
}

function stopTimelapse() {
  if (!state.timelapse) return;
  clearTimeout(state.timelapse.timer);
  state.timelapse = null;
  $("timeline").hidden = true;
  $("tl-clock").hidden = true;
  document.querySelector(".map-wrap").classList.remove("has-timeline");
  setTimeAll(nicosiaNowMs()); // back to the "now" line
}

function playTimelapse() {
  const tl = state.timelapse;
  if (!tl) return;
  const last = state.layers.get(tl.ref).data.frames.times.length - 1;
  if (tl.frame >= last) applyFrame(0);
  tl.playing = true;
  $("tl-play").textContent = "❚❚";
  $("tl-play").setAttribute("aria-label", "Pause");
  const step = () => {
    if (!tl.playing || state.timelapse !== tl) return;
    if (tl.frame >= last) { pauseTimelapse(); return; }
    applyFrame(tl.frame + 1);
    tl.timer = setTimeout(step, 650 / tl.speed);
  };
  clearTimeout(tl.timer);
  tl.timer = setTimeout(step, 650 / tl.speed);
}

function pauseTimelapse() {
  const tl = state.timelapse;
  if (!tl) return;
  tl.playing = false;
  clearTimeout(tl.timer);
  $("tl-play").textContent = "▶";
  $("tl-play").setAttribute("aria-label", "Play");
}

$("tl-play").addEventListener("click", () => (state.timelapse?.playing ? pauseTimelapse() : playTimelapse()));
$("tl-slider").addEventListener("input", (e) => { pauseTimelapse(); applyFrame(Number(e.target.value)); });
$("tl-speed").addEventListener("click", () => {
  const tl = state.timelapse;
  if (!tl) return;
  tl.speed = SPEEDS[(SPEEDS.indexOf(tl.speed) + 1) % SPEEDS.length];
  $("tl-speed").textContent = `${tl.speed}×`;
});
$("tl-close").addEventListener("click", stopTimelapse);

// ---------------------------------------------------------------------------------------------
// Linked highlights: one value (a device/camera id, or a time-lapse frame) shared by the map,
// linked charts and custom visuals. Hovering any of them highlights it in the others.

const HL_SRC = "hl::src";
let hlFeatures = [];
let hlValue = null;

function syncHighlight() {
  if (!styleReady) return;
  const data = { type: "FeatureCollection", features: hlFeatures };
  if (!map.getSource(HL_SRC)) {
    map.addSource(HL_SRC, { type: "geojson", data });
    map.addLayer({ id: "hl::glow", type: "circle", source: HL_SRC, paint: { "circle-radius": 20, "circle-color": "#ff2d55", "circle-opacity": 0.2, "circle-blur": 0.35 } });
    map.addLayer({ id: "hl::ring", type: "circle", source: HL_SRC, paint: { "circle-radius": 12, "circle-opacity": 0, "circle-stroke-color": "#ff2d55", "circle-stroke-width": 3 } });
  } else {
    map.getSource(HL_SRC).setData(data);
    map.moveLayer("hl::glow");
    map.moveLayer("hl::ring");
  }
}

function hl3dFilter(value) {
  return ["all", ["==", ["get", "_kind"], "footprint"], ["==", ["to-string", ["get", "id"]], value === null || value === undefined ? "\u0000" : String(value)]];
}

function centerOf(geom) {
  const pts = coordsOf(geom, []);
  return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
}

function highlightOnMap(value) {
  hlFeatures = [];
  if (value !== null && value !== undefined) {
    const want = String(value);
    let shapes = [];
    for (const e of state.layers.values()) {
      for (const f of e.data.features) {
        if (String(f.properties?.id) !== want) continue;
        if (f.geometry?.type === "Point") hlFeatures.push({ type: "Feature", geometry: f.geometry, properties: {} });
        else if (f.properties._kind === "footprint") shapes.push(f);
      }
    }
    // A building with no marker (get_buildings_3d) rings its footprint's centre instead.
    if (!hlFeatures.length) hlFeatures = shapes.map((f) => ({ type: "Feature", geometry: { type: "Point", coordinates: centerOf(f.geometry) }, properties: {} }));
  }
  for (const id of state.hl3d) if (map.getLayer(id)) map.setFilter(id, hl3dFilter(value));
  syncHighlight();
}

function setChartHighlight(c, value) {
  try { c.view.signal("_hl", value ?? null).runAsync(); } catch { /* chart was removed */ }
}

function postToVisual(v, value, kind) {
  v.iframe.contentWindow?.postMessage({ __host: true, type: "highlight", value: value ?? null, kind }, "*");
}

function linkHover(key, value, origin) {
  if (key === "frame") {
    if (state.timelapse && value !== null && value !== undefined && Number.isFinite(Number(value))) {
      pauseTimelapse();
      applyFrame(Number(value));
    }
    return;
  }
  value = value ?? null;
  if (value === hlValue) return;
  hlValue = value;
  highlightOnMap(value);
  // Pan to a single highlighted feature that is off screen, unless the hover came from the map.
  if (origin !== "map" && hlFeatures.length === 1 && !map.getBounds().contains(hlFeatures[0].geometry.coordinates)) {
    map.easeTo({ center: hlFeatures[0].geometry.coordinates, duration: 600 });
  }
  for (const c of state.charts) if (c.link.key !== "frame") setChartHighlight(c, value);
  for (const v of state.visuals) if (v !== origin) postToVisual(v, value, "id");
}

// ---------------------------------------------------------------------------------------------
// Custom visuals: model-written JavaScript in a sandboxed iframe (opaque origin, no network).
// The CSP allows only the pinned library URLs and our two inline scripts (by hash).

const VISUAL_BOOT = `(function () {
  var post = function (m) { m.__visual = true; parent.postMessage(m, "*"); };
  var where = function (stack) {
    var m = /about:srcdoc:(\\d+):(\\d+)/.exec(String(stack || ""));
    return m && window.__codeLine ? " (line " + (Number(m[1]) - window.__codeLine + 1) + ")" : "";
  };
  window.__fail = function (err) {
    post({ type: "error", message: String((err && err.message) || err) + where(err && err.stack) });
  };
  window.addEventListener("error", function (e) {
    var line = e.lineno && window.__codeLine ? " (line " + (e.lineno - window.__codeLine + 1) + ")" : "";
    post({ type: "error", message: String(e.message || "error") + line });
  });
  window.addEventListener("unhandledrejection", function (e) { window.__fail(e.reason); });
  var handlers = [];
  window.highlight = function (value) { post({ type: "highlight", value: value === undefined ? null : value }); };
  window.onHighlight = function (fn) { if (typeof fn === "function") handlers.push(fn); };
  window.addEventListener("message", function (e) {
    if (e.source !== parent || !e.data || e.data.__host !== true || e.data.type !== "highlight") return;
    handlers.forEach(function (fn) { try { fn(e.data.value, e.data.kind); } catch (err) { window.__fail(err); } });
  });
  var last = 0;
  window.__size = function () {
    var h = Math.ceil(document.documentElement.scrollHeight);
    if (Math.abs(h - last) > 2) { last = h; post({ type: "size", height: h }); }
  };
  document.addEventListener("DOMContentLoaded", function () { new ResizeObserver(window.__size).observe(document.body); });
  window.__done = function () { window.__size(); post({ type: "ready" }); };
})();`;

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(buf)));
}

async function visualDoc(ev, width) {
  const libs = [...new Set([...(ev.libraries.includes("plot") ? ["d3"] : []), ...ev.libraries])];
  const urls = libs.map((l) => VISUAL_LIBS[l]).filter(Boolean);
  const payload = {
    data: ev.data, order: ev.data_order, layers: ev.layers, colors: ev.colors, theme: VISUAL_THEME, height: ev.height, width,
  };
  // JSON.parse reads it, so only "<" needs escaping (it could close the <script> element).
  const json = JSON.stringify(payload).replace(/</g, "\\u003c");
  // Keep the HTML parser inside the script element: `</script` would end it, `<!--` can confuse it.
  // (The regex avoids a literal "<!" + "--": in a classic script that starts an HTML-style comment.)
  const code = ev.code.replace(/<\/(script)/gi, "<\\/$1").replace(/<!(?=--)/g, "<\\!");
  const prelude = `window.__codeLine = LINE;
const __p = JSON.parse(document.getElementById("payload").textContent);
const root = document.getElementById("root");
const WIDTH = root.clientWidth || __p.width, HEIGHT = __p.height;
const DATA = __p.data, DATA_LIST = __p.order.map((k) => __p.data[k]), LAYERS = __p.layers;
const COLORS = __p.colors, THEME = __p.theme;
(async () => {
`;
  const run0 = `${prelude}${code}\n})().then(window.__done, window.__fail);`;
  const head = (csp) => `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>html,body{margin:0;padding:0;background:#fff;color:#16181d;font:13px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}#root{position:relative;width:100%;min-height:${ev.height}px}svg{display:block;max-width:100%}</style>
<script>${VISUAL_BOOT}</script>
${urls.map((u) => `<script src="${u}"></script>`).join("\n")}
<script type="application/json" id="payload">${json}</script>
</head><body><div id="root"></div>
<script>`;
  // The user's code starts on the line after the prelude's "(async () => {".
  const codeLine = (head("").split("\n").length - 1) + prelude.split("\n").length;
  const run = run0.replace("window.__codeLine = LINE;", `window.__codeLine = ${codeLine};`);
  const csp = [
    "default-src 'none'",
    `script-src 'sha256-${await sha256(VISUAL_BOOT)}' 'sha256-${await sha256(run)}' ${urls.join(" ")}`,
    "style-src 'unsafe-inline'", "img-src data: blob:", "font-src data:", "connect-src 'none'",
    "form-action 'none'", "base-uri 'none'",
  ].join("; ");
  return `${head(csp)}${run}</script></body></html>`;
}

async function renderVisual(v) {
  // Expanded, the visual gets the whole card: HEIGHT grows as well as WIDTH.
  const height = v.card.classList.contains("expanded") ? Math.max(v.ev.height, v.card.clientHeight - 60) : v.ev.height;
  v.iframe.style.height = `${height}px`;
  v.iframe.srcdoc = await visualDoc({ ...v.ev, height }, Math.max(200, v.iframe.clientWidth || v.card.clientWidth - 20));
}

window.addEventListener("message", (e) => {
  const v = state.visuals.find((x) => x.iframe.contentWindow === e.source);
  const m = e.data;
  if (!v || !m || m.__visual !== true) return;
  if (m.type === "size" && Number.isFinite(m.height)) {
    v.iframe.style.height = `${Math.max(80, Math.min(v.card.classList.contains("expanded") ? 4000 : 1400, m.height))}px`;
  } else if (m.type === "error") {
    const text = String(m.message).slice(0, 400);
    v.card.querySelector(".err")?.remove();
    const p = document.createElement("p");
    p.className = "err";
    p.textContent = `Visual error: ${text}`;
    v.card.appendChild(p);
    if (!state.visualErrors.includes(`${v.title}: ${text}`)) state.visualErrors.push(`${v.title}: ${text}`);
  } else if (m.type === "ready") {
    // A visual that just (re)loaded learns what is highlighted right now.
    if (state.timelapse) postToVisual(v, state.timelapse.frame, "frame");
    if (hlValue !== null) postToVisual(v, hlValue, "id");
  } else if (m.type === "highlight") {
    const val = m.value;
    if (val === null || typeof val === "string" || typeof val === "number") {
      linkHover(typeof val === "number" ? "frame" : "id", val, v);
    }
  }
});

function renderLegend() {
  const el = $("legend");
  if (!state.layers.size && !CITY.on) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  const stops = [];
  for (let i = 0; i < CITY_HEIGHTS.length; i += 2) stops.push(`${CITY_HEIGHTS[i + 1]} ${Math.round((100 * CITY_HEIGHTS[i]) / 60)}%`);
  const city = CITY.on
    ? `<div class="layer"><div class="row"><span class="title">3D city (DLS)</span><button class="x" data-city="1" title="Turn off the 3D city" aria-label="Turn off the 3D city">×</button></div><div class="ramp" style="background:linear-gradient(90deg, ${stops.join(", ")})"></div><div class="ramp-labels"><span>0 m</span><span>height to roof</span><span>60 m+</span></div></div>`
    : "";
  el.innerHTML = [...state.layers.values()].map(({ spec, stats }) => {
    let keys = "";
    if ((spec.style === "columns_3d" || spec.style === "heatmap") && stats) {
      keys = `<div class="ramp${spec.style === "heatmap" ? " heat" : ""}"></div><div class="ramp-labels"><span>${escapeHtml(fmt(stats.min))}</span><span>${escapeHtml(spec.value_field)}</span><span>${escapeHtml(fmt(stats.max))}</span></div>`;
    } else {
      keys = `<div class="keys">${Object.entries(spec.legend || {}).map(([k, c]) => `<span class="key"><span class="sw" style="background:${escapeHtml(c)}"></span>${escapeHtml(k.replace(/_/g, " "))}</span>`).join("")}</div>`;
    }
    return `<div class="layer"><div class="row"><span class="title">${escapeHtml(spec.title)} <span style="color:var(--muted);font-weight:400">(${spec.count})</span></span><button class="x" data-ref="${escapeHtml(spec.ref)}" title="Remove layer" aria-label="Remove layer">×</button></div>${keys}</div>`;
  }).join("") + city;
  el.querySelectorAll("button.x").forEach((b) => b.addEventListener("click", () => (b.dataset.city ? setCity3d(false) : removeLayer(b.dataset.ref))));
}

function fmt(v) { return Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : (Math.round(v * 10) / 10).toString(); }

function clearCallouts() { state.callouts.forEach((m) => m.remove()); state.callouts = []; }

function addCallouts(items, clearPrevious) {
  if (clearPrevious !== false) clearCallouts();
  for (const it of items) {
    const el = document.createElement("div");
    el.className = "callout";
    el.innerHTML = `<b>${escapeHtml(it.title)}</b>${it.text ? escapeHtml(it.text) : ""}`;
    state.callouts.push(new maplibregl.Marker({ element: el, anchor: "bottom", offset: [0, -14] }).setLngLat([it.lon, it.lat]).addTo(map));
  }
}

// Animations: pulsing halos and marching route dashes.
const DASHES = [[0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1], [2.5, 4, 0.5], [3, 4, 0], [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5], [0, 2, 3, 2], [0, 2.5, 3, 1.5], [0, 3, 3, 1], [0, 3.5, 3, 0.5]];
let lastDash = -1;
function animate(ts) {
  const t = (ts % 1600) / 1600;
  for (const id of state.pulses) {
    if (!map.getLayer(id)) continue;
    map.setPaintProperty(id, "circle-radius", 8 + t * 22);
    map.setPaintProperty(id, "circle-opacity", 0.45 * (1 - t));
  }
  const step = Math.floor(ts / 70) % DASHES.length;
  if (step !== lastDash) {
    lastDash = step;
    for (const id of state.dashLayers) if (map.getLayer(id)) map.setPaintProperty(id, "line-dasharray", DASHES[step]);
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

function applyUi(ev, msg) {
  switch (ev.action) {
    case "show_layer":
      showLayer({ ...ev.layer, askedAt: performance.now() }).catch((e) => msg.note(`Couldn't draw layer: ${e.message}`, true));
      break;
    case "fly_to": {
      state.flownAt = performance.now();
      const opts = { essential: true, duration: 2200 };
      if (ev.center) opts.center = ev.center;
      for (const k of ["zoom", "pitch", "bearing"]) if (ev[k] !== undefined) opts[k] = ev[k];
      map.flyTo(opts);
      break;
    }
    case "callouts":
      addCallouts(ev.items, ev.clear_previous);
      break;
    case "city_3d":
      setCity3d(Boolean(ev.on));
      break;
    case "clear":
      ev.ref ? removeLayer(ev.ref) : clearMap();
      break;
    case "chart":
      msg.chart(ev.title, ev.spec, ev.link, ev.time_field);
      break;
    case "visual":
      msg.visual(ev);
      break;
  }
}

function xEncodingOf(spec) {
  const unit = spec.encoding ? spec : (spec.layer || []).find((l) => l.encoding?.x);
  return unit?.encoding?.x || {};
}

function xScale(view) {
  try { return view.scale("x"); } catch { return null; }
}

// The pointer's x inside the plot area, or null outside it. Lines and areas have no single
// mark under the pointer, so charts over time work from this position instead.
function pointerX(view, event) {
  const svg = view.container()?.querySelector("svg");
  if (!svg) return null;
  const x = event.clientX - svg.getBoundingClientRect().left - view.origin()[0];
  return x >= 0 && x <= view.width() ? x : null;
}

// The row under the pointer in a time-lapse chart over frame numbers or frame labels.
function rowAtX(view, event, rows, field) {
  const x = pointerX(view, event), scale = xScale(view);
  if (x === null || !scale || !rows.length) return null;
  if (typeof scale.invert === "function") {
    const want = +scale.invert(x);
    return rows.reduce((best, r) => (Math.abs(r[field] - want) < Math.abs(best[field] - want) ? r : best), rows[0]);
  }
  if (typeof scale.step === "function") {
    const domain = scale.domain();
    const i = Math.floor((x - Math.min(...scale.range())) / scale.step());
    const label = domain[Math.max(0, Math.min(domain.length - 1, i))];
    return rows.find((r) => r[field] === label) || null;
  }
  return null;
}

function wireChart(view, link, spec) {
  const c = { view, link };
  state.charts.push(c);
  const rows = spec.data?.values || [];
  if (link.key === "frame") {
    const field = xEncodingOf(spec).field;
    view.addEventListener("pointermove", (event, item) => {
      const row = ["frame", "label"].includes(field) ? rowAtX(view, event, rows, field) : item?.datum;
      if (row && row.frame !== undefined) linkHover("frame", row.frame, c);
    });
    if (state.timelapse?.ref === link.layer) setChartHighlight(c, state.timelapse.frame);
  } else {
    view.addEventListener("pointermove", (_, item) => linkHover(link.key, item?.datum?.[link.key] ?? null, c));
    view.addEventListener("pointerout", () => linkHover(link.key, null, c));
  }
}

// ---------------------------------------------------------------------------------------------
// Time sync: every chart over real time has a red rule at its `_t` param. While a time-lapse
// plays, `_t` is the frame's moment; otherwise it is now, moved on every 30 seconds. Times are
// Nicosia wall-clock strings read as browser-local, the same way Vega reads the data.

function nicosiaNowMs() {
  const s = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Nicosia", hour12: false });
  return Date.parse(s.replace(" ", "T")); // "2026-10-20 14:05:33" -> local
}

function frameMs(entry, k) {
  return Date.parse(entry.data.frames.iso[k]);
}

function currentTimeMs() {
  const tl = state.timelapse;
  const entry = tl && state.layers.get(tl.ref);
  return entry ? frameMs(entry, tl.frame) : nicosiaNowMs();
}

function setChartTime(tc, ms) {
  const v = Number.isFinite(ms) && ms >= tc.min && ms <= tc.max ? ms : null; // hide outside the chart
  if (v === tc.last) return;
  tc.last = v;
  try { tc.view.signal("_t", v).runAsync(); } catch { /* chart was removed */ }
}

function setTimeAll(ms) {
  for (const tc of state.timeCharts) setChartTime(tc, ms);
}

// Jump the time-lapse to the frame nearest a moment, if the moment is within it.
function seekTimelapse(ms) {
  const tl = state.timelapse;
  const entry = tl && state.layers.get(tl.ref);
  if (!entry) return;
  const n = entry.data.frames.iso.length;
  let best = 0;
  for (let k = 1; k < n; k++) if (Math.abs(frameMs(entry, k) - ms) < Math.abs(frameMs(entry, best) - ms)) best = k;
  const step = n > 1 ? frameMs(entry, 1) - frameMs(entry, 0) : Infinity;
  if (Math.abs(frameMs(entry, best) - ms) > step) return;
  if (best !== tl.frame) { pauseTimelapse(); applyFrame(best); }
}

function wireTimeChart(view, spec, field) {
  const toMs = (v) => (typeof v === "number" ? v : Date.parse(v));
  const times = (spec.data?.values || []).map((r) => toMs(r[field])).filter(Number.isFinite).sort((a, b) => a - b);
  if (!times.length) return;
  let step = Infinity;
  for (let i = 1; i < times.length; i++) if (times[i] > times[i - 1]) step = Math.min(step, times[i] - times[i - 1]);
  if (!Number.isFinite(step)) step = 3600e3;
  // The last row covers one step (an hourly bucket, a day), so the rule may sit inside it.
  const tc = { view, min: times[0], max: times[times.length - 1] + step, last: undefined };
  state.timeCharts.push(tc);
  setChartTime(tc, currentTimeMs());
  if (!xEncodingOf(spec).timeUnit) { // a timeUnit axis (e.g. hour of day) has no single moment
    view.addEventListener("pointermove", (event) => {
      const x = pointerX(view, event), scale = xScale(view);
      if (x !== null && typeof scale?.invert === "function") seekTimelapse(+scale.invert(x));
    });
  }
}

setInterval(() => { if (!state.timelapse) setTimeAll(nicosiaNowMs()); }, 30e3);

function toggleExpand(v) {
  const expanded = v.card.classList.toggle("expanded");
  v.card.querySelector(".expand").textContent = expanded ? "Close" : "Expand";
  if (expanded) {
    const back = document.createElement("div");
    back.className = "backdrop";
    back.addEventListener("click", () => toggleExpand(v));
    document.body.appendChild(back);
    // Move the card out of the scrolling chat so `fixed` covers the window; keep its place.
    v.placeholder = document.createElement("div");
    v.card.replaceWith(v.placeholder);
    document.body.appendChild(v.card);
  } else {
    document.querySelector(".backdrop")?.remove();
    v.placeholder?.replaceWith(v.card);
  }
  // Draw again at the new size: the code reads WIDTH once.
  renderVisual(v);
}

// ---------------------------------------------------------------------------------------------
// Chat

marked.setOptions({ gfm: true, breaks: false });

function scrollDown() { const m = $("messages"); m.scrollTop = m.scrollHeight; }

function userMessage(text) {
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  $("messages").appendChild(el);
  scrollDown();
}

function botMessage() {
  const el = document.createElement("div");
  el.className = "msg bot";
  el.innerHTML = `<div class="typing" aria-label="Working"><i></i><i></i><i></i></div>`;
  $("messages").appendChild(el);
  let textPart = null, textBuf = "", chipsPart = null, thinkingBox = null, renderQueued = false;
  const chips = new Map();
  const usage = { input: 0, output: 0, cache_read: 0 };

  const dropTyping = () => el.querySelector(".typing")?.remove();
  const render = () => {
    renderQueued = false;
    if (textPart) textPart.innerHTML = DOMPurify.sanitize(marked.parse(textBuf));
    scrollDown();
  };
  return {
    text(t) {
      dropTyping();
      if (!textPart) {
        textPart = document.createElement("div");
        textPart.className = "md";
        el.appendChild(textPart);
        textBuf = "";
        chipsPart = null;
        thinkingBox = null;
      }
      textBuf += t;
      if (!renderQueued) { renderQueued = true; requestAnimationFrame(render); }
    },
    thinkingStart() {
      thinkingBox = document.createElement("details");
      thinkingBox.className = "thinking";
      thinkingBox.innerHTML = "<summary>Thinking</summary><div></div>";
      el.insertBefore(thinkingBox, el.querySelector(".typing"));
    },
    thinking(t) {
      if (!thinkingBox) this.thinkingStart();
      thinkingBox.querySelector("div").textContent += t;
    },
    toolStart(id, name) {
      textPart = null;
      if (!chipsPart) {
        chipsPart = document.createElement("div");
        chipsPart.className = "chips";
        el.insertBefore(chipsPart, el.querySelector(".typing"));
      }
      const chip = document.createElement("span");
      chip.className = "chip running";
      chip.innerHTML = `<span class="dot"></span><span>${escapeHtml(TOOL_LABELS[name] || name)}</span><span class="sum"></span>`;
      chipsPart.appendChild(chip);
      chips.set(id, chip);
      scrollDown();
    },
    toolEnd(id, ok, summary) {
      const chip = chips.get(id);
      if (!chip) return;
      chip.classList.remove("running");
      chip.classList.add(ok ? "ok" : "bad");
      if (summary) chip.querySelector(".sum").textContent = `· ${summary}`;
      chip.title = summary || "";
    },
    chart(title, spec, link, timeField) {
      dropTyping();
      const card = document.createElement("div");
      card.className = "chart-card";
      card.innerHTML = `<h3>${escapeHtml(title)}</h3><div class="vis"></div>`;
      el.appendChild(card);
      textPart = null;
      vegaEmbed(card.querySelector(".vis"), spec, { actions: { export: true, source: false, compiled: false, editor: false }, renderer: "svg" })
        .then((res) => {
          if (link) wireChart(res.view, link, spec);
          if (timeField) wireTimeChart(res.view, spec, timeField);
          scrollDown();
        })
        .catch((e) => { card.querySelector(".vis").innerHTML = `<p class="err">Chart error: ${escapeHtml(e.message)}</p>`; });
    },
    visual(ev) {
      dropTyping();
      const card = document.createElement("div");
      card.className = "chart-card visual-card";
      card.innerHTML = `<div class="card-head"><h3>${escapeHtml(ev.title)}</h3><button type="button" class="expand">Expand</button></div>`;
      const iframe = document.createElement("iframe");
      iframe.setAttribute("sandbox", "allow-scripts");
      iframe.setAttribute("title", ev.title);
      iframe.setAttribute("referrerpolicy", "no-referrer");
      card.appendChild(iframe);
      el.appendChild(card);
      textPart = null;
      const v = { iframe, card, ev, title: ev.title };
      state.visuals.push(v);
      card.querySelector(".expand").addEventListener("click", () => toggleExpand(v));
      renderVisual(v).then(scrollDown).catch((e) => this.note(`Couldn't draw the visual: ${e.message}`, true));
    },
    note(text, isError) {
      const p = document.createElement("p");
      p.className = isError ? "err" : "note";
      p.textContent = text;
      el.appendChild(p);
      scrollDown();
    },
    usage(u) { usage.input += u.input + u.cache_read + (u.cache_write || 0); usage.output += u.output; usage.cache_read += u.cache_read; },
    done() {
      dropTyping();
      if (usage.input) {
        const p = document.createElement("div");
        p.className = "usage";
        const cached = usage.input ? Math.round((100 * usage.cache_read) / usage.input) : 0;
        p.textContent = `${usage.input.toLocaleString()} tokens in (${cached}% cached) · ${usage.output.toLocaleString()} out`;
        el.appendChild(p);
      }
      scrollDown();
    },
  };
}

function mapContext() {
  const c = map.getCenter();
  return {
    map_view: { center: [c.lng, c.lat], zoom: map.getZoom() },
    layers_on_map: [...state.layers.values()].map((l) => `${l.spec.ref} (${l.spec.title})`),
    city_3d: CITY.on,
    user_location: me.loc,
    visual_errors: state.visualErrors.splice(0),
  };
}

async function send(text) {
  if (state.busy || !text.trim()) return;
  state.busy = true;
  $("send").disabled = true;
  $("welcome")?.remove();
  userMessage(text);
  const msg = botMessage();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: state.conversationId, context: mapContext() }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        for (const line of chunk.split("\n")) {
          if (line.startsWith("data: ")) handleEvent(JSON.parse(line.slice(6)), msg);
        }
      }
    }
  } catch (e) {
    msg.note(`Connection problem: ${e.message}`, true);
  } finally {
    msg.done();
    state.busy = false;
    $("send").disabled = false;
    $("input").focus();
  }
}

function handleEvent(ev, msg) {
  switch (ev.type) {
    case "conversation": state.conversationId = ev.id; break;
    case "text": msg.text(ev.text); break;
    case "thinking_start": msg.thinkingStart(); break;
    case "thinking": msg.thinking(ev.text); break;
    case "tool_start": msg.toolStart(ev.id, ev.name); break;
    case "tool_end": msg.toolEnd(ev.id, ev.ok, ev.summary); break;
    case "ui": applyUi(ev, msg); break;
    case "usage": msg.usage(ev); break;
    case "notice": msg.note(ev.message, false); break;
    case "error": msg.note(ev.message, true); break;
  }
}

// Composer
const input = $("input");
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 140)}px`; });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});
$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value;
  input.value = "";
  input.style.height = "auto";
  send(text);
});
$("new-chat").addEventListener("click", async () => {
  if (state.busy) return;
  if (state.conversationId) fetch(`/api/reset/${state.conversationId}`, { method: "POST" });
  state.conversationId = null;
  clearMap();
  for (const view of new Set([...state.charts, ...state.timeCharts].map((c) => c.view))) view.finalize();
  state.charts = [];
  state.timeCharts = [];
  state.visuals = [];
  state.visualErrors = [];
  hlValue = null;
  document.querySelector(".backdrop")?.remove();
  document.querySelectorAll("body > .visual-card").forEach((n) => n.remove());
  $("messages").innerHTML = "";
  map.flyTo({ center: [33.3615, 35.1725], zoom: 14.2, pitch: $("basemap").value === "liberty" ? 35 : 0, bearing: 0 });
});

const sugg = $("suggestions");
for (const s of SUGGESTIONS) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = s;
  b.addEventListener("click", () => send(s));
  sugg.appendChild(b);
}

fetch("/api/health").then((r) => r.json()).then((h) => {
  if (!h.has_credentials) {
    const b = $("banner");
    b.hidden = false;
    b.innerHTML = "No Anthropic API key found. Copy <code>.env.example</code> to <code>.env</code> in the project folder, set <code>ANTHROPIC_API_KEY</code>, and restart the chatbot. The map works without it.";
  }
}).catch(() => {});
