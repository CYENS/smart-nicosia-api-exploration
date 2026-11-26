/* The environment's colours, one ramp per job and per base map, validated with the dataviz
   skill's checker (one-hue ordinal ramps: monotone lightness, steps at least 0.06 apart, the
   step nearest the map at 2:1 or more; on the night map #0e1828 and the day map #f8f4f0).
   On the night map more reads brighter; on the day map, darker. Temperature is diverging: blue
   for cold, red for hot, grey for mild. Shade and sun are blue and amber, which stay apart for
   every kind of colour vision. */
import type { Basemap } from "@/lib/map/basemap"

export type Metric = "pm2_5" | "pm10" | "no2" | "o3" | "caqi" | "noise_db" | "temperature_c"

export const SUN_HOURS: Record<Basemap, string[]> = {
  night: ["#714909", "#8f5f15", "#ae7521", "#ce8d2d", "#efa539", "#ffbe46"],
  day: ["#ec9d53", "#d5873a", "#c0721c", "#aa5d00", "#954800", "#803400"],
}
export const SUN_HOUR_STEPS = [2, 4, 6, 8, 10] // upper bounds of the first five classes
export const SUN_HOUR_LABELS = ["Under 2 h", "2 to 4 h", "4 to 6 h", "6 to 8 h", "8 to 10 h", "10 h or more"]

export const SHADE = { shade: "#3987e5", sun: "#c98500" }

const AIR: Record<Basemap, string[]> = {
  night: ["#7e3261", "#974677", "#af598d", "#c96da3", "#e382bb", "#fd97d2"],
  day: ["#d98bb8", "#c472a1", "#ae598c", "#994177", "#832762", "#6e034f"],
}
const NOISE: Record<Basemap, string[]> = {
  night: ["#006363", "#0e8484", "#27a7a8", "#3dcccc", "#53f2f2"],
  day: ["#56b6bb", "#349ba1", "#008287", "#00696f", "#005157"],
}
const TEMPERATURE: Record<Basemap, string[]> = {
  night: ["#83c1ff", "#4c88d3", "#5b6577", "#c9593d", "#f07b5e", "#ff9e80"],
  day: ["#0b4c92", "#4c88d3", "#c9c6bd", "#f37e61", "#c9593d", "#9d3013"],
}

export function bandColors(metric: string, base: Basemap): string[] {
  if (metric === "noise_db") return NOISE[base]
  if (metric === "temperature_c") return TEMPERATURE[base]
  return AIR[base]
}

export function sunHoursColor(hours: number, base: Basemap) {
  const k = SUN_HOUR_STEPS.findIndex((top) => hours < top)
  return SUN_HOURS[base][k < 0 ? SUN_HOURS[base].length - 1 : k]
}

export function hexRgb(h: string): [number, number, number] {
  const n = parseInt(h.slice(1), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

// The wind's particles: pale ink on the night map, deep ink on the day map.
export const WIND_INK: Record<Basemap, string> = { night: "rgba(214, 228, 246, 0.85)", day: "rgba(22, 40, 68, 0.78)" }
// Building shadows: a cool, deep blue rather than black, so the ground still reads through.
export const SHADOW_INK: Record<Basemap, string> = { night: "#02060e", day: "#16233d" }
