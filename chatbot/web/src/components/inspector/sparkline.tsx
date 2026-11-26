import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react"

import type { Band, Series } from "@/lib/entities"
import { hoverTime, registerTimeChart, unregisterView } from "@/lib/map/engine"
import { cn } from "@/lib/utils"

const SODIUM = "var(--sodium)"
const PAD = { top: 6, right: 2, bottom: 2, left: 2 }

function useWidth() {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(300)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(120, Math.round(e.contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

// The moment the rest of the twin is looking at (a time-lapse frame, the hovered moment, or now),
// so the sparkline's line moves with the charts on the stage.
function useSharedTime(min: number, max: number) {
  const [t, setT] = useState<number | null>(null)
  useEffect(() => {
    const view = { signal: (_name: string, v: number | null) => ({ runAsync: () => { setT(v); return Promise.resolve() } }) }
    registerTimeChart(view, min, max)
    return () => unregisterView(view)
  }, [min, max])
  return t
}

function fmt(v: number) {
  return Math.abs(v) >= 100 ? Math.round(v).toLocaleString("en-GB") : (Math.round(v * 10) / 10).toLocaleString("en-GB")
}

function timeOf(t: string, today: string) {
  const [d, hm] = t.split("T")
  return d === today ? hm : `${new Date(`${d}T12:00`).toLocaleDateString("en-GB", { weekday: "short" })} ${hm}`
}

// 24 hours of one reading: a line (or bars) in sodium with the states it went through along the
// bottom, in the map's colours, over the usual for each hour (a grey band, when the twin's memory
// has weeks of it). Pointer or arrow keys read out any moment; the city clock's moment is the
// dashed cursor.
export function Sparkline({ series, title, band = null, live = true }: { series: Series; title: string; band?: Band | null; live?: boolean }) {
  const [box, width] = useWidth()
  const pts = series.points
  const times = useMemo(() => pts.map((p) => Date.parse(p.t)), [pts])
  const shared = useSharedTime(times[0], times[times.length - 1])
  const [cursor, setCursor] = useState<number | null>(null)
  // The usual at each of the series' moments.
  const usual = useMemo(() => {
    if (!band) return null
    const by = new Map(band.points.map((b) => [b.t, b]))
    const out = pts.map((p) => by.get(p.t) ?? null)
    return out.some((u) => u && u.lo !== null && u.hi !== null) ? out : null
  }, [band, pts])

  const strip = series.state_colors && Object.keys(series.state_colors).length > 0
  const statesOnly = series.draw === "states"
  // A bay's states get a small plot above them for how often it is usually occupied.
  const plotH = statesOnly ? (usual ? 22 : 0) : 56
  const stripH = statesOnly ? 22 : strip ? 6 : 0
  const height = PAD.top + plotH + (stripH ? stripH + (plotH ? 4 : 0) : 0) + PAD.bottom
  const vals = statesOnly ? [0, 100] : pts.map((p) => p.v).filter((v): v is number => typeof v === "number")
  const bandVals = usual ? usual.flatMap((u) => (u ? [u.lo, u.hi] : [])).filter((v): v is number => typeof v === "number") : []
  const lo = Math.min(0, ...vals, ...bandVals, series.threshold?.value ?? Infinity)
  const hi = Math.max(...vals, ...bandVals, series.threshold?.value ?? -Infinity, lo + 1)
  const x0 = PAD.left, x1 = width - PAD.right
  const n = pts.length
  const xAt = (i: number) => x0 + (n <= 1 ? 0 : (i / (n - 1)) * (x1 - x0))
  const yAt = (v: number) => PAD.top + plotH - ((v - lo) / (hi - lo)) * plotH

  const line = useMemo(() => {
    let d = "", area = "", run: number[] = []
    const flush = () => {
      if (run.length) {
        area += `M${xAt(run[0])},${yAt(lo)}` + run.map((i) => `L${xAt(i)},${yAt(pts[i].v!)}`).join("") + `L${xAt(run[run.length - 1])},${yAt(lo)}Z`
      }
      run = []
    }
    pts.forEach((p, i) => {
      if (typeof p.v !== "number") { flush(); return }
      d += `${run.length ? "L" : "M"}${xAt(i)},${yAt(p.v)}`
      run.push(i)
    })
    flush()
    return { d, area }
  }, [pts, width, lo, hi]) // eslint-disable-line react-hooks/exhaustive-deps

  // The usual band (between the quartiles) and its median, in runs where it is known.
  const typical = useMemo(() => {
    if (!usual) return null
    let area = "", mid = "", run: number[] = []
    const flush = () => {
      if (run.length > 1) {
        area += `M${xAt(run[0])},${yAt(usual[run[0]]!.hi!)}` + run.slice(1).map((i) => `L${xAt(i)},${yAt(usual[i]!.hi!)}`).join("")
          + [...run].reverse().map((i) => `L${xAt(i)},${yAt(usual[i]!.lo!)}`).join("") + "Z"
        mid += run.map((i, k) => `${k ? "L" : "M"}${xAt(i)},${yAt(usual[i]!.mid ?? usual[i]!.lo!)}`).join("")
      }
      run = []
    }
    usual.forEach((u, i) => { if (u && u.lo !== null && u.hi !== null) run.push(i); else flush() })
    flush()
    return { area, mid }
  }, [usual, width, lo, hi]) // eslint-disable-line react-hooks/exhaustive-deps

  const at = cursor ?? (shared !== null ? nearest(times, shared) : null)
  const today = pts[n - 1].t.slice(0, 10)
  const readout = (i: number) => {
    const p = pts[i]
    const value = typeof p.v === "number" ? `${fmt(p.v)}${series.unit ? ` ${series.unit}` : ""}` : null
    const state = p.s ? p.s.replace(/_/g, " ") : null
    return [timeOf(p.t, today), statesOnly ? state : value, !statesOnly && strip ? state : null].filter(Boolean).join(", ")
  }

  const move = (i: number | null) => {
    setCursor(i)
    hoverTime(i === null ? null : times[i])
  }
  const onPointer = (e: PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const f = (e.clientX - r.left - x0) / Math.max(1, x1 - x0)
    move(Math.max(0, Math.min(n - 1, Math.round(f * (n - 1)))))
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 4 : 1
    const here = cursor ?? n - 1
    const next = { ArrowLeft: here - step, ArrowRight: here + step, Home: 0, End: n - 1 }[e.key]
    if (next === undefined) { if (e.key === "Escape" && cursor !== null) { e.stopPropagation(); move(null) } return }
    e.preventDefault()
    move(Math.max(0, Math.min(n - 1, next)))
  }

  const barW = Math.max(2, (x1 - x0) / n - 2)
  const summary = series.draw === "states" && series.summary?.occupied_pct !== undefined
    ? `occupied ${series.summary.occupied_pct}% of the time`
    : series.summary ? `from ${fmt(series.summary.min!)} to ${fmt(series.summary.max!)}${series.unit ? ` ${series.unit}` : ""}` : ""

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-3 text-[12px] text-muted-foreground">
        <span className="font-condensed">{title}</span>
        <span className="font-readout text-foreground" aria-live="polite">{cursor !== null ? readout(cursor) : ""}</span>
      </div>
      <div
        ref={box}
        tabIndex={0}
        role="group"
        aria-label={`${title}, ${summary}. Use the arrow keys to read a moment.`}
        onKeyDown={onKey}
        onBlur={() => move(null)}
        className="rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <svg
          width={width}
          height={height}
          className="block touch-none"
          onPointerMove={onPointer}
          onPointerDown={onPointer}
          onPointerLeave={() => move(null)}
          aria-hidden="true"
        >
          {typical && (
            <>
              <path d={typical.area} style={{ fill: "var(--muted-foreground)" }} fillOpacity={0.22} />
              <path d={typical.mid} fill="none" style={{ stroke: "var(--muted-foreground)" }} strokeOpacity={0.75} strokeWidth={1} strokeDasharray="3 2" />
            </>
          )}
          {!statesOnly && (
            <line x1={x0} x2={x1} y1={yAt(lo)} y2={yAt(lo)} style={{ stroke: "var(--line)" }} strokeWidth={1} />
          )}
          {series.threshold && !statesOnly && (
            <line x1={x0} x2={x1} y1={yAt(series.threshold.value)} y2={yAt(series.threshold.value)} style={{ stroke: "var(--signal)" }} strokeOpacity={0.55} strokeDasharray="3 3" strokeWidth={1} />
          )}
          {series.draw === "bars" ? pts.map((p, i) => typeof p.v === "number" && (
            <rect key={i} x={xAt(i) - barW / 2} y={yAt(p.v)} width={barW} height={Math.max(1, yAt(lo) - yAt(p.v))} rx={2} style={{ fill: SODIUM }} fillOpacity={at === i ? 1 : 0.8} />
          )) : !statesOnly && (
            <>
              <path d={line.area} style={{ fill: SODIUM }} fillOpacity={0.14} />
              <path d={line.d} fill="none" style={{ stroke: SODIUM }} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            </>
          )}
          {stripH > 0 && pts.map((p, i) => (
            <rect
              key={`s${i}`}
              x={xAt(i) - (x1 - x0) / (2 * Math.max(1, n - 1))}
              y={PAD.top + plotH + (plotH ? 4 : 0)}
              width={(x1 - x0) / Math.max(1, n - 1) + 0.5}
              height={stripH}
              style={{ fill: series.state_colors?.[p.s ?? ""] ?? "var(--muted)" }}
              fillOpacity={0.85}
            />
          ))}
          {at !== null && (
            <>
              <line x1={xAt(at)} x2={xAt(at)} y1={PAD.top - 4} y2={height - PAD.bottom} style={{ stroke: "var(--foreground)" }} strokeOpacity={cursor !== null ? 0.7 : 0.45} strokeDasharray={cursor !== null ? undefined : "3 2"} strokeWidth={1} />
              {!statesOnly && typeof pts[at].v === "number" && series.draw !== "bars" && (
                <circle cx={xAt(at)} cy={yAt(pts[at].v!)} r={4} style={{ fill: SODIUM, stroke: "var(--console)" }} strokeWidth={2} />
              )}
            </>
          )}
        </svg>
      </div>
      <div className={cn("flex justify-between gap-2 font-readout text-[11.5px] text-muted-foreground")}>
        <span>{timeOf(series.from, today)}</span>
        <span>{summary}</span>
        <span className={live ? "" : "text-foreground"}>{live ? "now" : timeOf(series.to, today)}</span>
      </div>
      {usual && band && (
        <p className="text-[12px] leading-snug text-muted-foreground">
          {statesOnly ? "Grey: how often it is usually occupied at that hour" : "Grey band: the usual for that hour"} of that weekday, from {band.weeks} week{band.weeks === 1 ? "" : "s"} in the twin's memory.
        </p>
      )}
    </div>
  )
}

function nearest(times: number[], t: number) {
  if (t < times[0] || t > times[times.length - 1]) return null
  let best = 0
  for (let i = 1; i < times.length; i++) if (Math.abs(times[i] - t) < Math.abs(times[best] - t)) best = i
  return best
}
