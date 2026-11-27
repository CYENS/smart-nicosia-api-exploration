/* The Scenario lab's pieces: the parameter form drawn from a scenario's schema, the KPI cards
   with their ranges, the map's view and legend, the charts, the assumptions with their
   sensitivity, the backtest, the notes and the saved runs. Every number comes from a run. */
import { CheckIcon, ChevronsLeftRightIcon } from "lucide-react"
import { Slider as SliderPrimitive } from "@base-ui/react/slider"
import { useEffect, useId, useMemo, useRef, useState } from "react"

import { Row, RowList } from "@/components/mobility/parts"
import { Badge } from "@/components/ui/badge"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { drawChart } from "@/lib/charts"
import { useGeometry } from "@/lib/geometry"
import { SODIUM } from "@/lib/map/engine"
import {
  PARAM_GROUPS, type Kpi, type ParamSchema, type RunSummary, type ScenarioDef, type ScenarioRun, type SeriesRow,
} from "@/lib/registries/scenarios"
import { plainLayers, setMapView, setSwipe, useLabMap, variantLayers } from "@/lib/scenarios/map"
import { fmtNum, fmtSigned, fmtWhen, openRun, setCompare, setValue, useLab } from "@/lib/scenarios/store"
import { cn } from "@/lib/utils"

/* eslint-disable @typescript-eslint/no-explicit-any */

const MUTED = "#8798b0"

// -- the form ----------------------------------------------------------------------------------

function decimals(step: number | undefined) {
  const s = String(step ?? 1)
  return s.includes(".") ? s.split(".")[1].length : 0
}

function valueText(p: ParamSchema, v: unknown) {
  if (typeof v === "number") return `${fmtNum(v, decimals(p["x-step"]))}${p["x-unit"] ? ` ${p["x-unit"]}` : ""}`
  return String(v)
}

function enumTitle(p: ParamSchema, v: string) {
  return (p as any)["x-enum-titles"]?.[v] ?? v
}

// shadcn's Slider, on Base UI's primitive so the thumb can say its value with its unit: arrows
// step, Page Up and Down take five steps, Home and End go to the ends. Sodium marks the value.
function ParamSlider({ id, label, value, min, max, step, text, onChange, onCommit }: {
  id: string; label: string; value: number; min: number; max: number; step: number
  text: (v: number) => string; onChange: (v: number) => void; onCommit: (v: number) => void
}) {
  const one = (v: number | readonly number[]) => (Array.isArray(v) ? v[0] : (v as number))
  return (
    <SliderPrimitive.Root id={id} value={value} min={min} max={max} step={step} largeStep={step * 5} thumbAlignment="edge"
      onValueChange={(v) => onChange(one(v))} onValueCommitted={(v) => onCommit(one(v))} className="w-full py-1">
      <SliderPrimitive.Control className="relative flex h-4 w-full touch-none items-center select-none">
        <SliderPrimitive.Track className="relative h-1 grow overflow-hidden rounded-full bg-muted">
          <SliderPrimitive.Indicator className="h-full bg-sodium" />
        </SliderPrimitive.Track>
        <SliderPrimitive.Thumb
          getAriaLabel={() => label}
          getAriaValueText={(_formatted, v) => text(v)}
          className="relative block size-3.5 shrink-0 rounded-full border border-sodium bg-foreground ring-sodium/45 transition-[box-shadow] select-none after:absolute after:-inset-2 hover:ring-4 has-[:focus-visible]:ring-4 active:ring-4 data-dragging:ring-4"
        />
      </SliderPrimitive.Control>
    </SliderPrimitive.Root>
  )
}

