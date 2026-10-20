/* Vega-Lite charts on the stage: the console theme, and the wiring that links a chart to the
   map (hovering a row highlights its device) and to time (a rule at "now" or the time-lapse). */
import type { Result } from "vega-embed"

import {
  linkHover, registerChart, registerTimeChart, seekTimelapse, unregisterView, SODIUM,
  type ChartLink,
} from "@/lib/map/engine"

/* eslint-disable @typescript-eslint/no-explicit-any */
const INK = "#e4ebf4"
const MUTED = "#8798b0"
const SANS = "Sofia Sans Variable"
const CONDENSED = "Sofia Sans Condensed Variable"
const GRID = "rgba(138,160,190,0.14)"
const DOMAIN = "rgba(138,160,190,0.34)"

// Series colours tuned for the night console: sodium leads, the rest stay clear of it.
export const CATEGORICAL = ["#ffae45", "#6cb4ff", "#6fdcbf", "#ff8a7a", "#c49bff", "#f2d06b", "#5fd0ef", "#ff9ccb", "#a8b5c7", "#7fd18a"]

function config(height: number) {
  return {
    background: null,
    font: SANS,
    padding: { left: 2, right: 8, top: 6, bottom: 2 },
    view: { stroke: null, continuousHeight: height },
    axis: {
      labelColor: MUTED, labelFont: CONDENSED, labelFontSize: 12, labelPadding: 4,
      titleColor: "#aebbcc", titleFont: SANS, titleFontSize: 12, titleFontWeight: 500, titlePadding: 8,
      gridColor: GRID, domainColor: DOMAIN, tickColor: DOMAIN,
    },
    legend: {
      labelColor: "#c3cedb", labelFont: CONDENSED, labelFontSize: 12,
      titleColor: MUTED, titleFont: SANS, titleFontSize: 11.5, titleFontWeight: 500,
    },
    header: { labelColor: "#c3cedb", titleColor: MUTED, labelFont: SANS },
    title: { color: INK, subtitleColor: MUTED, font: SANS, fontWeight: 600, fontSize: 14, anchor: "start" },
    range: { category: CATEGORICAL },
    mark: { color: SODIUM },
    bar: { color: SODIUM, cornerRadiusEnd: 2 },
    line: { color: SODIUM, strokeWidth: 2 },
    area: { color: SODIUM, fillOpacity: 0.28, line: true },
    point: { color: SODIUM, filled: true },
    rule: { color: DOMAIN },
    text: { color: INK, font: CONDENSED },
    arc: { stroke: "#15233a", strokeWidth: 1 },
  }
}

function xEncodingOf(spec: any) {
  const unit = spec.encoding ? spec : (spec.layer || []).find((l: any) => l.encoding?.x)
  return unit?.encoding?.x || {}
}

function xScale(view: any) {
  try { return view.scale("x") } catch { return null }
}

// The pointer's x inside the plot area, or null outside it. Lines and areas have no single
// mark under the pointer, so charts over time work from this position instead.
function pointerX(view: any, event: MouseEvent) {
  const svg = view.container()?.querySelector("svg")
  if (!svg) return null
  const x = event.clientX - svg.getBoundingClientRect().left - view.origin()[0]
  return x >= 0 && x <= view.width() ? x : null
}

// The row under the pointer in a time-lapse chart over frame numbers or frame labels.
function rowAtX(view: any, event: MouseEvent, rows: any[], field: string) {
  const x = pointerX(view, event), scale = xScale(view)
  if (x === null || !scale || !rows.length) return null
  if (typeof scale.invert === "function") {
    const want = +scale.invert(x)
    return rows.reduce((best, r) => (Math.abs(r[field] - want) < Math.abs(best[field] - want) ? r : best), rows[0])
  }
  if (typeof scale.step === "function") {
    const domain = scale.domain()
    const i = Math.floor((x - Math.min(...scale.range())) / scale.step())
    const label = domain[Math.max(0, Math.min(domain.length - 1, i))]
    return rows.find((r) => r[field] === label) || null
  }
  return null
}

function wireChart(view: any, link: ChartLink, spec: any) {
  const c = registerChart(view, link)
  const rows = spec.data?.values || []
  if (link.key === "frame") {
    const field = xEncodingOf(spec).field
    view.addEventListener("pointermove", (event: MouseEvent, item: any) => {
      const row = ["frame", "label"].includes(field) ? rowAtX(view, event, rows, field) : item?.datum
      if (row && row.frame !== undefined) linkHover("frame", row.frame, c)
    })
  } else {
    view.addEventListener("pointermove", (_: unknown, item: any) => linkHover(link.key, item?.datum?.[link.key] ?? null, c))
    view.addEventListener("pointerout", () => linkHover(link.key, null, c))
  }
}

function wireTimeChart(view: any, spec: any, field: string) {
  const toMs = (v: unknown) => (typeof v === "number" ? v : Date.parse(String(v)))
  const times = (spec.data?.values || []).map((r: any) => toMs(r[field])).filter(Number.isFinite).sort((a: number, b: number) => a - b)
  if (!times.length) return
  let step = Infinity
  for (let i = 1; i < times.length; i++) if (times[i] > times[i - 1]) step = Math.min(step, times[i] - times[i - 1])
  if (!Number.isFinite(step)) step = 3600e3
  // The last row covers one step (an hourly bucket, a day), so the rule may sit inside it.
  registerTimeChart(view, times[0], times[times.length - 1] + step)
  if (!xEncodingOf(spec).timeUnit) { // a timeUnit axis (e.g. hour of day) has no single moment
    view.addEventListener("pointermove", (event: MouseEvent) => {
      const x = pointerX(view, event), scale = xScale(view)
      if (x !== null && typeof scale?.invert === "function") seekTimelapse(+scale.invert(x))
    })
  }
}

export type ChartPayload = { title: string; spec: any; link?: ChartLink | null; time_field?: string | null }

// Draw a chart into `el`; returns a function that takes it down again.
export async function drawChart(el: HTMLElement, chart: ChartPayload, height: number): Promise<() => void> {
  const { default: embed } = await import("vega-embed") // Vega loads with the first chart
  const res: Result = await embed(el, chart.spec, {
    actions: { export: true, source: false, compiled: false, editor: false },
    renderer: "svg",
    config: config(height) as any,
    tooltip: { theme: "dark" },
  })
  const view = res.view
  if (chart.link) wireChart(view, chart.link, chart.spec)
  if (chart.time_field) wireTimeChart(view, chart.spec, chart.time_field)
  return () => {
    unregisterView(view)
    res.finalize()
  }
}
