/* The measuring stations in the calm layout: a "Measuring stations" row in the Layers sheet's
   Environment group, a "Measured" section on the Outside card (the centre's thermometer with its
   age, the rain of the last hour, the wind and the traffic station's air), and two inspector
   sections: on a station, how the city's sensors compare with it; on a city air sensor, how it
   compares with the official station nearest it. */
import { GaugeIcon, ThermometerIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { ENV_GROUP } from "@/components/environment/rows"
import { Skeleton } from "@/components/ui/skeleton"
import { Toggle } from "@/components/ui/toggle"
import { useClock } from "@/lib/clock"
import type { Entity } from "@/lib/entities"
import {
  AIR_RING, WEATHER_DOT, fetchComparison, headline, setStationsOn, useStations, watchStations,
  type Comparison, type MeasuredStation, type StationsData,
} from "@/lib/environment/stations"
import { inspect } from "@/lib/inspector"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { registerSystemLayer } from "@/lib/registries/layers"
import { registerTopicSection } from "@/lib/registries/topics"

function ago(min: number | null | undefined) {
  if (min === null || min === undefined) return ""
  if (min < 2) return "just now"
  if (min < 90) return `${min} minutes ago`
  return `${Math.round(min / 60)} hours ago`
}

const byId = (d: StationsData | null, id: string) => d?.stations.find((s) => s.id === id)

// The centre's thermometer: Lefkosia, else the nearest Met station that reads the temperature.
function thermometer(d: StationsData | null): MeasuredStation | undefined {
  for (const id of ["met-LEFKOSIA", "met-ATHALASSA", "met-VISITOR", "met-MAMMARI"]) {
    const s = byId(d, id)
    if (s?.readings.temperature_c) return s
  }
  return undefined
}

function airLine(s: MeasuredStation | undefined) {
  const no2 = s?.readings.no2
  if (!s || !no2) return null
  const pm10 = s.readings.pm10
  return `NO₂ ${Math.round(no2.value)}${pm10 ? `, PM10 ${Math.round(pm10.value)}` : ""} µg/m³ at the ${s.name.replace(/^Nicosia /, "")}, ${no2.as_of.slice(11, 16)} to ${no2.until?.slice(11, 16)}`
}

function summaryLine(d: StationsData | null, past: boolean) {
  const t = thermometer(d)
  const tr = t?.readings.temperature_c
  const air = airLine(byId(d, "aq-CY0004A"))
  if (!tr && !air) return past ? "No station readings recorded for that moment" : "No station has reported yet"
  const parts = []
  if (t && tr) parts.push(`${tr.value.toFixed(1)} °C at ${t.name}${past ? "" : `, ${ago(tr.age_min)}`}`)
  if (air) parts.push(air)
  return parts.join("; ")
}

// -- the Layers sheet's row ------------------------------------------------------------------

function StationsKey() {
  return (
    <ul className="flex flex-col gap-1.5 text-[12.5px] text-muted-foreground">
      <li className="flex items-center gap-2">
        <span className="size-2.5 shrink-0 rounded-full" style={{ background: WEATHER_DOT }} aria-hidden="true" />
        Weather station of the Met Department, with its temperature (every 10 minutes)
      </li>
      <li className="flex items-center gap-2">
        <span className="size-3 shrink-0 rounded-full border-[2.5px]" style={{ borderColor: AIR_RING }} aria-hidden="true" />
        Official air station, with its NO₂ in µg/m³ (hourly, two to three hours behind)
      </li>
    </ul>
  )
}

registerSystemLayer({
  id: "env_stations", group: ENV_GROUP, order: 35, label: "Measuring stations",
  useOn: () => useStations((s) => s.on),
  setOn: setStationsOn,
  Summary: function Summary() {
    const st = useStations()
    const at = useClock((s) => s.at)
    if (!st.on) return <>Real thermometers, rain gauges and the official air stations</>
    if (st.status === "error") return <>Couldn't read the stations: {st.error}. Switch it off and on to try again</>
    if (!st.data) return <>Reading the stations</>
    return <>{summaryLine(st.data, at !== null)}</>
  },
  Legend: StationsKey,
})

// -- the Outside card's section --------------------------------------------------------------

function Readout({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="font-condensed text-[12px] text-muted-foreground">{label}</dt>
      <dd className="text-[13.5px] leading-snug">{children}</dd>
    </div>
  )
}

function OutsideMeasured() {
  useEffect(() => watchStations(), [])
  const { data, status, error, on } = useStations()
  const past = useClock((s) => s.at) !== null
  if (!data && status !== "error") {
    return <div className="flex flex-col gap-2" aria-busy="true"><Skeleton className="h-4 w-40" /><Skeleton className="h-8 w-full" /></div>
  }
  const t = thermometer(data)
  const tr = t?.readings.temperature_c
  const rainAt = byId(data, "met-LEFKOSIA") ?? data?.stations.find((s) => s.rain_last_hour_mm !== undefined)
  const wind = data?.stations.find((s) => s.readings.wind_ms)
  const traffic = byId(data, "aq-CY0004A")
  return (
    <section aria-label="Measured" className="flex flex-col gap-2.5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[12.5px] text-muted-foreground">Measured {past ? "then" : "now"}</h3>
        <Toggle variant="outline" size="sm" pressed={on} onPressedChange={(p) => setStationsOn(p)} aria-label="Show the measuring stations on the map">
          <GaugeIcon data-icon="inline-start" />
          Stations
        </Toggle>
      </div>
      {status === "error" && !data ? (
        <p className="text-[12.5px] text-muted-foreground">The stations couldn't be read ({error}). The model below still stands.</p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2" aria-live="polite">
          <Readout label="Temperature">
            {t && tr ? (
              <button type="button" className="text-left hover:underline focus-visible:underline focus-visible:outline-none" onClick={(e) => inspect({ id: t.id, at: [t.lon, t.lat] }, { opener: e.currentTarget })}>
                <span className="font-readout text-[16px] font-semibold">{tr.value.toFixed(1)} °C</span>{" "}
                <span className="text-muted-foreground">at {t.name}{past ? "" : `, ${ago(tr.age_min)}`}</span>
              </button>
            ) : <span className="text-muted-foreground">{past ? "Not recorded then" : "No reading yet"}</span>}
          </Readout>
          <Readout label="Rain, last hour">
            {rainAt?.rain_last_hour_mm !== undefined
              ? <><span className="font-readout text-[16px] font-semibold">{rainAt.rain_last_hour_mm}</span> mm <span className="text-muted-foreground">at {rainAt.name}</span></>
              : <span className="text-muted-foreground">Unknown</span>}
          </Readout>
          <Readout label="Wind, measured">
            {wind?.readings.wind_ms
              ? <><span className="font-readout text-[16px] font-semibold">{wind.readings.wind_ms.kmh}</span> km/h <span className="text-muted-foreground">at {wind.name}</span></>
              : <span className="text-muted-foreground">Unknown</span>}
          </Readout>
          <Readout label="Air, official station">
            {traffic?.readings.no2 ? (
              <button type="button" className="text-left hover:underline focus-visible:underline focus-visible:outline-none" onClick={(e) => inspect({ id: traffic.id, at: [traffic.lon, traffic.lat] }, { opener: e.currentTarget })}>
                <span className="font-readout text-[16px] font-semibold">{Math.round(traffic.readings.no2.value)}</span> NO₂{" "}
                <span className="text-muted-foreground">µg/m³, {traffic.readings.no2.as_of.slice(11, 16)} to {traffic.readings.no2.until?.slice(11, 16)}</span>
              </button>
            ) : <span className="text-muted-foreground">No hour yet</span>}
          </Readout>
        </dl>
      )}
    </section>
  )
}

registerTopicSection({ id: "measured", topic: "outside", order: 5, Section: OutsideMeasured })

// -- the inspector ----------------------------------------------------------------------------

function ComparisonList({ rows, title, empty, open }: { rows: Comparison[]; title: string; empty: string; open: "sensor" | "reference" }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h3 className="text-[12.5px] text-muted-foreground">{title}</h3>
      {rows.length === 0 ? <p className="text-[12.5px] leading-snug text-muted-foreground">{empty}</p> : (
        <ul className="flex flex-col gap-1.5">
          {rows.map((c) => {
            const id = open === "sensor" ? c.sensor_id : c.reference_id
            const name = open === "sensor" ? c.sensor : c.reference
            const off = c.bias_pct !== undefined ? Math.abs(c.bias_pct) : c.bias !== undefined ? Math.abs(c.bias) * 10 : null
            return (
              <li key={`${c.sensor_id}-${c.metric}`} className="flex items-start gap-2 text-[13px] leading-snug">
                <span className="mt-1.5 size-2 shrink-0 rounded-full" aria-hidden="true"
                  style={{ background: off === null ? "var(--muted-foreground)" : off < 10 ? "#4fbf8f" : off < 25 ? "#e0b43a" : "#ff8a3d" }} />
                <span>
                  <span className="font-medium">{c.label}</span>{" "}
                  <button type="button" className="underline-offset-2 hover:underline focus-visible:underline focus-visible:outline-none" onClick={(e) => inspect({ id }, { opener: e.currentTarget })}>
                    {name}
                  </button>
                  <span className="text-muted-foreground">: {c.text ?? c.note}</span>
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

type StationEntity = Entity & { comparison?: Comparison[] }

function StationSection({ entity }: SectionProps) {
  const weather = (entity.kind as string) === "weather_station"
  // The station's headline (temperature, or NO₂), one line per city sensor; the sensor's own
  // inspector tells the rest.
  const rows = ((entity as StationEntity).comparison ?? [])
    .filter((c) => c.metric === (weather ? "temperature_c" : "no2") && (c.bias !== undefined || c.hours > 0))
    .sort((a, b) => a.distance_m - b.distance_m)
  return (
    <ComparisonList
      rows={rows}
      open="sensor"
      title={weather ? "City sensors' temperature against this station, past 7 days" : "City sensors' NO₂ against this station, past 7 days"}
      empty={weather
        ? "No city sensor is compared with this station yet: the twin records the Met readings from when it starts reading them, and needs a day of both."
        : "No city sensor has this as its nearest official station, or the twin's memory doesn't yet hold enough hours of both."}
    />
  )
}

registerInspectorSection({ id: "measured-station", order: 45, kinds: ["weather_station", "reference_air_station"], Component: StationSection })

function CitySensorSection({ entity }: SectionProps) {
  // Kept with the id it was read for, so another sensor never shows this one's comparison.
  const [read, setRead] = useState<{ id: string; rows: Comparison[] | null; failed: boolean } | null>(null)
  useEffect(() => {
    const ac = new AbortController()
    fetchComparison({ sensor: entity.id }, ac.signal)
      .then((d) => setRead({ id: entity.id, rows: d.rows, failed: false }))
      .catch(() => { if (!ac.signal.aborted) setRead({ id: entity.id, rows: null, failed: true }) })
    return () => ac.abort()
  }, [entity.id])
  const mine = read?.id === entity.id ? read : null
  const rows = mine?.rows ?? null
  if (mine?.failed) return <p className="text-[12.5px] text-muted-foreground">The comparison with the official stations couldn't be read. Try again in a moment.</p>
  if (rows === null) return <Skeleton className="h-12 w-full" />
  return (
    <ComparisonList
      rows={rows}
      open="reference"
      title="Against the official stations, past 7 days"
      empty="Nothing to compare yet: the twin's memory needs hours of this sensor beside an official station."
    />
  )
}

registerInspectorSection({ id: "against-reference", order: 46, kinds: ["air_station"], Component: CitySensorSection })

export const STATION_ICONS = { weather_station: ThermometerIcon, reference_air_station: GaugeIcon }
export { headline }