// One parameter: a switch, a slider with its value and unit, or a select. A slider commits on
// release (or a key), so an assumption can re-run the scenario once, not on every step.
export function ParamField({ name, p, value, onCommit, idPrefix }: {
  name: string; p: ParamSchema; value: unknown; onCommit?: (v: unknown) => void; idPrefix: string
}) {
  const id = `${idPrefix}-${name}`
  const [draft, setDraft] = useState<number | null>(null)
  if (p.type === "boolean") {
    return (
      <Field orientation="horizontal" className="items-start gap-3">
        <Switch id={id} checked={Boolean(value)} onCheckedChange={(on) => { setValue(name, on); onCommit?.(on) }} className="mt-0.5" />
        <div className="flex min-w-0 flex-col gap-0.5">
          <FieldLabel htmlFor={id} className="text-[13.5px] font-medium">{p.title}</FieldLabel>
          {p.description && <FieldDescription className="text-[12.5px] leading-snug">{p.description}</FieldDescription>}
        </div>
      </Field>
    )
  }
  if (p.enum) {
    return (
      <Field className="gap-1.5">
        <FieldLabel htmlFor={id} className="text-[12.5px] font-normal text-muted-foreground">{p.title}</FieldLabel>
        <Select value={String(value)} onValueChange={(v) => { setValue(name, v); onCommit?.(v) }}>
          <SelectTrigger id={id} size="sm" className="w-full"><SelectValue>{enumTitle(p, String(value))}</SelectValue></SelectTrigger>
          <SelectContent>
            {p.enum.map((o) => <SelectItem key={o} value={o}>{enumTitle(p, o)}</SelectItem>)}
          </SelectContent>
        </Select>
      </Field>
    )
  }
  const num = Number(draft ?? value)
  const step = p["x-step"] ?? (p.type === "integer" ? 1 : 0.01)
  return (
    <Field className="gap-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <FieldLabel htmlFor={id} className="text-[12.5px] font-normal text-muted-foreground">{p.title}</FieldLabel>
        <span className="font-readout text-[14px] font-semibold tabular-nums" aria-hidden="true">{valueText(p, num)}</span>
      </div>
      <ParamSlider id={id} label={p.title} value={num} min={p.minimum ?? 0} max={p.maximum ?? 100} step={step}
        text={(v) => valueText(p, v)}
        onChange={setDraft}
        onCommit={(n) => {
          setDraft(null)
          setValue(name, n)
          onCommit?.(n)
        }} />
      <div className="flex justify-between font-condensed text-[11.5px] text-muted-foreground tabular-nums" aria-hidden="true">
        <span>{valueText(p, p.minimum)}</span><span>{valueText(p, p.maximum)}</span>
      </div>
    </Field>
  )
}

// The policy part of the form: each group's switch first, its settings under it while it is on.
export function ParamForm({ def, values }: { def: ScenarioDef; values: Record<string, unknown> }) {
  const props = Object.entries(def.params.properties).filter(([, p]) => !p["x-assumption"])
  const groups: [string, [string, ParamSchema][]][] = []
  for (const [k, p] of props) {
    const g = p["x-group"] ?? "other"
    let entry = groups.find(([name]) => name === g)
    if (!entry) groups.push((entry = [g, []]))
    entry[1].push([k, p])
  }
  return (
    <div className="flex flex-col gap-4">
      {groups.map(([g, items]) => (
        <fieldset key={g} className="flex flex-col gap-3 rounded-lg border border-line bg-night/40 p-3">
          <legend className="sr-only">{PARAM_GROUPS[g] ?? g}</legend>
          {items.filter(([, p]) => !p["x-when"] || values[p["x-when"]]).map(([k, p]) => (
            <div key={k} className={cn(p["x-when"] && "pl-11")}>
              <ParamField name={k} p={p} value={values[k]} idPrefix="lab" />
            </div>
          ))}
        </fieldset>
      ))}
    </div>
  )
}

// -- KPI cards ---------------------------------------------------------------------------------

export function changeWords(k: Kpi) {
  const d = k.delta
  if (d === null) return `${fmtNum(k.scenario, k.digits)} ${k.unit}`
  if (d === 0 && k.delta_low === 0 && k.delta_high === 0) return "No change"
  const good = (d < 0) === (k.better === "lower")
  const verb = d < 0 ? (good ? "Saves" : "Cuts") : (good ? "Gains" : "Adds")
  return `${verb} ${fmtNum(d, k.digits)} ${k.unit}` // the number and its unit stay on one line
}

function rangeWords(k: Kpi) {
  if (k.delta !== null) {
    if (k.delta_low === null || k.delta_high === null || k.delta_low === k.delta_high) return null
    const [a, b] = [Math.abs(k.delta_low), Math.abs(k.delta_high)].sort((x, y) => x - y)
    return `${fmtNum(a, k.digits)} to ${fmtNum(b, k.digits)}`
  }
  if (k.low === null || k.high === null || k.low === k.high) return null
  return `${fmtNum(k.low, k.digits)} to ${fmtNum(k.high, k.digits)}`
}

