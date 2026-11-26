/* The environment twin in the calm layout. Its own group in the Layers sheet (terrain, shadows,
   sun hours, the air and noise surfaces, the wind and the rain radar, each with its switch and a
   line on what it shows now), and its section on the Outside card: the sun and the wind at the
   clock's moment, the same layers one press away, and "What's it like here?". The Environment
   workspace lists the same rows. */
import { MapPinnedIcon } from "lucide-react"
import { useEffect, useId, useMemo, useState } from "react"

import { SunHoursLegend, SurfaceLegend, windLine } from "@/components/environment/parts"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useClock } from "@/lib/clock"
import type { Metric } from "@/lib/environment/colors"
import {
  clearSunHours, mapSunHours, openSpotAtCentre, setRainOn, setRelief, setShadowsOn, setSurfaceMetric, setSurfaceOn,
  setTerrainOn, setWindOn,
} from "@/lib/environment/map"
import { METRICS, readWind, useEnvironment, type Surface, type Wind } from "@/lib/environment/store"
import { useMapUi } from "@/lib/map/engine"
import { skyAt } from "@/lib/map/sky"
import { registerLayerGroup, registerSystemLayer } from "@/lib/registries/layers"
import { registerTopicSection } from "@/lib/registries/topics"
import { openWorkspace } from "@/lib/registries/workspaces"
import { closeLayers } from "@/lib/layers-sheet"

export const ENV_GROUP = "environment"

const RELIEF = [
  { value: "1", label: "True", hint: "The ground at its true height" },
  { value: "1.5", label: "1.5×", hint: "Relief raised one and a half times" },
  { value: "3", label: "3×", hint: "Relief raised three times, to read a flat city" },
]

const hm = (ms: number) => new Date(ms).toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })

export function niceDate(iso: string) {
  return new Date(`${iso}T12:00`).toLocaleDateString("en-GB", { day: "numeric", month: "long" })
}

export function compass(deg: number) {
  return ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][Math.round((deg % 360) / 45) % 8]
}

function surfaceLine(s: { metric: Metric | null; status: string; error: string | null; data: Surface | null }, at: number | null) {
  if (!s.metric) return "Air, noise or heat over the city, from the stations and the air model"
  if (s.status === "loading" && !s.data) return "Reading the stations and the model"
  if (s.status === "error") return `Couldn't draw it: ${s.error}. Switch it off and on to try again`
  const d = s.data
  if (!d) return "Reading the stations and the model"
  if (!d.known) return `Nothing is known for ${at === null ? "now" : "that moment"}: ${d.reason ?? "no stations and no model"}`
  const n = d.stations?.length ?? 0
  const label = METRICS.find((m) => m.id === d.metric)?.label ?? d.label
  return `${label} from ${n} station${n === 1 ? "" : "s"}${d.background === "model" ? " over the air model" : ", near them only"}${at === null ? ", now" : ", then"}`
}

// -- the Layers sheet's Environment group -----------------------------------------------------

registerLayerGroup({ id: ENV_GROUP, label: "Environment", order: 5 })

function Relief() {
  const id = useId()
  const exaggeration = useEnvironment((s) => s.terrain.exaggeration)
  return (
    <Field>
      <FieldLabel id={id} className="text-[12.5px] font-normal text-muted-foreground">Relief</FieldLabel>
      <ToggleGroup
        aria-labelledby={id}
        variant="outline" size="sm" spacing={0} className="w-full"
        value={[String(exaggeration)]}
        onValueChange={(v) => { if (v[0]) setRelief(Number(v[0])) }}
      >
        {RELIEF.map((r) => (
          <ToggleGroupItem key={r.value} value={r.value} className="flex-1" aria-label={r.hint}>{r.label}</ToggleGroupItem>
        ))}
      </ToggleGroup>
    </Field>
  )
}

registerSystemLayer({
  id: "env_terrain", group: ENV_GROUP, order: 0, label: "Terrain",
  useOn: () => useEnvironment((s) => s.terrain.on),
  setOn: setTerrainOn,
  Summary: () => <>Hills lit by the sun, the 3D city standing on them</>,
  Legend: Relief,
})

