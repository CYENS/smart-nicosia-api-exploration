/* Pieces the environment's panels share: the legends of its layers (in the colours of the base
   map on show), a day chart of how much of a square is in the sun, and a spot's day strip of
   sun, shade and night. Charts read out on hover and with the arrow keys. */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react"

import { SHADE, SUN_HOURS, SUN_HOUR_LABELS, bandColors } from "@/lib/environment/colors"
import { fmtValue, type DayPoint, type Surface, type Wind } from "@/lib/environment/store"
import { useMapUi } from "@/lib/map/engine"
import { cn } from "@/lib/utils"

function useBase() {
  return useMapUi((s) => s.basemap)
}

// A stepped legend: one swatch per band, its name under it.
function Steps({ colors, labels, label, className }: { colors: string[]; labels: string[]; label: string; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-1", className)} role="img" aria-label={`${label}: ${labels.join(", ")}`}>
      <div className="flex h-2.5 gap-[2px] overflow-hidden rounded-sm">
        {colors.map((c, i) => <span key={i} className="flex-1" style={{ background: c }} />)}
      </div>
      <div className="flex gap-[2px] font-readout text-[11px] leading-tight text-muted-foreground" aria-hidden="true">
        {labels.map((l, i) => <span key={i} className="min-w-0 flex-1 truncate">{l}</span>)}
      </div>
    </div>
  )
}

export function SurfaceLegend({ surface }: { surface: Pick<Surface, "metric" | "unit" | "bands" | "background" | "model_bias" | "label" | "rests_on"> }) {
  const base = useBase()
  const bands = surface.bands ?? []
  const colors = bandColors(surface.metric, base).slice(0, bands.length)
  const stationsOnly = surface.background === "stations"
  return (
    <div className="flex flex-col gap-2">
      <Steps colors={colors} labels={bands.map((b) => b.label)} label={`${surface.label} bands`} />
      <p className="text-[12px] leading-snug text-muted-foreground">
        {bands.filter((b) => b.to !== null).map((b) => `${b.label} under ${b.to}`).slice(0, 3).join(", ")}{surface.unit ? ` ${surface.unit}` : ""}.{" "}
        {stationsOnly
          ? "Only near the stations: it fades out between them, where nothing is measured."
          : `Strong near a station, faded where only the regional model speaks${surface.model_bias ? ` (corrected by ${surface.model_bias > 0 ? "+" : ""}${fmtValue(surface.model_bias, surface.unit)} to match the stations)` : ""}.`}
      </p>
      {surface.rests_on && <RestsOnLine rests={surface.rests_on} />}
    </div>
  )
}

// Which official stations the surface rests on (drawn as rings), or why they are left out.
function RestsOnLine({ rests }: { rests: NonNullable<Surface["rests_on"]> }) {
  const city = rests.city_sensors
  if (rests.reference.length) {
    const names = rests.reference.map((r) => r.name.replace(/^Nicosia /, "")).join(", ")
    const when = rests.reference[0].as_of?.slice(11, 16)
    return (
      <p className="text-[12px] leading-snug text-muted-foreground">
        Rests on official stations (the rings): {names}{when ? `, ${when}` : ""}{city ? `, and ${city} city sensor${city === 1 ? "" : "s"}` : ""}.
      </p>
    )
  }
  if (rests.left_out.length) {
    return (
      <p className="text-[12px] leading-snug text-muted-foreground">
        The official stations aren't used: {rests.left_out[0].why}. Move the clock back a few hours to see the surface rest on them.
      </p>
    )
  }
  return null
}

export function SunHoursLegend() {
  const base = useBase()
  return <Steps colors={SUN_HOURS[base]} labels={["<2", "2", "4", "6", "8", "10+ h"]} label={`Hours of direct sun: ${SUN_HOUR_LABELS.join(", ")}`} />
}

export function ShadeLegend() {
  return (
    <ul className="flex gap-4 text-[12.5px] text-muted-foreground">
      <li className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm" style={{ background: SHADE.shade }} aria-hidden="true" />In shade</li>
      <li className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm" style={{ background: SHADE.sun }} aria-hidden="true" />In sun</li>
    </ul>
  )
}