// A small bar: the range across the assumptions, with the value as set.
function RangeBar({ k }: { k: Kpi }) {
  const lo = k.delta !== null ? k.delta_low : k.low
  const hi = k.delta !== null ? k.delta_high : k.high
  const v = k.delta !== null ? k.delta : k.scenario
  if (lo === null || hi === null || v === null || lo === hi) return null
  const [a, b] = [Math.min(lo, hi), Math.max(lo, hi)]
  const span = Math.max(Math.abs(a), Math.abs(b)) || 1
  const pos = (x: number) => `${(Math.abs(x) / span) * 100}%`
  const left = Math.min(Math.abs(a), Math.abs(b))
  return (
    <span className="relative mt-1.5 block h-1.5 rounded-full bg-raised" aria-hidden="true">
      <span className="absolute inset-y-0 rounded-full bg-sodium/45" style={{ left: pos(left), right: `${100 - (Math.max(Math.abs(a), Math.abs(b)) / span) * 100}%` }} />
      <span className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-night bg-sodium" style={{ left: pos(v) }} />
    </span>
  )
}

function KpiCard({ k, other, labels }: { k: Kpi; other?: Kpi | null; labels?: [string, string] }) {
  const good = k.delta !== null && k.delta !== 0 && (k.delta < 0) === (k.better === "lower")
  const bad = k.delta !== null && k.delta !== 0 && !good
  const range = rangeWords(k)
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border border-line bg-night/40 p-3" role="group" aria-label={k.label}>
      <span className="text-[12px] leading-tight text-muted-foreground">{k.label}</span>
      <span className={cn("font-readout text-[17px] leading-tight font-semibold tabular-nums", good && "text-[#86efac]", bad && "text-signal")}>
        {changeWords(k)}
        {k.delta_pct !== null && k.delta !== 0 && (
          <span className="ml-1 text-[12.5px] font-normal whitespace-nowrap text-muted-foreground">
            {fmtNum(k.delta_pct, 1)}% {k.delta_pct < 0 ? "less" : "more"}
          </span>
        )}
      </span>
      {range && <span className="font-condensed text-[12px] text-muted-foreground tabular-nums">{range} across the assumptions</span>}
      <RangeBar k={k} />
      <span className="mt-1 font-condensed text-[12px] text-muted-foreground tabular-nums">
        {k.baseline !== null && <>Today {fmtNum(k.baseline, k.digits)}, </>}
        {labels ? `${labels[0]} ` : "scenario "}{fmtNum(k.scenario, k.digits)} {k.unit}
      </span>
      {other && labels && (
        <span className="font-condensed text-[12px] tabular-nums">
          <span className="text-muted-foreground">{labels[1]} </span>{fmtNum(other.scenario, other.digits)} {other.unit}
          {other.delta !== null && <span className="text-muted-foreground">, {changeWords(other).replace(/^./, (c) => c.toLowerCase())}</span>}
        </span>
      )}
      {k.note && <span className="mt-0.5 text-[11.5px] leading-snug text-muted-foreground">{k.note}</span>}
    </div>
  )
}

export function KpiCards({ run, compare }: { run: ScenarioRun; compare: ScenarioRun | null }) {
  const labels: [string, string] | undefined = compare ? ["A", "B"] : undefined
  return (
    <div className="grid grid-cols-1 gap-2 min-[380px]:grid-cols-2" aria-live="polite">
      {run.result.kpis.map((k) => (
        <KpiCard key={k.id} k={k} labels={labels} other={compare?.result.kpis.find((x) => x.id === k.id) ?? null} />
      ))}
    </div>
  )
}

// -- the map's view and legend ----------------------------------------------------------------

function legendOf(run: ScenarioRun, names: string[]) {
  const out: [string, string][] = []
  for (const n of names) {
    for (const [state, color] of Object.entries(run.result.layers[n]?.meta?.legend ?? {})) {
      if (!out.some(([s]) => s === state)) out.push([state, color])
    }
  }
  return out
}

