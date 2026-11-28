/* Semantic color ramps for Summer Morning Ground Surface Heat, Modelled 2022 Population,
   15-Minute Walkability & Step-Free Access, and CyNAP Road Gradients across night & day basemaps. */

export type HeatBandDef = { to: number | null; label: string; short: string }

export const HEAT_BANDS: HeatBandDef[] = [
  { to: 36, label: "Cooler (<36 °C)", short: "<36" },
  { to: 41, label: "Warm (36 to 41 °C)", short: "36-41" },
  { to: 46, label: "Hot (41 to 46 °C)", short: "41-46" },
  { to: 51, label: "Very hot (46 to 51 °C)", short: "46-51" },
  { to: null, label: "Extreme (51+ °C)", short: "51+ °C" },
]

export const HEAT_COLORS = {
  night: ["#3b82f6", "#22c55e", "#eab308", "#f97316", "#ff5a4e"],
  day: ["#2563eb", "#16a34a", "#ca8a04", "#ea580c", "#dc2626"],
} as const

export const PEOPLE_LEGEND = [
  { label: "<500", color: "#1e3a5f", hint: "Under 500 modelled residents" },
  { label: "500 to 2k", color: "#2563eb", hint: "500 to 2,000" },
  { label: "2k to 5k", color: "#38bdf8", hint: "2,000 to 5,000" },
  { label: "5,000+", color: "#ffae45", hint: "5,000 or more" },
] as const

export const ACCESS_LEGEND = [
  { label: "<25", color: "#f97316", hint: "Low (<25 / 100)" },
  { label: "25 to 49", color: "#eab308", hint: "Moderate (25 to 49)" },
  { label: "50 to 74", color: "#84cc16", hint: "Good (50 to 74)" },
  { label: "75+", color: "#22c55e", hint: "High (75+)" },
] as const

export const SLOPE_LEGEND = [
  { key: "gentle", label: "<=5% gentle", color: "#22c55e" },
  { key: "moderate", label: "5 to 8% climb", color: "#f5a524" },
  { key: "steep", label: ">8% steep barrier", color: "#ff5a4e" },
] as const

export function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

export function heatBandIndex(c: number): number {
  for (let i = 0; i < HEAT_BANDS.length; i++) {
    const to = HEAT_BANDS[i].to
    if (to === null || c < to) return i
  }
  return HEAT_BANDS.length - 1
}

export async function inflateZlibUint16Grid(b64: string): Promise<Uint16Array> {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  const ds = new DecompressionStream("deflate")
  const stream = new Blob([bytes]).stream().pipeThrough(ds)
  const ab = await new Response(stream).arrayBuffer()
  return new Uint16Array(ab)
}