export function windLine(w: Wind | null) {
  if (!w?.known) return null
  const c = w.centre
  if (c.speed_ms === null) return null
  const kmh = Math.round(c.speed_ms * 3.6)
  return `${kmh} km/h from the ${c.from} over the centre${c.gusts_ms ? `, gusts to ${Math.round(c.gusts_ms * 3.6)}` : ""}`
}

// -- the day chart ---------------------------------------------------------------------------

function useWidth(min = 160) {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(280)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(min, Math.round(e.contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [min])
  return [ref, w] as const
}

// How much of the open ground is in direct sun through the day, every 15 minutes: an area in
// the sun's amber over a night band where the sun is down. Pointer or arrow keys read a moment.
export function DayChart({ points, title, mark }: { points: DayPoint[]; title: string; mark?: string | null }) {
  const [box, width] = useWidth()
  const [cursor, setCursor] = useState<number | null>(null)
  const H = 92, top = 6, bottom = 18, left = 26, right = 4
  const n = points.length
  const x = (i: number) => left + (n <= 1 ? 0 : (i / (n - 1)) * (width - left - right))
  const y = (v: number) => top + (1 - v / 100) * (H - top - bottom)
  const path = useMemo(() => {
    if (!n) return { line: "", area: "" }
    const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.sun_pct).toFixed(1)}`).join("")
    return { line, area: `${line}L${x(n - 1)},${y(0)}L${x(0)},${y(0)}Z` }
  }, [points, width]) // eslint-disable-line react-hooks/exhaustive-deps
  const night = points.map((p, i) => (p.sun_altitude_deg <= 0 ? i : -1)).filter((i) => i >= 0)
  const markAt = mark ? points.findIndex((p) => p.time.slice(11, 16) >= mark) : -1
  const read = (i: number) => {
    const p = points[i]
    return p.sun_altitude_deg <= 0
      ? `${p.time.slice(11, 16)}, the sun is down`
      : `${p.time.slice(11, 16)}, ${Math.round(p.sun_pct)}% in sun, the sun ${Math.round(p.sun_altitude_deg)}° up`
  }
  const onPointer = (e: PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const f = (e.clientX - r.left - left) / Math.max(1, width - left - right)
    setCursor(Math.max(0, Math.min(n - 1, Math.round(f * (n - 1)))))
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const here = cursor ?? (markAt >= 0 ? markAt : Math.floor(n / 2))
    const next = { ArrowLeft: here - (e.shiftKey ? 4 : 1), ArrowRight: here + (e.shiftKey ? 4 : 1), Home: 0, End: n - 1 }[e.key]
    if (next === undefined) { if (e.key === "Escape" && cursor !== null) { e.stopPropagation(); setCursor(null) } return }
    e.preventDefault()
    setCursor(Math.max(0, Math.min(n - 1, next)))
  }
  if (!n) return null
  const hours = [0, 6, 12, 18, 24]
  const peak = Math.max(...points.map((p) => p.sun_pct))
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2 text-[12px] text-muted-foreground">
        <span className="font-condensed">{title}</span>
        <span className="font-readout text-foreground" aria-live="polite">{cursor !== null ? read(cursor) : ""}</span>
      </div>
      <div
        ref={box}
        tabIndex={0}
        role="group"
        aria-label={`${title}. At most ${Math.round(peak)}% in sun. Use the arrow keys to read a moment.`}
        onKeyDown={onKey}
        onBlur={() => setCursor(null)}
        className="rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <svg width={width} height={H} className="block touch-none" aria-hidden="true"
          onPointerMove={onPointer} onPointerDown={onPointer} onPointerLeave={() => setCursor(null)}>
          {night.map((i) => (
            <rect key={i} x={x(i) - (width - left - right) / (2 * (n - 1))} y={top} width={(width - left - right) / (n - 1) + 0.5}
              height={H - top - bottom} style={{ fill: "var(--muted)" }} />
          ))}
          {[0, 50, 100].map((v) => (
            <g key={v}>
              <line x1={left} x2={width - right} y1={y(v)} y2={y(v)} style={{ stroke: "var(--line)" }} strokeWidth={1} />
              <text x={left - 4} y={y(v) + 3.5} textAnchor="end" className="fill-muted-foreground font-readout text-[10px]">{v}%</text>
            </g>
          ))}
          <path d={path.area} style={{ fill: SHADE.sun }} fillOpacity={0.22} />
          <path d={path.line} fill="none" style={{ stroke: SHADE.sun }} strokeWidth={2} strokeLinejoin="round" />
          {markAt >= 0 && (
            <line x1={x(markAt)} x2={x(markAt)} y1={top - 2} y2={H - bottom} style={{ stroke: "var(--sodium)" }} strokeDasharray="3 2" strokeWidth={1.2} />
          )}
          {cursor !== null && (
            <>
              <line x1={x(cursor)} x2={x(cursor)} y1={top - 2} y2={H - bottom} style={{ stroke: "var(--foreground)" }} strokeOpacity={0.7} strokeWidth={1} />
              <circle cx={x(cursor)} cy={y(points[cursor].sun_pct)} r={4} style={{ fill: SHADE.sun, stroke: "var(--console)" }} strokeWidth={2} />
            </>
          )}
          {hours.map((h) => {
            const i = Math.min(n - 1, Math.round((h / 24) * (n - 1)))
            return <text key={h} x={x(i)} y={H - 4} textAnchor={h === 0 ? "start" : h === 24 ? "end" : "middle"} className="fill-muted-foreground font-readout text-[10px]">{String(h % 24).padStart(2, "0")}:00</text>
          })}
        </svg>
      </div>
    </div>
  )
}

// A spot's day: 96 quarter hours of sun (amber), shade (blue) or night (muted), with the clock's
// moment marked. Arrow keys read them.
export function DayStrip({ day, now, label }: { day: { time: string; altitude_deg: number; sun: boolean }[]; now: string | null; label: string }) {
  const [cursor, setCursor] = useState<number | null>(null)
  const n = day.length
  const nowAt = now ? day.findIndex((d) => d.time >= now) : -1
  const text = (i: number) => {
    const d = day[i]
    return `${d.time}, ${d.altitude_deg <= 0 ? "night" : d.sun ? "in the sun" : "in shade"}`
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const here = cursor ?? (nowAt >= 0 ? nowAt : 48)
    const next = { ArrowLeft: here - (e.shiftKey ? 4 : 1), ArrowRight: here + (e.shiftKey ? 4 : 1), Home: 0, End: n - 1 }[e.key]
    if (next === undefined) { if (e.key === "Escape" && cursor !== null) { e.stopPropagation(); setCursor(null) } return }
    e.preventDefault()
    setCursor(Math.max(0, Math.min(n - 1, next)))
  }
  const sunQuarters = day.filter((d) => d.sun && d.altitude_deg > 0).length
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2 text-[12px] text-muted-foreground">
        <span className="font-condensed">{label}</span>
        <span className="font-readout text-foreground" aria-live="polite">{cursor !== null ? text(cursor) : ""}</span>
      </div>
      <div
        tabIndex={0}
        role="group"
        aria-label={`${label}: ${sunQuarters / 4} hours in direct sun. Use the arrow keys to read a quarter hour.`}
        onKeyDown={onKey}
        onBlur={() => setCursor(null)}
        className="relative rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <div className="flex h-5 gap-px overflow-hidden rounded-sm" aria-hidden="true"
          onPointerLeave={() => setCursor(null)}>
          {day.map((d, i) => (
            <span key={i} className={cn("flex-1", cursor === i && "outline outline-1 outline-foreground")}
              onPointerEnter={() => setCursor(i)}
              style={{ background: d.altitude_deg <= 0 ? "var(--muted)" : d.sun ? SHADE.sun : SHADE.shade, opacity: d.altitude_deg <= 0 ? 1 : 0.9 }} />
          ))}
        </div>
        {nowAt >= 0 && (
          <span className="pointer-events-none absolute -top-1 -bottom-1 w-0.5 rounded-full bg-sodium" style={{ left: `${(100 * (nowAt + 0.5)) / n}%` }} aria-hidden="true" />
        )}
        <div className="mt-1 flex justify-between font-readout text-[10.5px] text-muted-foreground" aria-hidden="true">
          <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span>
        </div>
      </div>
    </div>
  )
}