export function MapViewControls({ run, compare }: { run: ScenarioRun; compare: ScenarioRun | null }) {
  const view = useLabMap((s) => s.view)
  const overlays = useLabMap((s) => s.overlays)
  const { overlays: ov, views } = plainLayers(run)
  const split = variantLayers(run, "baseline").length + variantLayers(run, "scenario").length > 0
  const shown = view === "split"
    ? [...variantLayers(run, "baseline"), ...variantLayers(run, "scenario")].map(([n]) => n)
    : [view]
  const legend = legendOf(run, [...shown, ...ov.filter(([n]) => overlays[n] ?? true).map(([n]) => n)])
  return (
    <div className="flex flex-col gap-2.5">
      {(split || views.length > 0) && (
        <ToggleGroup
          value={[view]}
          onValueChange={(v) => { if (v[0]) setMapView(v[0]) }}
          aria-label="What the map shows"
          variant="outline"
          size="sm"
          className="flex w-full flex-wrap"
        >
          {split && <ToggleGroupItem value="split" className="h-auto min-h-8 flex-1 py-1 whitespace-normal data-pressed:border-sodium/60 data-pressed:bg-sodium/12 data-pressed:text-sodium">{compare ? "A and B, split" : "Today and scenario"}</ToggleGroupItem>}
          {views.map(([n, l]) => (
            <ToggleGroupItem key={n} value={n} className="h-auto min-h-8 flex-1 py-1 whitespace-normal data-pressed:border-sodium/60 data-pressed:bg-sodium/12 data-pressed:text-sodium">{l.meta.title}</ToggleGroupItem>
          ))}
        </ToggleGroup>
      )}
      {ov.map(([n, l]) => (
        <Field key={n} orientation="horizontal" className="items-center gap-3">
          <Switch id={`lab-ov-${n}`} checked={overlays[n] ?? true} onCheckedChange={(on) => useLabMap.setState((s) => ({ overlays: { ...s.overlays, [n]: on } }))} />
          <FieldLabel htmlFor={`lab-ov-${n}`} className="text-[13px] font-normal">{l.meta.title}</FieldLabel>
        </Field>
      ))}
      {legend.length > 0 && (
        <ul className="flex flex-wrap gap-x-3 gap-y-1" aria-label="Legend">
          {legend.map(([state, color]) => (
            <li key={state} className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <span className="size-2.5 rounded-full" style={{ background: color }} aria-hidden="true" />{state}
            </li>
          ))}
        </ul>
      )}
      {view === "split" && split && (
        <p className="text-[12px] leading-snug text-muted-foreground">
          {compare ? "Left of the swipe, run A; right of it, run B." : "Left of the swipe, today as measured; right of it, the scenario."} Click a lamp to open it.
        </p>
      )}
    </div>
  )
}

// The swipe across the open map: a Base UI slider whose handle alone takes the pointer, so the
// map still pans everywhere else. Arrow keys, Page Up and Down, Home and End move it.
export function ScenarioSwipe() {
  const g = useGeometry()
  const run = useLab((s) => s.run)
  const compare = useLab((s) => s.compare)
  const view = useLabMap((s) => s.view)
  const swipe = useLabMap((s) => s.swipe)
  if (!run || view !== "split" || !variantLayers(run, "scenario").length || g.inspecting) return null
  const left = g.insets.left
  const width = Math.max(1, g.vw - left - g.insets.right)
  const pct = Math.round(swipe * 100)
  const leftLabel = compare ? `A: ${run.label}` : "Today"
  const rightLabel = compare ? `B: ${compare.label}` : run.label
  return (
    <div className="pointer-events-none absolute z-[5]" style={{ left, width, top: g.insets.top, bottom: g.insets.bottom + 8 }}>
      <div className="absolute inset-y-0" style={{ left: `${swipe * 100}%` }}>
        <div className="absolute inset-y-0 -left-px w-0.5 bg-foreground/85 shadow-[0_0_0_1px_rgb(14_24_40_/_0.6)]" />
        <span className="panel-float absolute top-2 max-w-48 truncate rounded-md px-2 py-1 font-readout text-[12.5px] font-semibold" style={{ right: 10 }}>{leftLabel}</span>
        <span className="panel-float absolute top-2 max-w-48 truncate rounded-md px-2 py-1 font-readout text-[12.5px] font-semibold text-sodium" style={{ left: 10 }}>{rightLabel}</span>
      </div>
      <SliderPrimitive.Root value={pct} min={4} max={96} step={1} largeStep={10} thumbAlignment="center"
        onValueChange={(v) => setSwipe((Array.isArray(v) ? v[0] : v) / 100)} className="pointer-events-none absolute inset-0">
        <SliderPrimitive.Control className="pointer-events-none relative size-full touch-none select-none">
          <SliderPrimitive.Track className="pointer-events-none relative size-full">
            <SliderPrimitive.Thumb
              getAriaLabel={() => "Scenario swipe"}
              getAriaValueText={() => `Left ${pct}% of the map shows ${leftLabel}; the rest shows ${rightLabel}`}
              className="pointer-events-auto flex size-10 cursor-ew-resize items-center justify-center rounded-full border-2 border-foreground/85 bg-console text-foreground shadow-lg outline-none transition-[border-color,box-shadow,color] hover:border-sodium hover:text-sodium has-[:focus-visible]:border-sodium has-[:focus-visible]:text-sodium has-[:focus-visible]:shadow-[0_0_0_4px_rgb(255_174_69_/_0.45)] active:border-sodium data-dragging:border-sodium"
              style={{ top: "50%" }}
            >
              <ChevronsLeftRightIcon className="size-5" aria-hidden="true" />
            </SliderPrimitive.Thumb>
          </SliderPrimitive.Track>
        </SliderPrimitive.Control>
      </SliderPrimitive.Root>
    </div>
  )
}

