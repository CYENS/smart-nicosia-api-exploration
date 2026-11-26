/* The inspector's sections for the mobility kinds: a bus's next stops, a stop's departure
   board, a line's buses and a road's speed against its free flow. Buses and stops refresh
   themselves while open, so the board and the delay stay true. */
import { ChevronRightIcon, ClockIcon, RadioTowerIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { Sparkline } from "@/components/inspector/sparkline"
import { LinePill, SpeedMeter } from "@/components/mobility/parts"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Item, ItemContent, ItemGroup, ItemTitle } from "@/components/ui/item"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchSeries, type Entity, type Series } from "@/lib/entities"
import { inspect, useInspector } from "@/lib/inspector"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

type Departure = {
  line: string; line_id: string; color: string; text_color: string; headsign: string; stop: string; stop_id: string
  scheduled: string; expected: string | null; realtime: boolean; delay_s: number | null; in_min: number; bus?: string | null
}
type NextStop = { id: string; name: string; scheduled: string; expected?: string; lon: number; lat: number }
type Live = {
  line_id?: string; line: string; color?: string; text_color?: string; headsign?: string; waiting?: string
  progress?: { stop: number; stops: number }; delay_source?: string
}
type Road = { speed_kmh: number | null; free_flow_kmh: number | null; free_flow: string; usual_kmh: number | null; share: number | null }
type OnRoad = { id: string; name: string; detail: string; lon: number; lat: number }
type MobilityEntity = Entity & {
  departures?: Departure[]; next_stops?: NextStop[]; live?: Live; road?: Road; feed_status?: string; running?: OnRoad[]
}

