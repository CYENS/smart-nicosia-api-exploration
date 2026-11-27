/* "Weather on the map", in the Layers sheet's Map section: the switch, and what the map shows
   with it on (the same word as the now line), at the clock's moment. */
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Switch } from "@/components/ui/switch"
import { useClock } from "@/lib/clock"
import { setWeatherOn, useWeatherMap } from "@/lib/weather/map"

export function WeatherField() {
  const on = useWeatherMap((s) => s.on)
  const word = useWeatherMap((s) => s.word)
  const past = useClock((s) => s.at !== null)
  const said = !on
    ? "Off: the map keeps a clear sky"
    : word
      ? `${word}, as ${past ? "it was at the clock's moment" : "it is outside now"}`
      : "Cloud, rain, mist and dust, as they are outside"
  return (
    <Field orientation="horizontal">
      <FieldContent>
        <FieldLabel htmlFor="ls-weather">Weather on the map</FieldLabel>
        <FieldDescription aria-live="polite">{said}</FieldDescription>
      </FieldContent>
      <Switch id="ls-weather" checked={on} onCheckedChange={(v) => setWeatherOn(v)} />
    </Field>
  )
}