registerSystemLayer({
  id: "env_shadows", group: ENV_GROUP, order: 10, label: "Shadows",
  useOn: () => useEnvironment((s) => s.shadows.on),
  setOn: setShadowsOn,
  Summary: function Summary() {
    const shadows = useEnvironment((s) => s.shadows)
    const city = useMapUi((s) => s.city.on)
    if (!shadows.on) return <>Cast by every 3D building at the clock's moment</>
    return <>{shadows.note || (city ? "Cast by the 3D city at the clock's moment" : "Turning on the 3D city")}</>
  },
})

function SunHoursKey() {
  return (
    <div className="flex flex-col gap-2">
      <SunHoursLegend />
      <Button variant="ghost" size="sm" className="-ml-2 self-start" onClick={() => { closeLayers(true); openWorkspace("environment") }}>
        Pick a square or a date in Environment
      </Button>
    </div>
  )
}

registerSystemLayer({
  id: "env_sun_hours", group: ENV_GROUP, order: 20, label: "Sun hours",
  useOn: () => useEnvironment((s) => s.sunHours.shown || s.sunHours.status === "loading"),
  setOn: (on) => { if (on) void mapSunHours(); else clearSunHours() },
  Summary: function Summary() {
    const sh = useEnvironment((s) => s.sunHours)
    if (sh.status === "loading") return <>Reading the buildings around {sh.place.name === "The map's centre" ? "the map's centre" : sh.place.name}</>
    if (sh.status === "error") return <>Couldn't read the sun hours: {sh.error}. Try again in a moment</>
    const d = sh.data
    if (sh.shown && d) {
      const where = sh.place.name === "The map's centre" ? "Around the map's centre" : `Around ${sh.place.name}`
      return <>{where} on {niceDate(d.date)}: {d.hours.mean ?? "?"} h of direct sun on average, of {d.sun.daylight_h} h of daylight</>
    }
    return <>Hours of direct sun over a day, around a square or the map's centre</>
  },
  Legend: SunHoursKey,
})

