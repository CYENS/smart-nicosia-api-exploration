/* The Layers sheet's Streets group: the rules and safety features of Nicosia's streets from the
   national inventories, each row with its switch, a line on what it shows (and how old the
   inventory is), and its key while it is on. Off by default: the default screen stays calm. */
import type { ComponentType } from "react"

import { useClock } from "@/lib/clock"
import { useMapUi } from "@/lib/map/engine"
import { fmtCount } from "@/lib/readouts"
import { registerLayerGroup, registerSystemLayer } from "@/lib/registries/layers"
import { LIMIT_COLORS, STREET_COLORS, setStreetLayer } from "@/lib/streets/map"
import { inventoryAge, useStreets, type StreetLayerId } from "@/lib/streets/store"

export const STREETS_GROUP = "streets"

registerLayerGroup({ id: STREETS_GROUP, label: "Streets", order: 8 })

const useBase = () => useMapUi((s) => s.basemap)

function Swatch({ color, dashed, faint, round }: { color: string; dashed?: boolean; faint?: boolean; round?: boolean }) {
  if (round) return <span className="size-2.5 rounded-full ring-1 ring-night/60" style={{ background: color }} aria-hidden="true" />
  return (
    <span aria-hidden="true" className="inline-block h-1.5 w-5 rounded-full"
          style={dashed ? { backgroundImage: `repeating-linear-gradient(90deg, ${color} 0 5px, transparent 5px 9px)`, opacity: 0.9 }
            : { background: color, opacity: faint ? 0.45 : 0.85 }} />
  )
}

function Key({ label, items }: { label: string; items: { color: string; text: string; dashed?: boolean; faint?: boolean; round?: boolean }[] }) {
  return (
    <ul aria-label={label} className="flex flex-wrap gap-x-3.5 gap-y-1.5 text-[12.5px] text-muted-foreground">
      {items.map((i) => (
        <li key={i.text} className="flex items-center gap-1.5"><Swatch {...i} />{i.text}</li>
      ))}
    </ul>
  )
}

function useLine(id: StreetLayerId, idleText: string, ready: (by: Record<string, number>, count: number) => string) {
  const one = useStreets((s) => s[id])
  const past = useClock((s) => s.at !== null)
  if (one.status === "loading") return "Reading the national inventory"
  if (one.status === "error") {
    const why = /couldn't/i.test(one.error ?? "") ? one.error : `Couldn't read it: ${one.error}`
    return `${why}. Switch it off and on to try again`
  }
  if (!one.on || !one.meta) return idleText
  const m = one.meta
  if (!m.count) return "None in Greater Nicosia in the national inventory"
  const age = inventoryAge(m.inventory)
  return [ready(m.by ?? {}, m.count), age, past ? "the same at every moment of the clock" : null].filter(Boolean).join("; ")
}

function row(id: string, order: number, label: string, layers: StreetLayerId[], Summary: ComponentType, Legend?: ComponentType) {
  registerSystemLayer({
    id: `streets_${id}`, group: STREETS_GROUP, order, label,
    useOn: () => useStreets((s) => layers.some((l) => s[l].on)),
    setOn: (on) => { for (const l of layers) void setStreetLayer(l, on) },
    Summary,
    // The key shows once the layer has drawn something: not while it loads, fails or is empty.
    Legend: Legend ? function Key() {
      const one = useStreets((s) => s[layers[0]])
      return one.status === "ready" && one.meta?.count ? <Legend /> : null
    } : undefined,
  })
}

row("limits", 0, "Speed limits", ["limits"], function Summary() {
  const meta = useStreets((s) => s.limits.meta)
  return useLine("limits", "Posted limits along the streets, from the signs",
    (by, n) => meta?.status === "loading" ? `${fmtCount(n)} signs; the street network is still loading, the stretches follow`
      : `${fmtCount(n)} signs, carried along ${Math.round(((by.signed ?? 0) + (by.likely ?? 0)) / 1000)} km of street`)
}, function Legend() {
  const base = useBase()
  const c = LIMIT_COLORS[base]
  return (
    <div className="flex flex-col gap-2">
      <Key label="Speed limits" items={[20, 30, 50, 65, 80, 100].map((k) => ({ color: c[k], text: `${k}` }))} />
      <Key label="How sure" items={[
        { color: c[50], text: "Posted" }, { color: c[50], text: "Mapped by OpenStreetMap", faint: true },
        { color: c[50], text: "Changes somewhere here", dashed: true },
      ]} />
    </div>
  )
})

row("zones", 10, "20 and 30 zones", ["zones"], function Summary() {
  return useLine("zones", "Slow zones and their entrances", (_by, n) => `${n} zone${n === 1 ? "" : "s"}, all planned, not yet in force`)
}, function Legend() {
  const base = useBase()
  return <Key label="Slow zones" items={[{ color: STREET_COLORS.zone[base], text: "Zone 30, planned", dashed: true },
                                         { color: STREET_COLORS.zone[base], text: "Entrance", round: true }]} />
})

row("crossings", 20, "Signals and crossings", ["signals", "crossings"], function Summary() {
  const sig = useStreets((s) => s.signals)
  const xing = useStreets((s) => s.crossings)
  const a = useLine("signals", "Where to cross: signalled junctions and marked crossings, from street zoom", (_b, n) => `${fmtCount(n)} signalled junctions`)
  if (!sig.on || !sig.meta || !xing.meta) return a
  return a.replace("signalled junctions", `signalled junctions and ${fmtCount(xing.meta.count)} crossings`)
}, function Legend() {
  const base = useBase()
  return <Key label="Signals and crossings" items={[{ color: STREET_COLORS.signal[base], text: "Traffic signals", round: true },
                                                    { color: STREET_COLORS.crossing[base], text: "Pedestrian crossing", round: true }]} />
})

row("cycleways", 30, "Cycleways", ["cycleways"], function Summary() {
  return useLine("cycleways", "The cycle network and where it breaks",
    (by) => `${by.km ?? "?"} km in ${by.pieces ?? "?"} separate pieces, ${by.gaps ?? 0} gap${by.gaps === 1 ? "" : "s"} under 1.2 km`)
}, function Legend() {
  const base = useBase()
  return <Key label="Cycleways" items={[{ color: STREET_COLORS.cycle[base], text: "Cycleway" },
                                        { color: STREET_COLORS.gap[base], text: "Gap", dashed: true },
                                        { color: STREET_COLORS.gap[base], text: "Loose end", round: true }]} />
})

row("bus_lanes", 40, "Bus lanes", ["bus_lanes"], function Summary() {
  return useLine("bus_lanes", "Lanes for buses, in use and planned", (by) => `${by.active ?? 0} in use, ${by.future ?? 0} planned`)
}, function Legend() {
  const base = useBase()
  return <Key label="Bus lanes" items={[{ color: STREET_COLORS.busLane[base], text: "In use" },
                                        { color: STREET_COLORS.busLane[base], text: "Planned", dashed: true }]} />
})

row("cameras", 50, "Enforcement cameras", ["cameras"], function Summary() {
  return useLine("cameras", "Traffic enforcement cameras at the junctions",
    (by, n) => `${n} cameras${by.rough ? `, ${by.rough} placed only roughly` : ""}`)
}, function Legend() {
  const base = useBase()
  return <Key label="Enforcement cameras" items={[{ color: STREET_COLORS.camera[base], text: "Camera", round: true }]} />
})