// -- charts --------------------------------------------------------------------------------------

const VARIANT_COLORS: Record<string, string> = { baseline: MUTED, measured: MUTED, scenario: SODIUM, modelled: SODIUM }
const VARIANT_LABELS: Record<string, string> = { baseline: "Today", scenario: "Scenario", measured: "Measured", modelled: "Modelled" }

function chartSpec(rows: SeriesRow[], def: { x: string; kind?: string; unit: string }, variants: string[], colors: string[]) {
  const x = def.x === "time"
    ? { field: "time", type: "temporal", title: null, axis: { format: "%H:%M", labelOverlap: true } }
    : def.x === "date"
      ? { field: "time", type: "temporal", title: null, axis: { format: "%d %b", labelOverlap: true } }
      : { field: "time", type: "ordinal", title: null, axis: { labelAngle: 0, labelExpr: "slice(datum.label, 5)" } }
  const color = { field: "label", type: "nominal", title: null, scale: { domain: variants, range: colors }, legend: { orient: "top", direction: "horizontal" } }
  const y = { field: "value", type: "quantitative", title: def.unit, axis: { tickCount: 4 } }
  const tooltip = [{ field: "label", title: "Series" }, { field: "time", title: "When", ...(def.x === "month" ? {} : { type: "temporal", format: def.x === "time" ? "%H:%M" : "%d %b" }) },
    { field: "value", title: def.unit, format: ",.2f" }]
  const data = { values: rows }
  if (def.kind === "bar") {
    return { width: "container", data,
      mark: { type: "bar" }, encoding: { x, xOffset: { field: "label", sort: variants }, y, color, tooltip } }
  }
  return { width: "container", data,
    mark: { type: "line", interpolate: def.x === "time" ? "step-after" : "linear", point: def.x === "date" ? { size: 18 } : false },
    encoding: { x, y, color, tooltip } }
}

