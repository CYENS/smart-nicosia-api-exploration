/* The rows of the Layers sheet's City systems: each system layer's switch and its live summary,
   the readouts the strip across the top used to show (lamps lit, free parking, full bins, air,
   the registers, buses and the slowest road), at the clock's moment. */
import { DotLegend } from "@/components/topics/builtin"
import { RoadLegend } from "@/components/mobility/parts"
import { cityHeights, setCity3d, setNightLights, showRegisters, useMapUi } from "@/lib/map/engine"
import { setBusesOn, setRoadsOn } from "@/lib/mobility/map"
import { useMobility } from "@/lib/mobility/store"
import { AIR_WORDS, airWords, fmtCount, parkingWord, useSky, useStrip } from "@/lib/readouts"
import { registerSystemLayer } from "@/lib/registries/layers"
import { setSystemLayer, useSystemLayers, type SystemLayerId } from "@/lib/system-layers"

const NO_RECORD = "No record for this moment"
const LABELS: Record<string, string> = {
  free: "Free", occupied: "Taken", unknown: "No reading", low: "Low", medium: "Half", high: "High", full: "Full",
  connected: "Counting", disconnected: "Not connected", alarm: "Alarm", ...AIR_WORDS,
}

// A system layer's own legend, from what the server said it draws.
function MetaLegend({ id, label }: { id: SystemLayerId; label: string }) {
  const legend = useSystemLayers((s) => s[id].meta?.legend)
  if (!legend) return null
  return <DotLegend label={label} items={Object.entries(legend).map(([k, c]): [string, string] => [c, LABELS[k] ?? k.replace(/_/g, " ")])} />
}

// While a system layer loads or fails, that is its summary.
function useSystemStatus(id: SystemLayerId): string | null {
  const one = useSystemLayers((s) => s[id])
  if (one.on && one.status === "loading") return "Loading"
  if (one.on && one.status === "error") return `Couldn't load: ${one.error}. Switch it off and on to try again.`
  return null
}

// -- Sensors --------------------------------------------------------------------------------------

registerSystemLayer({
  id: "street_lights", group: "sensors", order: 0, label: "Street lights",
  useOn: () => useMapUi((s) => s.nightLights),
  setOn: setNightLights,
  Summary: function Summary() {
    const { strip, past } = useStrip()
    const sky = useSky()
    const l = strip?.lights
    if (!l) return <>Lit lamps glow after dark</>
    if (l.unknown) return <>{NO_RECORD}</>
    if (!sky.dark && !l.on) return <>Off in daylight. Lit lamps glow after dark</>
    return <>{`${fmtCount(l.on)} of ${fmtCount(l.total)} ${past ? "were lit" : "lit now"}`}</>
  },
})

registerSystemLayer({
  id: "parking_sensor", group: "sensors", order: 10, label: "Parking sensors",
  useOn: () => useSystemLayers((s) => s.parking_sensor.on),
  setOn: (on) => setSystemLayer("parking_sensor", on),
  Summary: function Summary() {
    const { strip, past } = useStrip()
    const status = useSystemStatus("parking_sensor")
    const p = strip?.parking
    if (status) return <>{status}</>
    if (!p) return <>Bays free or taken</>
    if (p.unknown) return <>{NO_RECORD}</>
    return <>{`${parkingWord(p)}: ${fmtCount(p.free)} of ${fmtCount(p.total)} bays ${past ? "were free" : "free"}`}</>
  },
  Legend: () => <MetaLegend id="parking_sensor" label="Parking bays" />,
})

registerSystemLayer({
  id: "waste_bin", group: "sensors", order: 20, label: "Waste bins",
  useOn: () => useSystemLayers((s) => s.waste_bin.on),
  setOn: (on) => setSystemLayer("waste_bin", on),
  Summary: function Summary() {
    const { strip, past } = useStrip()
    const status = useSystemStatus("waste_bin")
    const b = strip?.bins
    if (status) return <>{status}</>
    if (!b) return <>How full each bin is</>
    if (b.unknown) return <>{NO_RECORD}</>
    return <>{b.full ? `${fmtCount(b.full)} of ${fmtCount(b.total)} ${past ? "were full" : "full"}` : `None of ${fmtCount(b.total)} ${past ? "was" : "is"} full`}</>
  },
  Legend: () => <MetaLegend id="waste_bin" label="Waste bins" />,
})

registerSystemLayer({
  id: "air_station", group: "sensors", order: 30, label: "Air and noise",
  useOn: () => useSystemLayers((s) => s.air_station.on),
  setOn: (on) => setSystemLayer("air_station", on),
  Summary: function Summary() {
    const { strip } = useStrip()
    const status = useSystemStatus("air_station")
    const a = strip?.air
    if (status) return <>{status}</>
    if (!a) return <>Air quality and noise at the stations</>
    if (a.unknown) return <>{NO_RECORD}</>
    return <>{airWords(a)} air, index {a.caqi} across {fmtCount(a.stations)} stations</>
  },
  Legend: () => <MetaLegend id="air_station" label="Air quality at the stations" />,
})

