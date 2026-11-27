/* The twin's four topics: Buses, Parking, Traffic and Outside. Each puts one layer on the map
   and says, in a sentence, how that part of the city is right now (or at the clock's moment). */
import { BusFrontIcon, CarFrontIcon, GaugeIcon, SunIcon } from "lucide-react"
import { useEffect, type ReactNode } from "react"

import { LinePill, RoadLegend } from "@/components/mobility/parts"
import { Skeleton } from "@/components/ui/skeleton"
import { openLayers } from "@/lib/layers-sheet"
import { nicosiaTime } from "@/lib/map/engine"
import { moonName } from "@/lib/map/sky"
import { setBusesOn, setRoadsOn } from "@/lib/mobility/map"
import { useMobility, watchSummary } from "@/lib/mobility/store"
import { AIR_BANDS, AIR_WORDS, airWords, cap, fmtCount, parkingWord, skyWord, useSky, useStrip } from "@/lib/readouts"
import { registerTopic, selectTopic, type TopicLink } from "@/lib/registries/topics"
import { openWorkspace, useWorkspace } from "@/lib/registries/workspaces"
import { setSystemLayer, systemLayerOn, useSystemLayers } from "@/lib/system-layers"
import { cn } from "@/lib/utils"

// -- the parts every card uses ------------------------------------------------------------------

// The summary: one plain sentence, and a quieter line of detail under it.
export function Lead({ children, detail, tone }: { children: ReactNode; detail?: ReactNode; tone?: "signal" }) {
  return (
    <div className="flex flex-col gap-1" aria-live="polite">
      <p className={cn("text-[15px] leading-snug font-medium", tone === "signal" && "text-signal")}>{children}</p>
      {detail && <p className="text-[13px] leading-snug text-muted-foreground">{detail}</p>}
    </div>
  )
}

export function LeadSkeleton() {
  return (
    <div className="flex flex-col gap-2" aria-label="Loading">
      <Skeleton className="h-4 w-4/5" />
      <Skeleton className="h-3 w-1/2" />
    </div>
  )
}

export function DotLegend({ items, label }: { items: [string, string][]; label: string }) {
  return (
    <ul aria-label={label} className="flex flex-wrap gap-x-3.5 gap-y-1.5 text-[12.5px] text-muted-foreground">
      {items.map(([color, text]) => (
        <li key={text} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full ring-1 ring-night/60" style={{ background: color }} aria-hidden="true" />
          {text}
        </li>
      ))}
    </ul>
  )
}

const NO_RECORD = "The twin's memory holds nothing for this moment."

// A workspace by id, if one is registered (the Mobility workspace, the environment twin's).
function workspaceLink(id: string, label: string): TopicLink | null {
  if (!useWorkspace.getState().list.some((w) => w.id === id)) return null
  return { label, run: () => { selectTopic(null); openWorkspace(id) } }
}

// -- Buses --------------------------------------------------------------------------------------

function BusesSummary() {
  const buses = useMobility((s) => s.buses)
  const lines = useMobility((s) => s.summary?.buses)
  const { past } = useStrip()
  useEffect(() => watchSummary(), [])
  if (past) return <Lead detail="They come back when the clock returns to now.">Buses are live only.</Lead>
  if (buses.status === "unavailable") {
    return <Lead tone="signal" detail="Stop timetables still work.">The live bus feed isn't answering. Try again in a minute.</Lead>
  }
  if (buses.status === "idle" || buses.status === "loading") return <LeadSkeleton />
  if (!buses.count) return <Lead detail="The feed answers, but no bus is reporting.">No buses on the road just now.</Lead>
  const late = lines?.late ?? 0
  return (
    <Lead detail={[lines?.lines ? `On ${fmtCount(lines.lines)} lines` : null, late ? `${fmtCount(late)} running late` : null].filter(Boolean).join(", ")}>
      {fmtCount(buses.count)} buses on the road now{buses.status === "stale" ? ", as of a few minutes ago" : ""}.
    </Lead>
  )
}