export function Charts({ run, compare }: { run: ScenarioRun; compare: ScenarioRun | null }) {
  const charts = run.result.charts ?? []
  const [metric, setMetric] = useState(charts[0]?.metric ?? "")
  const el = useRef<HTMLDivElement>(null)
  const def = charts.find((c) => c.metric === metric) ?? charts[0]
  useEffect(() => {
    if (!def || !el.current) return
    const mine = run.result.series.filter((r) => r.metric === def.metric)
    const variants = [...new Set(mine.map((r) => r.variant))]
    const label = (v: string) => (compare && v === "scenario" ? "A" : VARIANT_LABELS[v] ?? v)
    let rows = mine.map((r) => ({ ...r, label: label(r.variant) }))
    const names = variants.map(label)
    const colors = variants.map((v) => VARIANT_COLORS[v] ?? SODIUM)
    if (compare && def.metric !== "backtest_kwh") {
      const theirs = compare.result.series.filter((r) => r.metric === def.metric && r.variant === "scenario")
      if (theirs.length) {
        rows = [...rows, ...theirs.map((r) => ({ ...r, label: "B" }))]
        names.push("B")
        colors.push("#6cb4ff")
      }
    }
    let stop: (() => void) | null = null
    let gone = false
    void drawChart(el.current, { title: def.title, spec: chartSpec(rows, def, names, colors) }, 170).then((s) => {
      if (gone) s()
      else stop = s
    })
    return () => { gone = true; stop?.() }
  }, [run, compare, def])
  if (!def) return null
  return (
    <div className="flex flex-col gap-2">
      {charts.length > 1 && (
        <ToggleGroup value={[def.metric]} onValueChange={(v) => { if (v[0]) setMetric(v[0]) }} aria-label="Which chart" variant="outline" size="sm" className="flex w-full flex-wrap">
          {charts.map((c) => (
            <ToggleGroupItem key={c.metric} value={c.metric} className="h-auto min-h-8 flex-1 py-1 whitespace-normal data-pressed:border-sodium/60 data-pressed:bg-sodium/12 data-pressed:text-sodium">
              {c.metric === "night_kw" ? "A night" : c.metric === "monthly_mwh" ? "The year" : c.metric === "backtest_kwh" ? "Backtest" : c.title}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      )}
      <p className="text-[12.5px] leading-snug text-muted-foreground">{def.title}</p>
      <div ref={el} className="min-h-[200px] w-full" role="img" aria-label={`${def.title}, in ${def.unit}`} />
    </div>
  )
}

// -- assumptions and their sensitivity --------------------------------------------------------------

// An assumption's sweep in words: "saves 1,509 to 2,975 EUR", "adds 10 to 30 lamp-hours".
function effect(a: number, b: number, unit: string, digits: number) {
  if (a === b) return "no effect"
  if (a < 0 && b < 0) return `saves ${fmtNum(a, digits)} to ${fmtNum(b, digits)} ${unit}`
  if (a >= 0 && b >= 0) return `adds ${fmtNum(a, digits)} to ${fmtNum(b, digits)} ${unit}`
  return `${fmtSigned(a, digits)} to ${fmtSigned(b, digits)} ${unit}`
}

// How far each assumption alone, across its plausible range, moves the headline change: a bar
// per assumption from its low end to its high end, the line where it stands as set.
export function Tornado({ run }: { run: ScenarioRun }) {
  const rows = run.result.sensitivity.filter((s) => s.at_low !== null && s.at_high !== null)
  if (!rows.length) return null
  const base = rows[0].base ?? 0
  const all = rows.flatMap((s) => [s.at_low!, s.at_high!, base])
  const lo = Math.min(...all), hi = Math.max(...all)
  const span = hi - lo || 1
  const x = (v: number) => ((v - lo) / span) * 100
  const kpi = run.result.kpis.find((k) => k.id === rows[0].kpi)
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[12.5px] leading-snug text-muted-foreground">
        How each assumption alone moves the change in {kpi?.label.toLowerCase() ?? rows[0].kpi}, from the low to the high end of its range.
      </p>
      <ul className="flex flex-col gap-2">
        {rows.map((s) => {
          const [a, b] = [Math.min(s.at_low!, s.at_high!), Math.max(s.at_low!, s.at_high!)]
          return (
            <li key={s.id} className="flex flex-col gap-1">
              <span className="flex items-baseline justify-between gap-2 text-[12.5px]">
                <span className="truncate">{s.label}</span>
                <span className="shrink-0 font-condensed text-muted-foreground tabular-nums">
                  {effect(s.at_low!, s.at_high!, s.unit, kpi?.digits ?? 0)}
                </span>
              </span>
              <span className="relative block h-2.5 rounded-sm bg-raised" aria-hidden="true">
                <span className="absolute inset-y-0 rounded-sm bg-sodium/70" style={{ left: `${x(a)}%`, width: `${Math.max(0.8, x(b) - x(a))}%` }} />
                <span className="absolute inset-y-[-3px] w-px bg-foreground/80" style={{ left: `${x(base)}%` }} />
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export function Assumptions({ def, run, values, onCommit }: {
  def: ScenarioDef; run: ScenarioRun; values: Record<string, unknown>; onCommit: () => void
}) {
  const props = Object.entries(def.params.properties).filter(([, p]) => p["x-assumption"])
  return (
    <div className="flex flex-col gap-4">
      {props.map(([k, p]) => (
        <div key={k} className="flex flex-col gap-1">
          <ParamField name={k} p={p} value={values[k]} idPrefix="lab-a" onCommit={onCommit} />
          {p["x-source"] && <p className="text-[11.5px] leading-snug text-muted-foreground">{p["x-source"]}</p>}
          {run.params[k] !== values[k] && <Badge variant="outline" className="self-start text-[11px]">Not in this run yet</Badge>}
        </div>
      ))}
    </div>
  )
}

// -- the backtest -------------------------------------------------------------------------------

export function Backtest({ run }: { run: ScenarioRun }) {
  const bt = run.result.backtest
  if (!bt) return <p className="text-[13px] text-muted-foreground">This scenario has no backtest yet.</p>
  const hold = bt.holdout as { learnt_until: string; error_pct: number | null; measured: number; modelled: number } | undefined
  const ok = Math.abs(bt.error_pct ?? 99) <= 5
  return (
    <div className="flex flex-col gap-1.5 text-[13px] leading-snug" aria-live="polite">
      <p>
        <span className={cn("font-readout text-[17px] font-semibold tabular-nums", ok ? "text-[#86efac]" : "text-sodium")}>{fmtSigned(bt.error_pct, 1)}%</span>{" "}
        <span className="text-muted-foreground">error on {bt.period.toLowerCase()}:</span>
      </p>
      <p>
        The meters measured {fmtNum(bt.measured, 1)} {bt.unit}; today's policy, modelled, gives {fmtNum(bt.modelled, 1)} {bt.unit}.
        {hold && hold.error_pct !== null && <> Learnt only until {hold.learnt_until} and replayed on the rest: {fmtSigned(hold.error_pct, 1)}%.</>}
        {typeof bt.median_lamp_error_pct === "number" && <> Lamp by lamp, half are within {fmtNum(bt.median_lamp_error_pct as number, 1)}%.</>}
      </p>
      <p className="text-[12px] text-muted-foreground">{bt.note}</p>
    </div>
  )
}

export function Notes({ run }: { run: ScenarioRun }) {
  const notes = run.result.notes ?? []
  if (!notes.length) return null
  return (
    <ul className="flex list-disc flex-col gap-1.5 pl-4 text-[12.5px] leading-snug text-muted-foreground">
      {notes.map((n, i) => <li key={i}>{n}</li>)}
    </ul>
  )
}

// -- saved runs ------------------------------------------------------------------------------------

function headline(r: RunSummary) {
  const k = r.kpis.find((x) => x.delta !== null && x.delta !== 0) ?? r.kpis[0]
  return k ? changeWords(k) : ""
}

export function SavedRuns({ onOpened }: { onOpened?: () => void }) {
  const saved = useLab((s) => s.saved)
  const status = useLab((s) => s.savedStatus)
  const open = useLab((s) => s.run?.id)
  const titleId = useId()
  if (status === "loading" && !saved.length) return <p className="text-[13px] text-muted-foreground">Reading the saved runs.</p>
  if (status === "error") return <p className="text-[13px] text-muted-foreground">The saved runs can't be read just now. Try again in a moment.</p>
  if (!saved.length) return <p className="text-[13px] text-muted-foreground">No runs yet. Set a policy and run it: each run is kept here with a link.</p>
  return (
    <div className="flex flex-col gap-1.5">
      <span id={titleId} className="sr-only">Saved runs, newest first</span>
      <RowList label="Saved runs, newest first">
        {saved.map((r) => (
          <Row key={r.id} rowKey={r.id} current={r.id === open} label={`${r.label}, ${headline(r)}, ${fmtWhen(r.created)}`}
            onSelect={() => { void openRun(r.id); onOpened?.() }}>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px]">{r.label}</span>
              <span className="block truncate font-condensed text-[12px] text-muted-foreground">{headline(r)} · {fmtWhen(r.created)}</span>
            </span>
            {r.id === open && <CheckIcon className="size-4 shrink-0 text-sodium" aria-hidden="true" />}
          </Row>
        ))}
      </RowList>
    </div>
  )
}

export function CompareSelect({ run }: { run: ScenarioRun }) {
  const saved = useLab((s) => s.saved)
  const compare = useLab((s) => s.compare)
  const others = useMemo(() => saved.filter((r) => r.id !== run.id && r.scenario === run.scenario), [saved, run])
  return (
    <Field className="gap-1.5">
      <FieldLabel htmlFor="lab-compare" className="text-[12.5px] font-normal text-muted-foreground">Compare with</FieldLabel>
      <Select value={compare?.id ?? "none"} onValueChange={(v) => { void setCompare(v === "none" ? null : String(v)) }}>
        <SelectTrigger id="lab-compare" size="sm" className="w-full">
          <SelectValue>{compare ? compare.label : "No other run"}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="none">No other run</SelectItem>
          {others.map((r) => <SelectItem key={r.id} value={r.id}>{r.label} ({fmtWhen(r.created)})</SelectItem>)}
        </SelectContent>
      </Select>
    </Field>
  )
}
