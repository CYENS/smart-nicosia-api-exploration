/* The waste collection scenario's own sections in the Scenario lab, under the generic pieces:
   the rounds of a day (which bins, in which order, at what time, on which truck), the bins that
   sit full most, and the bins that would need a bigger container. A stop or a bin flies the
   lab's map to it and rings it; a trip of the day the map draws flies to its line. Everything
   here is read from the run's details (chatbot/scenarios/waste.py). */
import { useId, useMemo, useState } from "react"

import { Row, RowList } from "@/components/mobility/parts"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { registerParamGroups, registerScenarioSection, type ScenarioRun } from "@/lib/registries/scenarios"
import { flyToScenarioFeature } from "@/lib/scenarios/map"
import { fmtNum } from "@/lib/scenarios/store"

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

const STREAM: Record<string, string> = { glass: "Glass", underground: "Underground" }

function details(run: ScenarioRun): WasteDetails | null {
  const d = run.result.details as Partial<WasteDetails> | undefined
  return d?.days ? (d as WasteDetails) : null
}

function Rounds({ run }: { run: ScenarioRun; compare: ScenarioRun | null }) {
  const d = details(run)
  const ids = { side: useId(), day: useId() }
  const [side, setSide] = useState<"scenario" | "baseline">("scenario")
  const [picked, setPicked] = useState<string | null>(null)
  if (!d) return null
  const days = d.days[side]
  const fallback = side === "scenario" ? d.route_date : days[0]?.date
  const day = days.find((x) => x.date === picked) ?? days.find((x) => x.date === fallback) ?? days[0]
  const drawn = side === "scenario" && day?.date === d.route_date
  const drawnLabel = d.days.scenario.find((x) => x.date === d.route_date)?.label
  return (
    <div className="flex flex-col gap-3">
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
              <SelectValue>{day ? `${day.label}: ${day.stops} stops, ${fmtNum(day.km, 1)} km` : "No rounds"}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {days.map((x) => (
                <SelectItem key={x.date} value={x.date}>
                  <span className="flex w-full justify-between gap-3">
                    <span>{x.label}</span>
                    <span className="font-readout text-muted-foreground tabular-nums">{x.stops} stops, {fmtNum(x.km, 1)} km</span>
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
        <>
          {!drawn && drawnLabel && (
            <p className="text-[12px] text-muted-foreground">
              The map draws the rounds of {drawnLabel}, the busiest day; a stop here still flies to its bin.
            </p>
          )}
          <DayView day={day} drawn={drawn} />
        </>
      )}
    </div>
  )
}

function DayView({ day, drawn }: { day: Day; drawn: boolean }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="font-readout text-[13px] tabular-nums" aria-live="polite">
        {day.label}: {day.trucks} {day.trucks === 1 ? "truck" : "trucks"}, {day.trips.length} {day.trips.length === 1 ? "trip" : "trips"},{" "}
        {day.stops} stops, {fmtNum(day.km, 1)} km, {fmtNum(day.hours, 1)} driver hours
      </p>
      {day.trips.map((t) => (
        <div key={t.n} className="flex flex-col gap-1">
          <div className="flex items-baseline justify-between gap-2">
            <p className="min-w-0 text-[12.5px]">
              <span className="font-semibold">{STREAM[t.stream] ?? t.stream} round, truck {t.truck}, trip {t.n}</span>
              <span className="text-muted-foreground"> from {t.start} to {t.end}, {fmtNum(t.km, 1)} km</span>
            </p>
            {drawn && (
              <Button variant="ghost" size="xs" className="shrink-0"
                aria-label={`Show trip ${t.n} on the map`}
                onClick={() => flyToScenarioFeature(`trip-${day.date}-${t.n}`)}>
                On the map
              </Button>
            )}
          </div>
          <RowList label={`Stops of trip ${t.n}, in order`}>
            {t.stops.map((s, i) => (
              <Row
                key={`${s.id}-${i}`}
                rowKey={`${t.n}-${s.id}-${i}`}
                label={`Stop ${i + 1} at ${s.eta}: ${s.name}, ${s.level}% full. Show it on the map.`}
                onSelect={() => flyToScenarioFeature(s.id)}
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
  const left = useMemo(() => Object.entries(d?.summary.not_modelled ?? {}), [d])
  if (!d) return null
  return (
    <div className="flex flex-col gap-3">
      <p className="text-[12.5px] text-muted-foreground">Full the longest under today's pickups, hours a week: today to this scenario.</p>
      {d.watch.length ? (
        <RowList label="Bins full the longest today">
          {d.watch.map((w) => (
            <Row
              key={w.id}
              rowKey={w.id}
              label={`${w.name}: full ${w.full_hours_baseline} hours a week today, ${w.full_hours} in this scenario. Show it on the map.`}
              onSelect={() => flyToScenarioFeature(w.id)}
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px]">{w.name}</span>
                <span className="block truncate font-condensed text-[12px] text-muted-foreground">
                  fills {fmtNum(w.fill_per_day, 1)}% a day, emptied {fmtNum(w.collections_baseline, 1)} to {fmtNum(w.collections, 1)} times a week
                </span>
              </span>
              <span className="shrink-0 text-right font-readout text-[13px] tabular-nums">
                {w.full_hours_baseline} to <span className="font-semibold">{w.full_hours}</span>
                <span className="text-muted-foreground"> h</span>
              </span>
            </Row>
          ))}
        </RowList>
      ) : (
        <p className="text-[12.5px] text-muted-foreground">No bin sat at 80% or more under today's pickups.</p>
      )}
      <h4 className="text-[12.5px] font-semibold">
        A bigger container <span className="font-normal text-muted-foreground">would still fill between two round days</span>
      </h4>
      {d.bigger.length ? (
        <RowList label="Bins that need a bigger container">
          {d.bigger.map((b) => (
            <Row key={b.id} rowKey={b.id} label={`${b.name}: ${fmtNum(b.volume_l)} litres now, needs ${fmtNum(b.need_l)}. Show it on the map.`}
              onSelect={() => flyToScenarioFeature(b.id)}>
              <span className="min-w-0 flex-1 truncate text-[13px]">{b.name}</span>
              <span className="shrink-0 font-readout text-[13px] tabular-nums">{fmtNum(b.volume_l)} to {fmtNum(b.need_l)} l</span>
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
    </div>
  )
}

registerParamGroups({ policy: "Which bins, and when", rounds: "The rounds" })
registerScenarioSection({ scenario: "waste", id: "rounds", title: "Rounds, day by day", order: 10, Section: Rounds })
registerScenarioSection({ scenario: "waste", id: "watch", title: "Bins to watch", order: 20, Section: BinsToWatch })
