/* The island's grid on the page: a section on the Outside card (demand now, the share from wind
   and sun, the carbon, today's curve by source against the usual day) and a section in the
   inspector for grid-cyprus (what meets demand now). Both follow the city clock. */
import { ZapIcon } from "lucide-react"
import { useId } from "react"

import { FromSources } from "@/components/sources/from-sources"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ago, useClock } from "@/lib/clock"
import type { Entity } from "@/lib/entities"
import { inspect } from "@/lib/inspector"
import {
  COLORS, GRID_ID, NAMES, STACK, hm, useGridAt, type GridPoint, type GridSnapshot,
} from "@/lib/power"
import { fmtCount } from "@/lib/readouts"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { registerTopicSection } from "@/lib/registries/topics"
import { cn } from "@/lib/utils"

const W = 288
const SOURCES = ["tso_grid", "oil_co2"]

// "HH:MM" as minutes of the day.
function minutes(hhmm: string) {
  return Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5))
}

// Today from midnight, or in a day's first hours the last 24 hours.
export function spanWords(snap: GridSnapshot) {
  return snap.day_span === "today" ? (snap.live ? "today" : "that day until then") : "the last 24 hours"
}

export function hasChart(snap: GridSnapshot) {
  return snap.day.filter((p) => p.demand_mw !== null).length >= 2
}

// Carbon in grams of CO2 per kWh, the usual unit for a grid's carbon.
function fmtG(kg: number | null | undefined) {
  return kg === null || kg === undefined ? "?" : String(Math.round(kg * 1000))
}

// Today's curve: what met demand, stacked (oil-fired, wind, solar), the usual day dashed over it.
export function DayChart({ snap, height = 64 }: { snap: GridSnapshot; height?: number }) {
  const pts = snap.day.filter((p) => p.demand_mw !== null)
  if (pts.length < 2) return null
  // Across the day from its start (midnight, or 24 hours ago); the usual day wraps to match.
  const from = Date.parse(snap.day_from)
  const fromMin = minutes(snap.day_from.slice(11, 16))
  const x = (t: string) => ((Date.parse(t) - from) / 864e5) * W
  const xu = (hhmm: string) => (((minutes(hhmm) - fromMin + 1440) % 1440) / 1440) * W
  const usual = [...snap.usual].sort((a, b) => xu(a.t) - xu(b.t))
  const top = Math.max(...pts.map((p) => p.demand_mw ?? 0), ...usual.map((u) => u.demand_mw), 1) * 1.08
  const y = (v: number) => height - (v / top) * height
  const layers = STACK.map((key, i) => {
    const lower = (p: GridPoint) => STACK.slice(0, i).reduce((a, k) => a + (p[k] ?? 0), 0)
    const upper = (p: GridPoint) => lower(p) + (p[key] ?? 0)
    const line = pts.map((p) => `${x(p.t).toFixed(1)},${y(upper(p)).toFixed(1)}`)
    const back = [...pts].reverse().map((p) => `${x(p.t).toFixed(1)},${y(lower(p)).toFixed(1)}`)
    return <polygon key={key} points={[...line, ...back].join(" ")} fill={COLORS[key]} fillOpacity={0.85} />
  })
  const usualLine = usual.map((u) => `${xu(u.t).toFixed(1)},${y(u.demand_mw).toFixed(1)}`).join(" ")
  const last = pts[pts.length - 1]
  const first = pts[0]
  const peak = snap.peak
  const label = `Over ${spanWords(snap)}, the island's demand went from ${fmtCount(first.demand_mw ?? 0)} MW at ${hm(first.t)}`
    + (peak ? ` to a peak of ${fmtCount(peak.demand_mw)} MW at ${hm(peak.t)}` : "")
    + `, ${fmtCount(last.demand_mw ?? 0)} MW at ${hm(last.t)}, mostly from the oil-fired stations.`
    + (usual.length ? ` The usual day, dashed, rests on ${snap.usual_days} days.` : "")
  return (
    <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" role="img" aria-label={label}
      className="block w-full overflow-visible" style={{ height }}>
      {layers}
      {usual.length > 1 && (
        <polyline points={usualLine} fill="none" stroke="var(--color-muted-foreground, #8798b0)" strokeWidth={1.25}
          strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      )}
      <line x1={x(last.t)} x2={x(last.t)} y1={0} y2={height} stroke="#ffae45" strokeWidth={1.25} vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

function Legend({ usual }: { usual: boolean }) {
  return (
    <ul aria-label="The curve's colours" className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] text-muted-foreground">
      {STACK.map((k) => (
        <li key={k} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm" style={{ background: COLORS[k] }} aria-hidden="true" />
          {NAMES[k]}
        </li>
      ))}
      {usual && (
        <li className="flex items-center gap-1.5">
          <span className="w-3 border-t border-dashed border-muted-foreground" aria-hidden="true" />
          Usual day
        </li>
      )}
    </ul>
  )
}

function Readout({ label, value, unit, hint }: { label: string; value: string; unit?: string; hint?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="font-condensed text-[12px] text-muted-foreground">{label}</dt>
      <dd className="text-[13px] leading-snug">
        <span className="font-readout text-[17px] font-semibold tabular-nums">{value}</span>
        {unit && <span className="text-muted-foreground"> {unit}</span>}
        {hint && <span className="block text-[11.5px] text-muted-foreground">{hint}</span>}
      </dd>
    </div>
  )
}

function OutsidePower() {
  const load = useGridAt()
  const past = useClock((s) => s.at !== null)
  const clockNow = useClock((s) => s.now)
  const titleId = useId()
  const snap = load?.snap ?? null
  const n = snap?.now ?? null
  let body
  if (!snap) {
    body = load?.status === "error"
      ? <p className="text-[13px] text-signal">The grid's figures can't be read just now. Try again in a few minutes.</p>
      : <div className="flex flex-col gap-2" aria-label="Loading"><Skeleton className="h-5 w-3/4" /><Skeleton className="h-12 w-full" /></div>
  } else if (snap.status === "unavailable") {
    body = <p className="text-[13px] text-signal">The grid operator's page isn't answering. Try again in a few minutes.</p>
  } else if (snap.status === "unknown" || !n) {
    body = <p className="text-[13px] text-muted-foreground">The twin's memory holds no record of the grid for this moment.</p>
  } else {
    const asOf = Date.parse(n.t)
    body = (
      <>
        <dl className="grid grid-cols-3 gap-x-3" aria-live="polite">
          <Readout label="Demand" value={fmtCount(n.demand_mw ?? 0)} unit="MW" />
          <Readout label="Wind and sun" value={`${Math.round(n.renewable_pct ?? 0)}%`}
            hint={`${fmtCount(n.wind_mw ?? 0)} and ${fmtCount(n.solar_mw ?? 0)} MW`} />
          <Readout label="Carbon, estimated" value={fmtG(n.carbon_kg_kwh)} unit="g CO2/kWh" />
        </dl>
        {hasChart(snap) && (
          <>
            <DayChart snap={snap} />
            <Legend usual={snap.usual.length > 1} />
          </>
        )}
        <p className={cn("text-[12.5px] leading-snug text-muted-foreground", snap.status === "stale" && "text-sodium")}>
          {snap.status === "stale"
            ? `As of ${hm(n.t)}: the operator's page hasn't updated since.`
            : past ? `Recorded at ${hm(n.t)}.` : `Updated ${ago(asOf, clockNow)}, every 15 minutes.`}
          {" "}The whole island's grid, not Nicosia alone.
          {snap.peak ? ` Peak ${snap.day_span !== "today" ? "over the last 24 hours" : past ? "that day until then" : "so far today"}: ${fmtCount(snap.peak.demand_mw)} MW at ${hm(snap.peak.t)}.` : ""}
        </p>
      </>
    )
  }
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-2.5 border-t border-line pt-3">
      <h3 id={titleId} className="flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
        <ZapIcon className="size-3.5 text-sodium" aria-hidden="true" />
        The island's power {past ? "then" : "now"}
      </h3>
      {body}
      {n && (
        <Button variant="outline" size="sm" className="self-start" onClick={() => inspect({ id: GRID_ID }, { focus: true })}>
          Open the grid
        </Button>
      )}
      <FromSources ids={SOURCES} />
    </section>
  )
}

