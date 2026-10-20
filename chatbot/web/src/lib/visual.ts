/* Custom visuals: model-written JavaScript in a sandboxed iframe (opaque origin, no network).
   The CSP allows only the pinned library URLs and our two inline scripts (by hash). */
import { CATEGORICAL } from "@/lib/charts"
import { RAMP, SODIUM } from "@/lib/map/engine"

/* eslint-disable @typescript-eslint/no-explicit-any */

// Libraries a custom visual may load, pinned. The sandbox's CSP allows exactly these URLs.
const VISUAL_LIBS: Record<string, string> = {
  d3: "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js",
  plot: "https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6.17/dist/plot.umd.min.js",
  echarts: "https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js",
  three: "https://cdn.jsdelivr.net/npm/three@0.159.0/build/three.min.js",
}

// What the visual's code sees as THEME: the stage's console palette.
export const VISUAL_THEME = {
  background: "#15233a", text: "#e4ebf4", muted: "#8798b0", grid: "rgba(138,160,190,0.18)",
  accent: SODIUM, ramp: RAMP, categorical: CATEGORICAL,
}

export type VisualPayload = {
  title: string; code: string; libraries: string[]; data: Record<string, unknown[]>
  data_order: string[]; layers: Record<string, unknown>; colors: Record<string, string>; height: number
}

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
})();`

async function sha256(text: string) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
}

export async function visualDoc(ev: VisualPayload, width: number, height: number) {
  const libs = [...new Set([...(ev.libraries.includes("plot") ? ["d3"] : []), ...ev.libraries])]
  const urls = libs.map((l) => VISUAL_LIBS[l]).filter(Boolean)
  const payload = {
    data: ev.data, order: ev.data_order, layers: ev.layers, colors: ev.colors, theme: VISUAL_THEME, height, width,
  }
  // JSON.parse reads it, so only "<" needs escaping (it could close the <script> element).
  const json = JSON.stringify(payload).replace(/</g, "\\u003c")
  // Keep the HTML parser inside the script element: `</script` would end it, `<!--` can confuse it.
  // (The regex avoids a literal "<!" + "--": in a classic script that starts an HTML-style comment.)
  const code = ev.code.replace(/<\/(script)/gi, "<\\/$1").replace(/<!(?=--)/g, "<\\!")
  const prelude = `window.__codeLine = LINE;
const __p = JSON.parse(document.getElementById("payload").textContent);
const root = document.getElementById("root");
const WIDTH = root.clientWidth || __p.width, HEIGHT = __p.height;
const DATA = __p.data, DATA_LIST = __p.order.map((k) => __p.data[k]), LAYERS = __p.layers;
const COLORS = __p.colors, THEME = __p.theme;
(async () => {
`
  const run0 = `${prelude}${code}\n})().then(window.__done, window.__fail);`
  const head = (csp: string) => `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>html,body{margin:0;padding:0;background:${VISUAL_THEME.background};color:${VISUAL_THEME.text};font:13px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color-scheme:dark}#root{position:relative;width:100%;min-height:${height}px}svg{display:block;max-width:100%}</style>
<script>${VISUAL_BOOT}</script>
${urls.map((u) => `<script src="${u}"></script>`).join("\n")}
<script type="application/json" id="payload">${json}</script>
</head><body><div id="root"></div>
<script>`
  // The user's code starts on the line after the prelude's "(async () => {".
  const codeLine = (head("").split("\n").length - 1) + prelude.split("\n").length
  const run = run0.replace("window.__codeLine = LINE;", `window.__codeLine = ${codeLine};`)
  const csp = [
    "default-src 'none'",
    `script-src 'sha256-${await sha256(VISUAL_BOOT)}' 'sha256-${await sha256(run)}' ${urls.join(" ")}`,
    "style-src 'unsafe-inline'", "img-src data: blob:", "font-src data:", "connect-src 'none'",
    "form-action 'none'", "base-uri 'none'",
  ].join("; ")
  return `${head(csp)}${run}</script></body></html>`
}

export function isVisualPayload(ev: any): ev is VisualPayload {
  return typeof ev?.code === "string"
}