function Section({ title, children, live }: { title: string; children: React.ReactNode; live?: boolean }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2.5" aria-live={live ? "polite" : undefined}>
      <h3 className="text-[12.5px] text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

// While a bus or a stop is open, its record is read again every `ms`, so the delay, the next
// stops and the board move on. A fetch that fails leaves the last one.
function useRefresh(entity: Entity, ms: number) {
  useEffect(() => {
    const t = window.setInterval(async () => {
      if (document.hidden) return
      try {
        const res = await fetch(`/api/entities/${encodeURIComponent(entity.id)}?series=false`)
        if (!res.ok) return
        const fresh = (await res.json()) as Entity
        const st = useInspector.getState()
        if (st.open && st.entity?.id === entity.id) useInspector.setState({ entity: fresh })
      } catch { /* keep the last one */ }
    }, ms)
    return () => window.clearInterval(t)
  }, [entity.id, ms])
}

// -- a bus ------------------------------------------------------------------------------------

function BusSection({ entity }: SectionProps) {
  const e = entity as MobilityEntity
  useRefresh(entity, 20_000)
  const live = e.live
  const stops = e.next_stops ?? []
  return (
    <div className="flex flex-col gap-5">
      {live && (
        <div className="flex items-center gap-2.5">
          <LinePill line={live.line} color={live.color} textColor={live.text_color} className="h-7 min-w-10 text-[16px]" />
          <p className="min-w-0 text-[14px] leading-snug">
            <span className="text-muted-foreground">to </span>
            <span className="font-medium">{live.headsign ?? "its last stop"}</span>
            {live.progress && <span className="block text-[12.5px] text-muted-foreground">Stop {Math.min(live.progress.stop + 1, live.progress.stops)} of {live.progress.stops}</span>}
          </p>
        </div>
      )}
      <Section title={live?.waiting ? `Waiting to leave at ${live.waiting}` : "Next stops"} live>
        {stops.length ? (
          <ol className="relative flex flex-col">
            {stops.map((s, i) => {
              const late = s.expected && s.expected !== s.scheduled
              return (
                <li key={`${s.id}:${i}`} className="relative">
                  <span className={cn("absolute top-0 left-[7px] w-[2px] bg-line", i === 0 ? "top-3" : "", i === stops.length - 1 ? "h-3" : "bottom-0")} aria-hidden="true" />
                  <button
                    type="button"
                    onClick={() => inspect({ id: s.id, at: [s.lon, s.lat] }, { fly: true, focus: true })}
                    className="group relative flex w-full items-center gap-3 rounded-md py-1.5 pr-1 pl-0 text-left outline-none hover:bg-accent/50 focus-visible:ring-3 focus-visible:ring-ring/50"
                  >
                    <span
                      className={cn("relative z-[1] size-4 shrink-0 rounded-full border-2 border-console", i === 0 ? "bg-sodium" : "bg-muted-foreground")}
                      style={i === 0 && live?.color ? { background: live.color } : undefined}
                      aria-hidden="true"
                    />
                    <span className="min-w-0 flex-1 truncate text-[13.5px]">{s.name}</span>
                    <span className="shrink-0 text-right font-readout text-[14px] tabular-nums">
                      {late ? <><span className="text-sodium">{s.expected}</span> <s className="text-[12px] text-muted-foreground">{s.scheduled}</s></> : s.scheduled}
                    </span>
                    <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true" />
                  </button>
                </li>
              )
            })}
          </ol>
        ) : (
          <p className="text-[13px] text-muted-foreground">Its trip isn't in the timetable, so its stops aren't known. Its line and position are live.</p>
        )}
        {stops.length > 0 && (
          <p className="text-[12px] text-muted-foreground">
            {live?.delay_source === "timetable" ? "Times shift by its delay, estimated from where it is against the timetable." :
              live?.delay_source === "feed" ? "Times shift by the operator's live delay." : "Timetable times."}
          </p>
        )}
      </Section>
    </div>
  )
}

// -- a stop -------------------------------------------------------------------------------------

function StopSection({ entity }: SectionProps) {
  const e = entity as MobilityEntity
  useRefresh(entity, 30_000)
  const deps = e.departures ?? []
  return (
    <Section title="Next departures" live>
      {deps.length ? (
        <ul className="-mx-1 flex flex-col">
          {deps.map((d, i) => (
            <li key={`${d.line_id}:${d.scheduled}:${i}`}>
              <button
                type="button"
                onClick={() => (d.bus ? inspect({ id: d.bus }, { fly: true, focus: true }) : inspect({ id: d.line_id }, { focus: true }))}
                className="flex w-full items-center gap-2.5 rounded-md px-1 py-1.5 text-left outline-none hover:bg-accent/50 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <LinePill line={d.line} color={d.color} textColor={d.text_color} />
                <span className="min-w-0 flex-1 truncate text-[13.5px]">{d.headsign}</span>
                <span className="flex shrink-0 flex-col items-end leading-tight">
                  <span className={cn("font-readout text-[15px] font-semibold tabular-nums", d.realtime && "text-sodium")}>
                    {d.in_min === 0 ? "Now" : `${d.in_min} min`}
                  </span>
                  <span className="font-condensed text-[11.5px] text-muted-foreground tabular-nums">
                    {d.realtime ? <>{d.expected}{d.expected !== d.scheduled && <s className="ml-1">{d.scheduled}</s>}</> : `${d.scheduled}, timetable`}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <Empty className="border border-dashed border-line py-5">
          <EmptyHeader>
            <EmptyMedia variant="icon"><ClockIcon /></EmptyMedia>
            <EmptyTitle className="text-[14px]">No buses in the next 90 minutes</EmptyTitle>
            <EmptyDescription>Ask the assistant for a bus from a stop nearby, or plan the trip by bus.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
      <p className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
        <RadioTowerIcon className="size-3.5" aria-hidden="true" />
        {e.feed_status === "unavailable"
          ? "The live feed isn't answering: these are timetable times."
          : "Times in sodium are live, from the bus on the road; the others are the timetable's."}
      </p>
    </Section>
  )
}

// -- a road ---------------------------------------------------------------------------------------

const FREE_FLOW_FROM: Record<string, string> = {
  measured: "measured from 4 weeks of this road",
  estimated: "estimated from the road network until a day of readings is stored",
  assumed: "assumed at the urban limit for now",
}

function RoadSection({ entity }: SectionProps) {
  const e = entity as MobilityEntity
  const road = e.road
  const [series, setSeries] = useState<Series | null | undefined>(undefined)
  useRefresh(entity, 60_000)
  useEffect(() => {
    let alive = true
    fetchSeries(entity.id).then((s) => { if (alive) setSeries(s ?? null) }).catch(() => { if (alive) setSeries(null) })
    return () => { alive = false }
  }, [entity.id])
  if (!road) return null
  const share = road.share !== null ? Math.round(100 * road.share) : null
  return (
    <Section title="Speed against free flow">
      <SpeedMeter speed={road.speed_kmh} free={road.free_flow_kmh} usual={road.usual_kmh} state={entity.state?.value ?? "no_reading"} label={`${entity.name}: speed against free flow`} />
      <dl className="grid grid-cols-3 gap-2 text-[12.5px]">
        <div><dt className="text-muted-foreground">Now</dt><dd className="font-readout text-[15px] font-semibold">{road.speed_kmh !== null ? `${Math.round(road.speed_kmh)} km/h` : "None"}</dd></div>
        <div><dt className="flex items-center gap-1 text-muted-foreground"><span className="h-3 w-[2px] rounded-full bg-foreground" aria-hidden="true" />Free flow</dt><dd className="font-readout text-[15px] font-semibold">{road.free_flow_kmh ? `${Math.round(road.free_flow_kmh)} km/h` : "None"}</dd></div>
        <div><dt className="flex items-center gap-1 text-muted-foreground"><span className="h-3 w-[2px] rounded-full border border-muted-foreground" aria-hidden="true" />Usual now</dt><dd className="font-readout text-[15px] font-semibold">{road.usual_kmh ? `${Math.round(road.usual_kmh)} km/h` : "Not yet"}</dd></div>
      </dl>
      <p className="text-[12.5px] leading-snug text-muted-foreground">
        {share !== null ? `${share}% of its free-flow speed. ` : "No car crossed both detectors in the last 30 minutes. "}
        Free flow is {FREE_FLOW_FROM[road.free_flow] ?? "unknown"}.
      </p>
      {series === undefined && <Skeleton className="h-16 w-full" />}
      {series && series.points.length > 1 && <Sparkline series={series} title="Speed, last 24 hours" />}
      {series === null && (
        <p className="text-[12.5px] text-muted-foreground">The twin keeps this road's speeds from now on; its last 24 hours build up as it records.</p>
      )}
    </Section>
  )
}

// -- a line ---------------------------------------------------------------------------------------

function LineSection({ entity }: SectionProps) {
  const e = entity as MobilityEntity
  const running = e.running ?? []
  useRefresh(entity, 30_000)
  if (!e.live) return null
  return (
    <Section title="On the road now" live>
      <div className="flex items-center gap-2.5">
        <LinePill line={e.live.line} color={e.live.color} textColor={e.live.text_color} className="h-7 min-w-10 text-[16px]" />
        <span className="text-[13.5px] text-muted-foreground">{running.length ? `${running.length} running` : "None running now"}</span>
      </div>
      {running.length > 0 && (
        <ItemGroup className="-mx-2.5 w-auto gap-0.5">
          {running.map((r) => (
            <Item key={r.id} size="xs" role="listitem" className="cursor-pointer text-left hover:bg-accent/60"
              render={<button type="button" onClick={() => inspect({ id: r.id, at: [r.lon, r.lat] }, { fly: true, focus: true })} />}>
              <ItemContent className="min-w-0">
                <ItemTitle className="line-clamp-1 w-full">{r.name}</ItemTitle>
                <span className="text-[12.5px] text-muted-foreground">{r.detail}</span>
              </ItemContent>
            </Item>
          ))}
        </ItemGroup>
      )}
    </Section>
  )
}

registerInspectorSection({ id: "bus", order: 12, kinds: ["bus"], Component: BusSection })
registerInspectorSection({ id: "stop-departures", order: 12, kinds: ["bus_stop"], Component: StopSection })
registerInspectorSection({ id: "road-speed", order: 15, kinds: ["road_segment"], Component: RoadSection })
registerInspectorSection({ id: "line-buses", order: 12, kinds: ["bus_line"], Component: LineSection })