registerSystemLayer({
  id: "camera", group: "sensors", order: 40, label: "Traffic cameras",
  useOn: () => useSystemLayers((s) => s.camera.on),
  setOn: (on) => setSystemLayer("camera", on),
  Summary: function Summary() {
    const status = useSystemStatus("camera")
    const meta = useSystemLayers((s) => s.camera.meta)
    if (status) return <>{status}</>
    if (meta) return <>{fmtCount(meta.count)} cameras counting people, cars, buses and bikes</>
    return <>Cameras counting people, cars, buses and bikes</>
  },
  Legend: () => <MetaLegend id="camera" label="Traffic cameras" />,
})

registerSystemLayer({
  id: "buses", group: "sensors", order: 50, label: "Buses",
  useOn: () => useMobility((s) => s.buses.on),
  setOn: setBusesOn,
  Summary: function Summary() {
    const b = useMobility((s) => s.buses)
    const { strip } = useStrip()
    if (b.paused) return <>Live only: they come back when the clock returns to now</>
    if (b.on && b.status === "unavailable") return <>The live bus feed isn't answering. Try again in a minute</>
    if (b.on && (b.status === "loading" || b.status === "idle")) return <>Finding the buses on the road</>
    const running = b.on ? b.count : strip?.buses?.running
    const lines = strip?.buses?.lines
    if (running === undefined) return <>Live from the national feed, coloured by line</>
    return <>{fmtCount(running)} on the road now{lines ? ` on ${fmtCount(lines)} lines` : ""}</>
  },
})

registerSystemLayer({
  id: "roads", group: "sensors", order: 60, label: "Road speeds",
  useOn: () => useMobility((s) => s.roads.on),
  setOn: setRoadsOn,
  Summary: function Summary() {
    const r = useMobility((s) => s.roads)
    const { strip, past } = useStrip()
    if (r.on && (r.status === "loading" || r.status === "idle")) return <>Reading the road detectors</>
    if (r.on && r.status === "unavailable") return <>{past ? "The twin's memory has no road speeds for that moment" : "The national traffic feed isn't answering. Try again in a minute"}</>
    const worst = strip?.slowest_road
    if (worst && !past) return <>Slowest: {worst.name}, {Math.round(worst.speed_kmh)} km/h</>
    if (r.on && r.measured) return <>{fmtCount(r.measured)} of {fmtCount(r.total)} road segments measured</>
    return <>Main roads against their free-flow speed</>
  },
  Legend: () => <RoadLegend />,
})

// -- Buildings ------------------------------------------------------------------------------------

registerSystemLayer({
  id: "registers", group: "buildings", order: 0, label: "Dangerous buildings",
  useOn: () => useMapUi((s) => s.registers.on),
  setOn: (on) => { void showRegisters(on) },
  Summary: function Summary() {
    const { strip } = useStrip()
    const status = useMapUi((s) => s.registers.status)
    if (status) return <>{status}</>
    const n = strip?.registers?.buildings
    return <>{n ? `${fmtCount(n)} buildings on the ΕΟΑ registers of four districts` : "The ΕΟΑ registers of four districts"}</>
  },
  Legend: function Legend() {
    const layer = useMapUi((s) => s.layers.find((l) => l.ref === "eoa_registers"))
    if (!layer) return null
    return <DotLegend label="Dangerous buildings" items={Object.entries(layer.legend).map(([k, c]): [string, string] => [c, k.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase())])} />
  },
})

function HeightRamp() {
  const heights = cityHeights()
  const stops: string[] = []
  for (let i = 0; i < heights.length; i += 2) stops.push(`${heights[i + 1]} ${Math.round((100 * (heights[i] as number)) / 60)}%`)
  return (
    <div className="flex flex-col gap-1" aria-label="Height to roof, from 0 to 60 metres and over">
      <div className="h-2 rounded-full" style={{ background: `linear-gradient(90deg, ${stops.join(", ")})` }} />
      <div className="flex justify-between font-readout text-[12px] text-muted-foreground">
        <span>0 m</span><span>height to roof</span><span>60 m+</span>
      </div>
    </div>
  )
}

registerSystemLayer({
  id: "city3d", group: "buildings", order: 10, label: "Heights and the 3D city",
  useOn: () => useMapUi((s) => s.city.on),
  setOn: setCity3d,
  Summary: function Summary() {
    const status = useMapUi((s) => s.city.status)
    return <>{status || "Every building at its DLS height"}</>
  },
  Legend: HeightRamp,
})

// -- Alerts ---------------------------------------------------------------------------------------

registerSystemLayer({
  id: "alarms", group: "alerts", order: 0, label: "Alarms now",
  useOn: () => useSystemLayers((s) => s.alarms.on),
  setOn: (on) => setSystemLayer("alarms", on),
  Summary: function Summary() {
    const status = useSystemStatus("alarms")
    const meta = useSystemLayers((s) => s.alarms.meta)
    if (status) return <>{status}</>
    if (meta) return <>{meta.count ? `${fmtCount(meta.count)} raised and not yet seen to` : "No alarms raised just now"}</>
    return <>Lost connections, full bins, lamp faults and air warnings</>
  },
})
