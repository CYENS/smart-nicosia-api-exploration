/* What the Layers row, its legend and the Outside card say about natural hazards: the latest quake
   that reached the city, a warning in force and the district's fire danger, in plain words. */
import { MapIcon } from "lucide-react"
import { useEffect } from "react"

import { DotLegend } from "@/components/topics/builtin"
import { Button } from "@/components/ui/button"
import { useClock } from "@/lib/clock"
import { closeLayers } from "@/lib/layers-sheet"
import { flyToBounds } from "@/lib/map/engine"
import {
  FIRE_COLOR, FIRE_WORD, LEVEL_COLOR, useHazards, needHazards, wallWords, type HazardsAt, type Quake, type Warning,
} from "@/lib/hazards/store"

export const ISLAND: [number, number, number, number] = [32.25, 34.55, 34.6, 35.7]
const NOTABLE = new Set(["high", "medium", "low"])

// The quakes the watch raises (near and strong enough), strongest first.
export function notable(d: HazardsAt | null): Quake[] {
  if (!d) return []
  const sev = new Map(d.features.filter((f) => f.properties.kind === "earthquake").map((f) => [f.properties.id, f.properties.severity]))
  return d.earthquakes.filter((q) => NOTABLE.has(sev.get(q.id))).sort((a, b) => b.value - a.value)
}

export function warningWords(w: Warning, live: boolean) {
  const kind = `${w.level.charAt(0).toUpperCase()}${w.level.slice(1)} ${w.awareness ?? "weather"} warning`
  if (w.in_force) return `${kind} in force until ${wallWords(w.end, { day: w.end?.slice(0, 10) !== w.start.slice(0, 10) ? undefined : false })}`
  return `${kind} ${live ? "issued for" : "for"} ${wallWords(w.start)} to ${wallWords(w.end, { day: false })}`
}

function quakeWords(q: Quake) {
  const felt = q.felt?.near_nicosia ? `, felt by ${q.felt.near_nicosia.toLocaleString("en-GB")} near Nicosia` : ""
  return `M${q.value.toFixed(1)} earthquake ${q.distance_km?.toFixed(0)} km ${q.direction}, ${wallWords(q.start)}${felt}`
}

export function HazardsSummary() {
  const data = useHazards((s) => s.data)
  const status = useHazards((s) => s.status)
  const error = useHazards((s) => s.error)
  if (status === "error" && !data) return <>The hazard feeds didn't answer ({error}). The row tries again in a few minutes</>
  if (!data) return <>Earthquakes, weather warnings and fire danger, from the official sources</>
  const parts: string[] = []
  const w = data.warnings.find((x) => x.covers_nicosia && x.in_force) ?? data.warnings.find((x) => x.covers_nicosia)
  if (w) parts.push(warningWords(w, data.live))
  const q = notable(data)[0]
  parts.push(q ? quakeWords(q) : "no earthquake near enough to matter in 30 days")
  if (data.fire) parts.push(`fire danger ${FIRE_WORD[data.fire.level]?.toLowerCase()} in the district`)
  const text = parts.join("; ")
  return <>{text.charAt(0).toUpperCase() + text.slice(1)}</>
}

export function HazardsLegend() {
  const legend = useHazards((s) => s.data?.legend) ?? {}
  const items = Object.entries(legend).map(([text, color]): [string, string] => [color, text])
  return (
    <div className="flex flex-col items-start gap-1.5">
      {items.length > 0 && <DotLegend label="Natural hazards on the map" items={items} />}
      <p className="text-[12.5px] text-muted-foreground">Rings grow with magnitude. Click one for its record</p>
      <Button variant="link" size="sm" className="h-auto px-0 text-[12.5px]" onClick={() => { closeLayers(); flyToBounds(ISLAND) }}>
        <MapIcon data-icon="inline-start" />See the whole island
      </Button>
    </div>
  )
}

// The Outside card: a warning for the city, or that there is none, and the district's fire danger.
export function HazardsOutside() {
  useEffect(() => needHazards(), [])
  const data = useHazards((s) => s.data)
  const status = useHazards((s) => s.status)
  const past = useClock((s) => s.at !== null)
  if (!data) {
    return <p className="text-[12.5px] text-muted-foreground">{status === "error" ? "Weather warnings couldn't be read just now" : "Reading the weather warnings"}</p>
  }
  const warnings = data.warnings.filter((w) => w.covers_nicosia)
  const fire = data.fire
  return (
    <div className="flex flex-col gap-1.5" aria-live="polite">
      {warnings.length ? (
        <ul aria-label="Weather warnings for Nicosia" className="flex flex-col gap-1">
          {warnings.map((w) => (
            <li key={w.id} className="flex items-start gap-2 text-[13px] leading-snug">
              <span className="mt-1 size-2.5 shrink-0 rounded-full" style={{ background: LEVEL_COLOR[w.level] }} aria-hidden="true" />
              <span>{warningWords(w, data.live)}<span className="text-muted-foreground">, Department of Meteorology</span></span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[12.5px] text-muted-foreground">No weather warning for Cyprus {past ? "then" : "now"} (Meteoalarm)</p>
      )}
      {fire && (
        <p className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
          <span className="size-2.5 shrink-0 rounded-full" style={{ background: FIRE_COLOR[fire.level] }} aria-hidden="true" />
          Fire danger in the Nicosia district: {FIRE_WORD[fire.level]?.toLowerCase()}{fire.forecast ? " (forecast)" : ""}
        </p>
      )}
    </div>
  )
}