function SurfaceKey() {
  const id = useId()
  const surface = useEnvironment((s) => s.surface)
  const m = METRICS.find((x) => x.id === surface.metric)
  if (!surface.metric) return null
  return (
    <div className="flex flex-col gap-2.5">
      <Field>
        <FieldLabel htmlFor={id} className="text-[12.5px] font-normal text-muted-foreground">Show</FieldLabel>
        <Select value={surface.metric} onValueChange={(v) => setSurfaceMetric(v as Metric)}>
          <SelectTrigger id={id} size="sm" className="w-full">
            <SelectValue>{m ? `${m.label}${m.unit ? `, ${m.unit}` : ""}` : null}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {METRICS.map((x) => (
              <SelectItem key={x.id} value={x.id}>
                <span className="flex flex-col">
                  <span>{x.label}</span>
                  <span className="text-[11.5px] text-muted-foreground">{x.what}</span>
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      {surface.data?.known && surface.data.bands && <SurfaceLegend surface={surface.data} />}
    </div>
  )
}

registerSystemLayer({
  id: "env_surface", group: ENV_GROUP, order: 30, label: "Air and noise surface",
  useOn: () => useEnvironment((s) => s.surface.metric !== null),
  setOn: setSurfaceOn,
  Summary: function Summary() {
    const surface = useEnvironment((s) => s.surface)
    const at = useClock((s) => s.at)
    return <>{surfaceLine(surface, at)}</>
  },
  Legend: SurfaceKey,
})

registerSystemLayer({
  id: "env_wind", group: ENV_GROUP, order: 40, label: "Wind",
  useOn: () => useEnvironment((s) => s.wind.on),
  setOn: setWindOn,
  Summary: function Summary() {
    const wind = useEnvironment((s) => s.wind)
    if (!wind.on) return <>Moving with the modelled wind at 10 m</>
    if (wind.status === "error") return <>The weather model didn't answer: {wind.error}</>
    if (wind.data && !wind.data.known) return <>{wind.data.reason}</>
    return <>{windLine(wind.data) ?? "Reading the weather model"}</>
  },
  Legend: function Legend() {
    const still = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
    return (
      <p className="text-[12.5px] leading-snug text-muted-foreground">
        {still ? "Arrows point where the wind goes, longer where it is stronger." : "Each streak drifts where the wind goes, faster where it is stronger."}
      </p>
    )
  },
})

registerSystemLayer({
  id: "env_rain", group: ENV_GROUP, order: 50, label: "Rain radar",
  useOn: () => useEnvironment((s) => s.rain.on),
  setOn: setRainOn,
  Summary: function Summary() {
    const rain = useEnvironment((s) => s.rain)
    if (!rain.on) return <>RainViewer's radar of the last two hours</>
    if (rain.status === "loading" && !rain.frame) return <>Reading the radar</>
    if (rain.status === "error") return <>The radar isn't answering{rain.error ? `: ${rain.error}` : ""}. Try again in a few minutes</>
    if (rain.status === "old") return <>The radar keeps the last two hours only. Move the clock nearer to now</>
    if (rain.frame) return <>Radar of {hm(rain.frame.time)}: coloured where it rains, clear where it is dry</>
    return <>Reading the radar</>
  },
})

// -- the Outside card's section --------------------------------------------------------------

function useWindReadout(at: number | null): Wind | null {
  const now = useClock((s) => s.now)
  // Live, a new hour of the model is read when it comes.
  const hour = new Date(at ?? now).toISOString().slice(0, 13)
  const [wind, setWind] = useState<{ hour: string; data: Wind | null } | null>(null)
  useEffect(() => {
    let live = true
    readWind(at).then((data) => { if (live) setWind({ hour, data }) }).catch(() => { if (live) setWind({ hour, data: null }) })
    return () => { live = false }
  }, [hour]) // eslint-disable-line react-hooks/exhaustive-deps -- one read per hour of the model
  return wind?.hour === hour ? wind.data : null
}

const SHOWN = [
  { id: "shadows", label: "Shadows" },
  { id: "wind", label: "Wind" },
  { id: "surface", label: "Air" },
  { id: "rain", label: "Rain" },
] as const

function OutsideEnvironment() {
  const at = useClock((s) => s.at)
  const skyNow = useMapUi((s) => s.skyNow)
  const sky = useMemo(() => (at === null ? skyNow : skyAt(new Date(at))), [at, skyNow])
  const wind = useWindReadout(at)
  const on = {
    shadows: useEnvironment((s) => s.shadows.on),
    wind: useEnvironment((s) => s.wind.on),
    surface: useEnvironment((s) => s.surface.metric !== null),
    rain: useEnvironment((s) => s.rain.on),
  }
  const set: Record<keyof typeof on, (v: boolean) => void> = { shadows: setShadowsOn, wind: setWindOn, surface: setSurfaceOn, rain: setRainOn }
  const up = sky.sun.altitude > 0
  const c = wind?.known ? wind.centre : null
  const labelId = useId()
  return (
    <div className="flex flex-col gap-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1" aria-live="polite">
        <div className="flex flex-col gap-0.5">
          <dt className="font-condensed text-[12px] text-muted-foreground">Sun</dt>
          <dd className="text-[13.5px] leading-snug">
            {up ? <><span className="font-readout text-[16px] font-semibold">{Math.round(sky.sun.altitude)}°</span> up, from the {compass(sky.sun.bearing)}</> : "Down"}
          </dd>
        </div>
        <div className="flex flex-col gap-0.5">
          <dt className="font-condensed text-[12px] text-muted-foreground">Wind</dt>
          <dd className="text-[13.5px] leading-snug">
            {c && c.speed_ms !== null
              ? <><span className="font-readout text-[16px] font-semibold">{Math.round(c.speed_ms * 3.6)}</span> km/h from the {c.from}</>
              : <span className="text-muted-foreground">{wind === null ? "Reading" : "Unknown"}</span>}
          </dd>
        </div>
      </dl>
      <div className="flex flex-col gap-1.5">
        <span id={labelId} className="text-[12.5px] text-muted-foreground">On the map</span>
        <ToggleGroup
          multiple
          aria-labelledby={labelId}
          variant="outline" size="sm" spacing={0} className="w-full"
          value={SHOWN.filter((s) => on[s.id]).map((s) => s.id)}
          onValueChange={(next) => {
            for (const s of SHOWN) {
              const want = next.includes(s.id)
              if (want !== on[s.id]) set[s.id](want)
            }
          }}
        >
          {SHOWN.map((s) => <ToggleGroupItem key={s.id} value={s.id} className="flex-1">{s.label}</ToggleGroupItem>)}
        </ToggleGroup>
      </div>
      <Button variant="outline" size="sm" className="self-start" onClick={(e) => openSpotAtCentre(e.currentTarget)}>
        <MapPinnedIcon data-icon="inline-start" />
        What's it like here?
      </Button>
    </div>
  )
}

registerTopicSection({ id: "environment", topic: "outside", order: 10, Section: OutsideEnvironment })