function BusesLegend() {
  const byLine = useMobility((s) => s.summary?.buses?.by_line)
  const busiest = [...(byLine ?? [])].sort((a, b) => b.buses - a.buses).slice(0, 6)
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[12.5px] text-muted-foreground">Each bus in its line's colour, gliding along its route.</p>
      {busiest.length > 0 && (
        <ul aria-label="The busiest lines" className="flex flex-wrap gap-1.5">
          {busiest.map((l) => <li key={l.id}><LinePill line={l.line} color={l.color} /></li>)}
        </ul>
      )}
    </div>
  )
}

registerTopic({
  id: "buses", label: "Buses", icon: BusFrontIcon, order: 0,
  description: "Live buses on the map, coloured by line",
  sources: ["bus_live", "bus_timetable"],
  show: () => {
    const was = useMobility.getState().buses.on
    setBusesOn(true)
    return () => setBusesOn(was)
  },
  Summary: BusesSummary,
  Legend: BusesLegend,
  ask: () => "Where are the buses right now?",
  deeper: () => workspaceLink("mobility", "Open Mobility"),
})

// -- Parking ------------------------------------------------------------------------------------

function ParkingSummary() {
  const { strip, past, loading } = useStrip()
  const p = strip?.parking
  if (loading) return <LeadSkeleton />
  if (!p) return <Lead detail="The bay sensors will be back shortly.">Parking isn't reporting just now.</Lead>
  if (p.unknown) return <Lead detail={p.reason ? cap(p.reason) : undefined}>{NO_RECORD}</Lead>
  const word = parkingWord(p)
  return (
    <Lead detail={`Of ${fmtCount(p.total)} bays with a sensor${past ? ", as they were then" : ""}`}>
      {word} {past ? "then" : "now"} in the centre, {fmtCount(p.free)} bays free.
    </Lead>
  )
}

const PARKING_WORDS: Record<string, string> = { free: "Free", occupied: "Taken", unknown: "No reading" }

function ParkingLegend() {
  const legend = useSystemLayers((s) => s.parking_sensor.meta?.legend)
  const items = Object.entries(legend ?? { free: "#16a34a", occupied: "#dc2626" })
    .filter(([k]) => k in PARKING_WORDS)
    .map(([k, c]): [string, string] => [c, PARKING_WORDS[k]])
  return <DotLegend label="Parking bays" items={items} />
}

registerTopic({
  id: "parking", label: "Parking", icon: CarFrontIcon, order: 10,
  description: "Every parking bay with a sensor, free or taken",
  sources: ["smart_nicosia"],
  show: () => {
    const was = systemLayerOn("parking_sensor")
    setSystemLayer("parking_sensor", true)
    return () => setSystemLayer("parking_sensor", was)
  },
  Summary: ParkingSummary,
  Legend: ParkingLegend,
  ask: () => "Where can I park right now? Show the free spaces on the map",
  deeper: () => ({ label: "Every sensor in Layers", run: () => openLayers("sensors") }),
})

// -- Traffic ------------------------------------------------------------------------------------

