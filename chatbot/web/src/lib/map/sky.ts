/* The real sky over Nicosia: where the sun and moon are at a given moment, how much of the moon
   is lit, and when it gets dark (civil dusk, the sun 6° below the horizon) or light again. The
   map's base, its 3D lighting, its sky colours and the street lights all follow this. */
import SunCalc from "suncalc"

const LAT = 35.1725
const LON = 33.365
const DEG = 180 / Math.PI

export type Period = "day" | "golden" | "twilight" | "night"

export type Sky = {
  at: Date
  sun: { altitude: number; bearing: number } // degrees; bearing from north, clockwise
  moon: { altitude: number; bearing: number; lit: number; phase: number } // phase 0 new, 0.5 full
  period: Period
  dark: boolean // after civil dusk, before civil dawn
  next: { dark: boolean; at: Date } // the next switch between light and dark
}

// SunCalc measures azimuth from south, positive towards west.
const bearing = (azimuth: number) => (azimuth * DEG + 180 + 360) % 360

function nextSwitch(at: Date, dark: boolean): Date {
  for (let d = 0; d < 3; d++) {
    const day = new Date(at.getTime() + d * 86400e3)
    const t = SunCalc.getTimes(day, LAT, LON)
    const when = dark ? t.dawn : t.dusk
    if (when.getTime() > at.getTime()) return when
  }
  return new Date(at.getTime() + 12 * 3600e3)
}

export function skyAt(at: Date): Sky {
  const s = SunCalc.getPosition(at, LAT, LON)
  const m = SunCalc.getMoonPosition(at, LAT, LON)
  const ill = SunCalc.getMoonIllumination(at)
  const sunAlt = s.altitude * DEG
  const dark = sunAlt < -6
  const period: Period = sunAlt >= 10 ? "day" : sunAlt >= -0.8 ? "golden" : !dark ? "twilight" : "night"
  return {
    at,
    sun: { altitude: sunAlt, bearing: bearing(s.azimuth) },
    moon: { altitude: m.altitude * DEG, bearing: bearing(m.azimuth), lit: ill.fraction, phase: ill.phase },
    period,
    dark,
    next: { dark: !dark, at: nextSwitch(at, dark) },
  }
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

// MapLibre's single light, for the 3D buildings: the sun while it is up (warm when low), the moon
// at night (brighter the fuller it is), always from where it really is in the sky.
export function lightFor(sky: Sky) {
  const { sun, moon } = sky
  if (sun.altitude > -3) {
    const alt = Math.max(sun.altitude, 0.5)
    const color = alt < 3 ? "#ff9b5e" : alt < 9 ? "#ffbf82" : alt < 20 ? "#ffe1b8" : "#fff8ee"
    return {
      anchor: "map" as const,
      position: [1.4, sun.bearing, clamp(90 - alt, 4, 87)] as [number, number, number],
      color,
      intensity: clamp(0.28 + alt / 120, 0.3, 0.55),
    }
  }
  const up = moon.altitude > 0
  return {
    anchor: "map" as const,
    position: [1.4, up ? moon.bearing : 200, up ? clamp(90 - moon.altitude, 8, 86) : 70] as [number, number, number],
    color: "#a9bdf0",
    intensity: up ? 0.16 + 0.26 * moon.lit : 0.13,
  }
}

// The sky and haze that show when the map is tilted towards the horizon.
const SKIES: Record<Period, { sky: string; horizon: string; fog: string }> = {
  day: { sky: "#6ea4df", horizon: "#d6e4f2", fog: "#e7edf3" },
  golden: { sky: "#5370b0", horizon: "#ffb27c", fog: "#efc7a6" },
  twilight: { sky: "#222c58", horizon: "#b8707a", fog: "#33304c" },
  night: { sky: "#050b16", horizon: "#1a2740", fog: "#0e1828" },
}

export function skyFor(sky: Sky) {
  const c = SKIES[sky.period]
  return {
    "sky-color": c.sky,
    "horizon-color": c.horizon,
    "fog-color": c.fog,
    "sky-horizon-blend": 0.6,
    "horizon-fog-blend": 0.7,
    "fog-ground-blend": sky.period === "day" ? 0.85 : 0.6,
    "atmosphere-blend": 0,
  }
}

export function moonName(phase: number) {
  if (phase < 0.03 || phase > 0.97) return "New moon"
  if (phase < 0.22) return "Waxing crescent"
  if (phase < 0.28) return "First quarter"
  if (phase < 0.47) return "Waxing gibbous"
  if (phase < 0.53) return "Full moon"
  if (phase < 0.72) return "Waning gibbous"
  if (phase < 0.78) return "Last quarter"
  return "Waning crescent"
}

// Time-lapse frames carry Nicosia wall-clock times ("2026-10-20T14:00"); this is the instant.
export function nicosiaWallToDate(wall: string): Date {
  const [d, t = "00:00"] = wall.split("T")
  const [y, mo, day] = d.split("-").map(Number)
  const [h, mi] = t.split(":").map(Number)
  const guess = Date.UTC(y, mo - 1, day, h, mi)
  const name = new Date(guess).toLocaleString("en-US", { timeZone: "Asia/Nicosia", timeZoneName: "shortOffset" })
  const m = /GMT([+-]\d+)(?::(\d+))?/.exec(name)
  const offsetMin = m ? Number(m[1]) * 60 + Math.sign(Number(m[1])) * Number(m[2] ?? 0) : 120
  return new Date(guess - offsetMin * 60e3)
}
