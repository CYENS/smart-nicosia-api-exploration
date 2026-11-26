/* The environment's switches: terrain, the 3D city's shadows, an air, noise or temperature
   surface, the wind and the rain radar, each saying what it shows right now. In the map panel
   and in the Environment workspace. */
import { SurfaceLegend, windLine } from "@/components/environment/parts"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useClock } from "@/lib/clock"
import type { Metric } from "@/lib/environment/colors"
import { setRainOn, setRelief, setShadowsOn, setSurfaceMetric, setTerrainOn, setWindOn } from "@/lib/environment/map"
import { METRICS, useEnvironment } from "@/lib/environment/store"
import { useMapUi } from "@/lib/map/engine"

const RELIEF = [
  { value: "1", label: "True", hint: "The ground at its true height" },
  { value: "1.5", label: "1.5×", hint: "Relief raised one and a half times" },
  { value: "3", label: "3×", hint: "Relief raised three times, to read a flat city" },
]

function time(ms: number) {
  return new Date(ms).toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })
}

function surfaceLine(s: ReturnType<typeof useEnvironment.getState>["surface"], at: number | null) {
  if (!s.metric) return "Air, noise or temperature over the city, from the stations and the model"
  if (s.status === "loading" && !s.data) return "Reading the stations and the model"
  if (s.status === "error") return `Couldn't draw it: ${s.error}. Try again in a minute.`
  const d = s.data
  if (!d) return ""
  if (!d.known) return `Nothing is known for ${at === null ? "now" : "that moment"}: ${d.reason ?? "no stations and no model"}`
  const n = d.stations?.length ?? 0
  return `${n} station${n === 1 ? "" : "s"}${d.background === "model" ? ` over the model's ${d.model_hour?.slice(11) ?? ""} hour` : ""}${at === null ? ", now" : ", at the clock's moment"}`
}

export function EnvironmentSwitches({ idPrefix = "tg", legends = true }: { idPrefix?: string; legends?: boolean }) {
  const terrain = useEnvironment((s) => s.terrain)
  const shadows = useEnvironment((s) => s.shadows)
  const surface = useEnvironment((s) => s.surface)
  const wind = useEnvironment((s) => s.wind)
  const rain = useEnvironment((s) => s.rain)
  const city = useMapUi((s) => s.city)
  const at = useClock((s) => s.at)
  const metricLabel = METRICS.find((m) => m.id === surface.metric)
  return (
    <>
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={`${idPrefix}-terrain`}>Terrain</FieldLabel>
          <FieldDescription>Hills shaded by the sun, the 3D city standing on them</FieldDescription>
        </FieldContent>
        <Switch id={`${idPrefix}-terrain`} checked={terrain.on} onCheckedChange={(on) => setTerrainOn(on)} />
      </Field>
      {terrain.on && (
        <Field>
          <FieldLabel id={`${idPrefix}-relief-label`} className="text-[12.5px] font-normal text-muted-foreground">Relief</FieldLabel>
          <ToggleGroup
            aria-labelledby={`${idPrefix}-relief-label`}
            variant="outline" size="sm" spacing={0} className="w-full"
            value={[String(terrain.exaggeration)]}
            onValueChange={(v) => { if (v[0]) setRelief(Number(v[0])) }}
          >
            {RELIEF.map((r) => (
              <ToggleGroupItem key={r.value} value={r.value} className="flex-1" aria-label={r.hint}>{r.label}</ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Field>
      )}
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={`${idPrefix}-shadows`}>Shadows</FieldLabel>
          <FieldDescription aria-live="polite">
            {shadows.on ? shadows.note || (city.on ? "Cast by the 3D city at the clock's moment" : "Turning on the 3D city") : "Cast by every 3D building, following the clock"}
          </FieldDescription>
        </FieldContent>
        <Switch id={`${idPrefix}-shadows`} checked={shadows.on} onCheckedChange={(on) => setShadowsOn(on)} />
      </Field>
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={`${idPrefix}-surface`}>Air and noise</FieldLabel>
          <FieldDescription aria-live="polite">{surfaceLine(surface, at)}</FieldDescription>
        </FieldContent>
        <Switch id={`${idPrefix}-surface`} checked={!!surface.metric} onCheckedChange={(on) => setSurfaceMetric(on ? "pm2_5" : null)} />
      </Field>
      {surface.metric && (
        <div className="flex flex-col gap-2.5">
          <Field>
            <FieldLabel htmlFor={`${idPrefix}-metric`} className="text-[12.5px] font-normal text-muted-foreground">Show</FieldLabel>
            <Select value={surface.metric} onValueChange={(v) => setSurfaceMetric(v as Metric)}>
              <SelectTrigger id={`${idPrefix}-metric`} size="sm" className="w-full">
                <SelectValue>{metricLabel ? `${metricLabel.label}${metricLabel.unit ? `, ${metricLabel.unit}` : ""}` : null}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {METRICS.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    <span className="flex flex-col">
                      <span>{m.label}</span>
                      <span className="text-[11.5px] text-muted-foreground">{m.what}</span>
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          {legends && surface.data?.known && surface.data.bands && <SurfaceLegend surface={surface.data} />}
        </div>
      )}
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={`${idPrefix}-wind`}>Wind</FieldLabel>
          <FieldDescription aria-live="polite">
            {!wind.on ? "Moving with the modelled wind at 10 m"
              : wind.status === "error" ? `The weather model didn't answer: ${wind.error}`
                : wind.data && !wind.data.known ? wind.data.reason
                  : windLine(wind.data) ?? "Reading the weather model"}
          </FieldDescription>
        </FieldContent>
        <Switch id={`${idPrefix}-wind`} checked={wind.on} onCheckedChange={(on) => setWindOn(on)} />
      </Field>
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={`${idPrefix}-rain`}>Rain radar</FieldLabel>
          <FieldDescription aria-live="polite">
            {!rain.on ? "RainViewer's radar, the last two hours"
              : rain.status === "loading" && !rain.frame ? "Reading the radar"
                : rain.status === "error" ? `The radar isn't answering. ${rain.error ?? ""}`.trim()
                  : rain.status === "old" ? "The radar keeps the last two hours only; move the clock nearer now"
                    : rain.frame ? `Radar of ${time(rain.frame.time)}; blue where it rains, nothing where it is dry` : ""}
          </FieldDescription>
        </FieldContent>
        <Switch id={`${idPrefix}-rain`} checked={rain.on} onCheckedChange={(on) => setRainOn(on)} />
      </Field>
    </>
  )
}