function TrafficSummary() {
  const roads = useMobility((s) => s.roads)
  const summary = useMobility((s) => s.summary?.roads)
  const { strip, past } = useStrip()
  useEffect(() => watchSummary(), [])
  if (roads.status === "idle" || roads.status === "loading") return <LeadSkeleton />
  if (roads.status === "unavailable") {
    return past
      ? <Lead>The twin's memory has no road speeds for that moment.</Lead>
      : <Lead tone="signal">The national traffic feed isn't answering. Try again in a minute.</Lead>
  }
  if (roads.status === "no_readings") return <Lead>No road has a reading in the last 30 minutes.</Lead>
  if (past) {
    return (
      <Lead detail={`${fmtCount(roads.measured)} of ${fmtCount(roads.total)} road segments had a reading`}>
        Road speeds as the twin recorded them then.
      </Lead>
    )
  }
  const by = summary?.by_state ?? {}
  const slow = (by.slow ?? 0) + (by.very_slow ?? 0)
  const busy = by.busy ?? 0
  const worst = strip?.slowest_road ?? (summary?.slowest?.[0]?.speed_kmh != null
    ? { name: summary.slowest[0].road ?? summary.slowest[0].name, speed_kmh: summary.slowest[0].speed_kmh } : null)
  const lead = !summary ? "Road speeds against each road's free flow."
    : slow === 0 ? (busy ? "Busy on a few roads, slow on none." : "Moving freely on the measured roads.")
      : `Slow on ${fmtCount(slow)} of ${fmtCount(roads.measured)} road segments.`
  const detail = worst && slow > 0 ? `Slowest: ${worst.name}, ${Math.round(worst.speed_kmh)} km/h` : "Updated every 5 minutes"
  return <Lead detail={detail}>{lead}</Lead>
}

registerTopic({
  id: "traffic", label: "Traffic", icon: GaugeIcon, order: 20,
  description: "Main roads coloured by speed against their free flow",
  sources: ["road_speeds"],
  show: () => {
    const was = useMobility.getState().roads.on
    setRoadsOn(true)
    return () => setRoadsOn(was)
  },
  Summary: TrafficSummary,
  Legend: () => <RoadLegend />,
  ask: () => "Which roads are slow right now?",
  deeper: () => workspaceLink("mobility", "Open Mobility"),
})

// -- Outside ------------------------------------------------------------------------------------

function OutsideSummary() {
  const { strip, past, loading } = useStrip()
  const sky = useSky()
  if (loading) return <LeadSkeleton />
  const w = strip?.weather
  const air = strip?.air
  const words = airWords(air)
  const weather = w ? `${skyWord(w)}, ${Math.round(w.temp_c)}°C` : null
  const lead = [weather, words ? `${past ? "the air was" : "the air is"} ${words.toLowerCase()}` : null].filter(Boolean).join(", ")
  const light = `${sky.dark ? "Light at" : "Dark at"} ${nicosiaTime(sky.next.at)}${sky.dark ? `, ${moonName(sky.moon.phase).toLowerCase()}` : ""}`
  const caqi = air && !air.unknown && air.caqi !== undefined ? `Air quality index ${air.caqi} across ${fmtCount(air.stations)} stations` : null
  return (
    <Lead detail={[caqi, light].filter(Boolean).join(". ")}>
      {lead ? `${cap(lead)}.` : air?.unknown ? NO_RECORD : "The weather and the air aren't reporting just now."}
    </Lead>
  )
}

function OutsideLegend() {
  const legend = useSystemLayers((s) => s.air_station.meta?.legend)
  const fallback: Record<string, string> = { very_low: "#16a34a", low: "#16a34a", medium: "#eab308", high: "#f97316", very_high: "#7e22ce" }
  const items = AIR_BANDS.filter((b) => !legend || b in legend).map((b): [string, string] => [legend?.[b] ?? fallback[b], AIR_WORDS[b]])
  return <DotLegend label="Air quality at the stations" items={items} />
}

registerTopic({
  id: "outside", label: "Outside", icon: SunIcon, order: 30,
  description: "The weather, the air at the stations and when it gets dark",
  sources: ["open_meteo", "smart_nicosia"],
  show: () => {
    const was = systemLayerOn("air_station")
    setSystemLayer("air_station", true)
    return () => setSystemLayer("air_station", was)
  },
  Summary: OutsideSummary,
  Legend: OutsideLegend,
  ask: () => "Is it a good time for a walk around the old town?",
  // The environment twin's workspace, once it is there; until then the stations in Layers.
  deeper: () => workspaceLink("environment", "Open Environment") ?? { label: "Air and noise in Layers", run: () => openLayers("sensors") },
})