registerTopicSection({ id: "power", topic: "outside", order: 20, Section: OutsidePower })

// -- the inspector: what meets demand now --------------------------------------------------------

function GridMix({ entity }: SectionProps) {
  const snap = (entity as Entity & { grid?: GridSnapshot }).grid
  const n = snap?.now
  const titleId = useId()
  if (!snap || !n || !n.demand_mw) return null
  const total = STACK.reduce((a, k) => a + (n[k] ?? 0), 0) || n.demand_mw
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-2.5">
      <h3 id={titleId} className="text-[12.5px] text-muted-foreground">What meets demand at {hm(n.t)}</h3>
      <div className="flex h-3 w-full overflow-hidden rounded-full" role="img"
        aria-label={STACK.map((k) => `${NAMES[k]} ${fmtCount(n[k] ?? 0)} MW`).join(", ")}>
        {STACK.map((k) => <span key={k} style={{ width: `${(100 * (n[k] ?? 0)) / total}%`, background: COLORS[k] }} />)}
      </div>
      <dl className="grid grid-cols-3 gap-x-3">
        {STACK.map((k) => (
          <Readout key={k} label={NAMES[k]} value={fmtCount(n[k] ?? 0)} unit="MW" hint={`${Math.round((100 * (n[k] ?? 0)) / total)}%`} />
        ))}
      </dl>
      {hasChart(snap) && (
        <>
          <DayChart snap={snap} height={80} />
          <Legend usual={snap.usual.length > 1} />
        </>
      )}
      {snap.cleanest && (
        <p className="text-[12.5px] text-muted-foreground">
          Cleanest quarter hour over {spanWords(snap)}: {hm(snap.cleanest.t)}, {fmtG(snap.cleanest.carbon_kg_kwh)} g CO2 per kWh.
        </p>
      )}
    </section>
  )
}

registerInspectorSection({ id: "grid-mix", order: 25, kinds: ["power_grid"], Component: GridMix })
