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
];
const TOOL_LABELS = {
  city_overview: "Reading the city's sensors", find_devices: "Finding devices",
  get_device: "Reading a device", get_alarms: "Checking alarms", get_traffic: "Counting traffic",
  get_speed: "Checking traffic speed", get_weather: "Checking the weather",
  geocode: "Looking up a place", find_places: "Searching places", plan_route: "Planning the route",
  reachable_area: "Working out the reachable area", show_on_map: "Drawing on the map",
  map_fly_to: "Moving the map", map_callouts: "Adding callouts", map_clear: "Clearing the map",
  show_chart: "Drawing a chart", web_search: "Searching the web",
};

const $ = (id) => document.getElementById(id);
const state = {
  conversationId: null,
  busy: false,
  layers: new Map(), // ref -> {spec, data, ids: [], stats}
  callouts: [],
  pulses: new Set(), // halo layer ids
  dashLayers: new Set(),
};

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
// A page opened in a hidden tab measures a 0-size container; keep the canvas in step.
new ResizeObserver(() => map.resize()).observe($("map"));
map.on("load", () => map.resize());

$("basemap").addEventListener("change", (e) => {
  map.setStyle(BASEMAPS[e.target.value]);
  map.once("style.load", () => {
    for (const entry of state.layers.values()) addToMap(entry);
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
  map.on("mouseleave", layerId, () => (map.getCanvas().style.cursor = ""));
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
  entry.stats = vf ? numericStats(data.features, vf) : null;

  if (spec.style === "columns_3d") {
    const st = entry.stats || { min: 0, max: 1 };
    const span = st.max - st.min || 1;
    const pts = data.features.filter((f) => f.geometry?.type === "Point");
    // Column width follows the layer's extent: 6 m in a street, up to 45 m city-wide.
    const half = Math.max(6, Math.min(45, extentMeters(pts) / 220));
    const polys = pts.map((f) => {
      const v = Number(f.properties[vf]);
      const t = Number.isFinite(v) ? (v - st.min) / span : 0;
      return {
        type: "Feature",
        geometry: { type: "Polygon", coordinates: squareAround(f.geometry.coordinates, half) },
        properties: { ...f.properties, _h: half * 2 + t * half * 40, _c: rampColor(t) },
      };
    });
    map.addSource(src, { type: "geojson", data: { type: "FeatureCollection", features: polys } });
    addLayerSafe({
      id: `${ref}::columns`, type: "fill-extrusion", source: src,
      paint: { "fill-extrusion-color": ["get", "_c"], "fill-extrusion-height": ["get", "_h"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.9 },
    }, entry.ids);
    bindPopup(`${ref}::columns`);
    if (spec.fit === false && map.getPitch() < 45) map.easeTo({ pitch: 55, duration: 900 });
    return;
  }

  map.addSource(src, { type: "geojson", data });

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
    const polys = ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false];
    addLayerSafe({ id: `${ref}::fill`, type: "fill", source: src, filter: polys, paint: { "fill-color": ["get", "color"], "fill-opacity": 0.16 } }, entry.ids);
    addLayerSafe({ id: `${ref}::outline`, type: "line", source: src, filter: polys, paint: { "line-color": ["get", "color"], "line-width": 2.2 } }, entry.ids);
  }

  if (spec.style === "route") {
    const lines = ["==", ["geometry-type"], "LineString"];
    addLayerSafe({ id: `${ref}::casing`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#ffffff", "line-width": 10, "line-opacity": 0.85 } }, entry.ids);
    addLayerSafe({ id: `${ref}::line`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": ["get", "color"], "line-width": 5.5 } }, entry.ids);
    addLayerSafe({ id: `${ref}::dash`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#ffffff", "line-width": 2.5, "line-dasharray": [0, 4, 3] } }, entry.ids);
    state.dashLayers.add(`${ref}::dash`);
    addLayerSafe({ id: `${ref}::stops`, type: "circle", source: src, filter: points, paint: { "circle-radius": 11, "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 2 } }, entry.ids);
    addLayerSafe({ id: `${ref}::num`, type: "symbol", source: src, filter: points, layout: { "text-field": ["to-string", ["get", "n"]], "text-font": FONT_BOLD, "text-size": 12, "text-allow-overlap": true }, paint: { "text-color": "#fff" } }, entry.ids);
    bindPopup(`${ref}::stops`);
    if (spec.labels !== false) addLabels(entry, src, points, 1.6);
    return;
  }

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
  }
  if (map.getSource(`${ref}::src`)) map.removeSource(`${ref}::src`);
}

function removeLayer(ref) {
  removeFromMap(ref);
  state.layers.delete(ref);
  renderLegend();
}

function clearMap() {
  for (const ref of [...state.layers.keys()]) removeFromMap(ref);
  state.layers.clear();
  clearCallouts();
  renderLegend();
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

async function showLayer(spec) {
  const res = await fetch(spec.url);
  if (!res.ok) throw new Error(`layer ${spec.ref}: HTTP ${res.status}`);
  const data = await res.json();
  if (state.layers.has(spec.ref)) removeFromMap(spec.ref);
  const entry = { spec, data, ids: [] };
  state.layers.set(spec.ref, entry);
  const pitch = spec.style === "columns_3d" ? Math.max(55, map.getPitch()) : map.getPitch();
  const draw = () => { addToMap(entry); if (spec.fit !== false) fitTo(data, pitch); renderLegend(); };
  map.isStyleLoaded() ? draw() : map.once("idle", draw);
}

function renderLegend() {
  const el = $("legend");
  if (!state.layers.size) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = [...state.layers.values()].map(({ spec, stats }) => {
    let keys = "";
    if ((spec.style === "columns_3d" || spec.style === "heatmap") && stats) {
      keys = `<div class="ramp${spec.style === "heatmap" ? " heat" : ""}"></div><div class="ramp-labels"><span>${escapeHtml(fmt(stats.min))}</span><span>${escapeHtml(spec.value_field)}</span><span>${escapeHtml(fmt(stats.max))}</span></div>`;
    } else {
      keys = `<div class="keys">${Object.entries(spec.legend || {}).map(([k, c]) => `<span class="key"><span class="sw" style="background:${escapeHtml(c)}"></span>${escapeHtml(k.replace(/_/g, " "))}</span>`).join("")}</div>`;
    }
    return `<div class="layer"><div class="row"><span class="title">${escapeHtml(spec.title)} <span style="color:var(--muted);font-weight:400">(${spec.count})</span></span><button class="x" data-ref="${escapeHtml(spec.ref)}" title="Remove layer" aria-label="Remove layer">×</button></div>${keys}</div>`;
  }).join("");
  el.querySelectorAll("button.x").forEach((b) => b.addEventListener("click", () => removeLayer(b.dataset.ref)));
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
      showLayer(ev.layer).catch((e) => msg.note(`Couldn't draw layer: ${e.message}`, true));
      break;
    case "fly_to": {
      const opts = { essential: true, duration: 2200 };
      if (ev.center) opts.center = ev.center;
      for (const k of ["zoom", "pitch", "bearing"]) if (ev[k] !== undefined) opts[k] = ev[k];
      map.flyTo(opts);
      break;
    }
    case "callouts":
      addCallouts(ev.items, ev.clear_previous);
      break;
    case "clear":
      ev.ref ? removeLayer(ev.ref) : clearMap();
      break;
    case "chart":
      msg.chart(ev.title, ev.spec);
      break;
  }
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
    chart(title, spec) {
      dropTyping();
      const card = document.createElement("div");
      card.className = "chart-card";
      card.innerHTML = `<h3>${escapeHtml(title)}</h3><div class="vis"></div>`;
      el.appendChild(card);
      textPart = null;
      vegaEmbed(card.querySelector(".vis"), spec, { actions: { export: true, source: false, compiled: false, editor: false }, renderer: "svg" })
        .then(scrollDown)
        .catch((e) => { card.querySelector(".vis").innerHTML = `<p class="err">Chart error: ${escapeHtml(e.message)}</p>`; });
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
