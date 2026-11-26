/* The Buses and Road speeds switches, with what each is showing right now, in the Mobility
   workspace (the Layers sheet has its own rows for them, components/layers/system-rows.tsx). */
import { RoadLegend } from "@/components/mobility/parts"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Switch } from "@/components/ui/switch"
import { setBusesOn, setRoadsOn } from "@/lib/mobility/map"
import { useMobility } from "@/lib/mobility/store"

function busLine(b: ReturnType<typeof useMobility.getState>["buses"]) {
  if (!b.on) return "Live from the national feed, gliding along their routes"
  if (b.paused) return "Live only: they come back when the clock returns to now"
  switch (b.status) {
    case "loading": case "idle": return "Finding the buses on the road"
    case "unavailable": return "The live bus feed isn't answering. Try again in a minute; stop timetables still work."
    case "stale": return `${b.count} on the road, as of a few minutes ago (the feed is slow to answer)`
    default: return b.count ? `${b.count} on the road now, coloured by line` : "No buses on the road just now"
  }
}

function roadLine(r: ReturnType<typeof useMobility.getState>["roads"]) {
  if (!r.on) return "Main roads against their free-flow speed"
  switch (r.status) {
    case "loading": case "idle": return "Reading the road detectors"
    case "unavailable": return r.at ? "The twin's memory has no road speeds for that moment" : "The national traffic feed isn't answering. Try again in a minute."
    case "recorded": return r.measured ? `${r.measured} of ${r.total} road segments, as the twin recorded them then` : "No road speeds recorded around that moment"
    case "no_readings": return "No road has a reading in the last 30 minutes"
    default: return `${r.measured} of ${r.total} road segments measured, updated every 5 minutes`
  }
}

export function MobilitySwitches({ idPrefix = "tg" }: { idPrefix?: string }) {
  const buses = useMobility((s) => s.buses)
  const roads = useMobility((s) => s.roads)
  const estimated = roads.freeFlow.some((f) => f !== "measured")
  return (
    <>
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={`${idPrefix}-buses`}>Buses</FieldLabel>
          <FieldDescription aria-live="polite">{busLine(buses)}</FieldDescription>
        </FieldContent>
        <Switch id={`${idPrefix}-buses`} checked={buses.on} onCheckedChange={(on) => setBusesOn(on)} />
      </Field>
      <Field orientation="horizontal">
        <FieldContent>
          <FieldLabel htmlFor={`${idPrefix}-roads`}>Road speeds</FieldLabel>
          <FieldDescription aria-live="polite">{roadLine(roads)}</FieldDescription>
        </FieldContent>
        <Switch id={`${idPrefix}-roads`} checked={roads.on} onCheckedChange={(on) => setRoadsOn(on)} />
      </Field>
      {roads.on && roads.measured > 0 && (
        <div className="flex flex-col gap-2">
          <RoadLegend />
          <p className="text-[12px] leading-snug text-muted-foreground">
            Against each road's own free flow{estimated ? ", estimated from the road network until the twin has a day of readings" : ""}.
            Each direction runs on its own side.
          </p>
        </div>
      )}
    </>
  )
}
