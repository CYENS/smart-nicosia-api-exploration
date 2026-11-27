/* The waste collection scenario's own sections in the Scenario lab, under the generic pieces:
   the rounds of a day (which bins, in which order, at what time, on which truck), the bins that
   sit full most, and the bins that would need a bigger container. A stop or a bin opens in the
   inspector. Everything here is read from the run's details (chatbot/scenarios/waste.py). */
import { useId, useMemo, useState } from "react"

import { Row, RowList } from "@/components/mobility/parts"
import { Field, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { inspectId } from "@/lib/inspector"
import { registerParamGroups, registerScenarioSection, type ScenarioRun } from "@/lib/registries/scenarios"

type Stop = { id: string; name: string; eta: string; level: number; load_m3: number }
type Trip = { n: number; stream: string; truck: number; start: string; end: string; km: number; stops: Stop[] }
type Day = { date: string; label: string; trucks: number; km: number; hours: number; stops: number; trips: Trip[] }
type Watch = {
  id: string; name: string; full_hours_baseline: number; full_hours: number; fill_per_day: number
  collections_baseline: number; collections: number
}
type WasteDetails = {
  summary: { bins_modelled: number; bins_in_inventory: number; not_modelled: Record<string, number>; depot: string }
  days: { scenario: Day[]; baseline: Day[] }
  route_date: string | null
  watch: Watch[]
  bigger: { id: string; name: string; volume_l: number; need_l: number }[]
}

const STREAM = { glass: "Glass", underground: "Underground" } as Record<string, string>
const fmt = (v: number, digits = 0) => v.toLocaleString("en-GB", { maximumFractionDigits: digits, minimumFractionDigits: digits })

function details(run: ScenarioRun): WasteDetails | null {
  const d = run.result.details as Partial<WasteDetails> | undefined
  return d?.days ? (d as WasteDetails) : null
}

function Rounds({ run }: { run: ScenarioRun; compare: ScenarioRun | null }) {
  const d = details(run)
  const ids = { side: useId(), day: useId(), title: useId() }
  const [side, setSide] = useState<"scenario" | "baseline">("scenario")
  const days = d?.days[side] ?? []
  const fallback = side === "scenario" ? d?.route_date : days[0]?.date
  const [picked, setPicked] = useState<string | null>(null)
  const day = days.find((x) => x.date === picked) ?? days.find((x) => x.date === fallback) ?? days[0]
  if (!d) return null
  return (
    <section aria-labelledby={ids.title} className="flex flex-col gap-3">
      <h3 id={ids.title} className="text-[13px] font-semibold">
        Rounds <span className="font-normal text-muted-foreground">which bins, in which order, when</span>
      </h3>
      <div className="grid gap-2.5 sm:grid-cols-2">
        <Field>
          <FieldLabel id={ids.side} className="text-[12.5px] font-normal text-muted-foreground">Show</FieldLabel>
          <ToggleGroup
            aria-labelledby={ids.side}
            variant="outline" size="sm" spacing={0} className="w-full"
            value={[side]}
            onValueChange={(v) => { if (v[0]) { setSide(v[0] as "scenario" | "baseline"); setPicked(null) } }}
          >
            <ToggleGroupItem value="scenario" className="flex-1">This scenario</ToggleGroupItem>
            <ToggleGroupItem value="baseline" className="flex-1">Today's pickups</ToggleGroupItem>
          </ToggleGroup>
        </Field>
        <Field>
          <FieldLabel htmlFor={ids.day} className="text-[12.5px] font-normal text-muted-foreground">Day</FieldLabel>
          <Select value={day?.date ?? ""} onValueChange={(v) => setPicked(v as string)} disabled={!days.length}>
            <SelectTrigger id={ids.day} size="sm" className="w-full">
              <SelectValue>{day ? `${day.label}: ${day.stops} stops, ${fmt(day.km, 1)} km` : "No rounds"}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {days.map((x) => (
                <SelectItem key={x.date} value={x.date}>
                  <span className="flex w-full justify-between gap-3">
                    <span>{x.label}</span>
                    <span className="font-readout text-muted-foreground tabular-nums">{x.stops} stops, {fmt(x.km, 1)} km</span>
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
      </div>
      {!day ? (
        <p className="text-[12.5px] text-muted-foreground">
          {side === "scenario"
            ? "No bin needed collecting in the period under this policy. Lower the level, or try the forecast policy."
            : "The sensors recorded no pickups in the period."}
        </p>
      ) : (
        <DayView day={day} />
      )}
    </section>
  )
}

function DayView({ day }: { day: Day }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="font-readout text-[13px] tabular-nums" aria-live="polite">
        {day.label}: {day.trucks} {day.trucks === 1 ? "truck" : "trucks"}, {day.trips.length} {day.trips.length === 1 ? "trip" : "trips"},{" "}
        {day.stops} stops, {fmt(day.km, 1)} km, {fmt(day.hours, 1)} driver hours
      </p>
      {day.trips.map((t) => (
        <div key={t.n} className="flex flex-col gap-1">
          <p className="text-[12.5px]">
            <span className="font-semibold">{STREAM[t.stream] ?? t.stream} round, truck {t.truck}, trip {t.n}</span>
            <span className="text-muted-foreground"> from {t.start} to {t.end}, {fmt(t.km, 1)} km</span>
          </p>
          <RowList label={`Stops of trip ${t.n}, in order`}>
            {t.stops.map((s, i) => (
              <Row
                key={`${s.id}-${i}`}
                rowKey={`${t.n}-${s.id}-${i}`}
                label={`Stop ${i + 1} at ${s.eta}: ${s.name}, ${s.level}% full. Open it.`}
                onSelect={() => inspectId(s.id, { fly: true, focus: true })}
              >
                <span className="w-5 shrink-0 text-right font-readout text-[12px] text-muted-foreground tabular-nums">{i + 1}</span>
                <span className="w-11 shrink-0 font-readout text-[13px] tabular-nums">{s.eta}</span>
                <span className="min-w-0 flex-1 truncate text-[13px]">{s.name}</span>
                <span className="shrink-0 font-readout text-[12.5px] text-muted-foreground tabular-nums">{s.level}%</span>
              </Row>
            ))}
          </RowList>
        </div>
      ))}
    </div>
  )
}

function BinsToWatch({ run }: { run: ScenarioRun; compare: ScenarioRun | null }) {
  const d = details(run)
  const title = useId()
  const left = useMemo(() => Object.entries(d?.summary.not_modelled ?? {}), [d])
  if (!d) return null
  return (
    <section aria-labelledby={title} className="flex flex-col gap-3">
      <h3 id={title} className="text-[13px] font-semibold">
        Bins to watch <span className="font-normal text-muted-foreground">full the longest today, hours a week</span>
      </h3>
      {d.watch.length ? (
        <RowList label="Bins full the longest today">
          {d.watch.map((w) => (
            <Row
              key={w.id}
              rowKey={w.id}
              label={`${w.name}: full ${w.full_hours_baseline} hours a week today, ${w.full_hours} in this scenario. Open it.`}
              onSelect={() => inspectId(w.id, { fly: true, focus: true })}
            >
              <span className="min-w-0 flex-1 truncate text-[13px]">{w.name}</span>
              <span className="shrink-0 font-readout text-[12.5px] text-muted-foreground tabular-nums">
                fills {fmt(w.fill_per_day, 1)}% a day
              </span>
              <span className="w-[4.5rem] shrink-0 text-right font-readout text-[13px] tabular-nums">
                {w.full_hours_baseline} to <span className="text-sodium">{w.full_hours}</span>
              </span>
            </Row>
          ))}
        </RowList>
      ) : (
        <p className="text-[12.5px] text-muted-foreground">No bin sat at 80% or more under today's pickups.</p>
      )}
      <h3 className="text-[13px] font-semibold">
        A bigger container <span className="font-normal text-muted-foreground">would still fill between two round days</span>
      </h3>
      {d.bigger.length ? (
        <RowList label="Bins that need a bigger container">
          {d.bigger.map((b) => (
            <Row key={b.id} rowKey={b.id} label={`${b.name}: ${fmt(b.volume_l)} litres now, needs ${fmt(b.need_l)}. Open it.`}
              onSelect={() => inspectId(b.id, { fly: true, focus: true })}>
              <span className="min-w-0 flex-1 truncate text-[13px]">{b.name}</span>
              <span className="shrink-0 font-readout text-[13px] tabular-nums">{fmt(b.volume_l)} to {fmt(b.need_l)} l</span>
            </Row>
          ))}
        </RowList>
      ) : (
        <p className="text-[12.5px] text-muted-foreground">None: every bin fits between two round days, even in a busy week.</p>
      )}
      <p className="text-[12px] text-muted-foreground">
        {d.summary.bins_modelled} of {d.summary.bins_in_inventory} bins modelled
        {left.length ? `; left out: ${left.map(([k, v]) => `${v} ${k.toLowerCase()}`).join("; ")}` : ""}. Depot: {d.summary.depot}.
      </p>
    </section>
  )
}

registerParamGroups({ policy: "Which bins, and when", rounds: "The rounds" })
registerScenarioSection({ scenario: "waste", id: "rounds", title: "Rounds", order: 10, Section: Rounds })
registerScenarioSection({ scenario: "waste", id: "watch", title: "Bins to watch", order: 20, Section: BinsToWatch })
