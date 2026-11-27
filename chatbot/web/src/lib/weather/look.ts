/* The weather, turned into how the map looks. Pure functions, so they can be tested on their own
   (tests/weather-look.test.ts runs them under Node): the weather the server sends for a moment
   becomes a few amounts (how much cloud, rain, fog, dust, how wet the streets are), and those,
   with the real sky of lib/map/sky.ts, become the light on the 3D buildings, the sky in tilted
   views, the veil, haze and cloud shadows on the ground, the rain and the lamps' halo.

   The weather is laid over the sun and moon, never instead of them: each function takes what the
   sky alone gives and leans it towards the weather. A clear sky changes nothing: every function
   gives back exactly what it was given, so a clear day looks as the map always has. */
import type { Period, Sky } from "../map/sky.ts"

// The weather of a moment as /api/overview sends it (chatbot/context.py `map_weather`). Every
// reading can be missing: then it adds nothing.
export type Weather = {
  temp_c?: number; sky?: string; daylight?: boolean
  cloud_pct?: number | null; cloud_low_pct?: number | null; cloud_mid_pct?: number | null; cloud_high_pct?: number | null
  visibility_m?: number | null; rain_mm?: number | null; snow_cm?: number | null; wet_mm?: number | null
  wind_kmh?: number | null; wind_from_deg?: number | null; code?: number | null
  dust_ugm3?: number | null; aod?: number | null
}

// The weather in amounts from 0 to 1, the form it is eased in when it changes.
export type Conditions = {
  cloud: number // how much of the sky is cloud, as it looks: the sky's greyness
  shade: number // how much of the sun (or the moon) the clouds hold back
  rain: number // how hard it rains (or snows), from a drizzle to a downpour
  snow: number // 1 when what falls is snow
  fog: number // how short the view is: mist, then fog
  dust: number // Saharan dust in the air
  wet: number // how wet the streets are, still after the rain stops
  windKmh: number
  windTo: number // the bearing the wind blows towards, degrees from north
}

export const CLEAR: Conditions = { cloud: 0, shade: 0, rain: 0, snow: 0, fog: 0, dust: 0, wet: 0, windKmh: 0, windTo: 0 }

export const clamp01 = (v: number) => Math.min(1, Math.max(0, v))
const lerp = (a: number, b: number, t: number) => a + (b - a) * t
export function smoothstep(e0: number, e1: number, v: number) {
  const t = clamp01((v - e0) / (e1 - e0))
  return t * t * (3 - 2 * t)
}
const num = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v) ? v : null)

// How hard it rains, on a log scale: 0.1 mm an hour is a drizzle (0.09), 1 mm steady rain (0.43),
// 5 mm heavy (0.82), 10 mm and more a downpour (1).
export function rainAmount(mmPerHour: number) {
  if (mmPerHour < 0.05) return 0
  return clamp01(Math.log10(1 + mmPerHour * 4) / Math.log10(41))
}

// How short the view is, from the visibility in metres: 20 km and more is clear air (0), 5 km a
// light mist (0.28), 1 km fog (0.61), 150 m thick fog (1).
export function fogAmount(visibilityM: number) {
  return clamp01((Math.log10(20000) - Math.log10(Math.max(visibilityM, 1))) / (Math.log10(20000) - Math.log10(150)))
}

// Saharan dust, from the dust near the ground (µg/m³) and the haze it makes (aerosol optical
// depth): 40 µg/m³ starts to show, 340 is a thick yellow day. Optical depth counts only when
// there is dust in the air, so a smoky or humid haze doesn't turn the sky yellow.
export function dustAmount(dust: number | null, aod: number | null) {
  const d = dust ?? 0
  const byDust = (d - 40) / 300
  const byAod = d >= 25 && aod !== null ? (aod - 0.35) / 0.8 : 0
  return clamp01(Math.max(byDust, byAod))
}

export function conditionsOf(w: Weather | null | undefined): Conditions {
  if (!w) return CLEAR
  const total = num(w.cloud_pct)
  const low = num(w.cloud_low_pct)
  const mid = num(w.cloud_mid_pct)
  const high = num(w.cloud_high_pct)
  const rainMm = Math.max(num(w.rain_mm) ?? 0, 0)
  const snowCm = Math.max(num(w.snow_cm) ?? 0, 0)
  const rain = rainAmount(rainMm + snowCm * 7) // a centimetre of snow is about 0.7 mm of water an hour, shown a little heavier
  // Low cloud hides the sun; thin high cloud mostly lets it through.
  let shade = low !== null || mid !== null || high !== null
    ? 1 - (1 - (low ?? 0) / 100) * (1 - 0.8 * (mid ?? 0) / 100) * (1 - 0.3 * (high ?? 0) / 100)
    : (total ?? 0) / 100 * 0.75
  if (rain > 0) shade = Math.max(shade, 0.75 + 0.25 * rain)
  const fog = Math.max(num(w.visibility_m) !== null ? fogAmount(num(w.visibility_m)!) : 0, w.code === 45 || w.code === 48 ? 0.6 : 0)
  const wetMm = Math.max(num(w.wet_mm) ?? 0, rainMm)
  return {
    cloud: clamp01(Math.max(shade, ((total ?? 0) / 100) * 0.6)),
    shade: clamp01(shade),
    rain,
    snow: snowCm > 0 && snowCm * 7 >= rainMm ? 1 : 0,
    fog,
    dust: dustAmount(num(w.dust_ugm3), num(w.aod)),
    wet: clamp01(Math.max(wetMm / 2, rain > 0 ? 0.5 + rain / 2 : 0)),
    windKmh: Math.max(num(w.wind_kmh) ?? 0, 0),
    windTo: ((num(w.wind_from_deg) ?? 0) + 180) % 360,
  }
}

// Weather is eased from one moment's to the next. The wind turns the short way round.
export function mixConditions(a: Conditions, b: Conditions, t: number): Conditions {
  const turn = ((b.windTo - a.windTo + 540) % 360) - 180
  return {
    cloud: lerp(a.cloud, b.cloud, t), shade: lerp(a.shade, b.shade, t), rain: lerp(a.rain, b.rain, t),
    snow: lerp(a.snow, b.snow, t), fog: lerp(a.fog, b.fog, t), dust: lerp(a.dust, b.dust, t), wet: lerp(a.wet, b.wet, t),
    windKmh: lerp(a.windKmh, b.windKmh, t), windTo: (a.windTo + turn * t + 360) % 360,
  }
}

// Below these, the sky reads as clear and the map stays exactly as it is.
const shadeOf = (c: Conditions) => smoothstep(0.08, 0.9, c.shade)
const greyOf = (c: Conditions) => smoothstep(0.1, 0.95, c.cloud)

export function isClear(c: Conditions) {
  return shadeOf(c) === 0 && greyOf(c) === 0 && c.rain === 0 && c.fog === 0 && c.dust === 0 && c.wet === 0
}

// -- colour ---------------------------------------------------------------------------------------

export type RGB = [number, number, number] // 0 to 1

export function rgb(hex: string): RGB {
  const h = hex.replace("#", "")
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16)
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255]
}

export function hex(c: RGB) {
  return `#${c.map((v) => Math.round(clamp01(v) * 255).toString(16).padStart(2, "0")).join("")}`
}

export function mixRgb(a: RGB, b: RGB, t: number): RGB {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)]
}

export function mixHex(a: string, b: string, t: number) {
  return t <= 0 ? a : hex(mixRgb(rgb(a), rgb(b), clamp01(t)))
}

// -- the light on the 3D buildings ------------------------------------------------------------------

export type Light = {
  anchor: "map"
  position: [number, number, number] // radial, azimuth, polar (degrees from straight overhead)
  color: string
  intensity: number // MapLibre: higher is more contrast between lit and shaded faces
}

// Cloud spreads the light: it comes from the whole sky rather than from the sun, so the faces of
// the buildings differ less (lower intensity), the light comes more from overhead, and it is
// cooler and dimmer (its colour multiplies the buildings'). Rain darkens it further; dust warms
// and dims it. At night, cloud hides the moon: its blue light goes flat and neutral. The night
// light stays bright enough that the 3D city's height colours keep reading.
export function weatherLight(base: Light, c: Conditions, sky: Sky): Light {
  if (isClear(c)) return base
  const s = Math.max(shadeOf(c), c.fog * 0.8)
  const day = sky.sun.altitude > -3
  if (day) {
    const overcast = mixHex("#d5dbe3", "#b7c0cb", c.rain)
    let color = mixHex(base.color, overcast, s)
    color = mixHex(color, "#e2c58f", c.dust * 0.55)
    return {
      ...base,
      position: [base.position[0], base.position[1], lerp(base.position[2], 30, s * 0.7)],
      color,
      intensity: base.intensity * (1 - 0.5 * s) * (1 - 0.25 * c.dust),
    }
  }
  let color = mixHex(base.color, "#b7bac6", s)
  color = mixHex(color, "#c9b89c", c.dust * 0.4)
  return {
    ...base,
    position: [base.position[0], base.position[1], lerp(base.position[2], 40, s * 0.6)],
    color,
    intensity: lerp(base.intensity, 0.13, s),
  }
}

// -- the sky in tilted views --------------------------------------------------------------------

export type SkySpec = {
  "sky-color": string; "horizon-color": string; "fog-color": string
  "sky-horizon-blend": number; "horizon-fog-blend": number; "fog-ground-blend": number; "atmosphere-blend": number
}
type SkyColours = { sky: string; horizon: string; fog: string }

// What the sky turns to under each weather, by the time of day. Under cloud at night the sky
// isn't black but faintly lit from below, the city's light on the cloud base.
const OVERCAST: Record<Period, SkyColours> = {
  day: { sky: "#9ba6b2", horizon: "#cdd3da", fog: "#d9dee3" },
  golden: { sky: "#687287", horizon: "#b49c90", fog: "#bcb0a8" },
  twilight: { sky: "#272c3a", horizon: "#5e5160", fog: "#2e2e3c" },
  night: { sky: "#11151d", horizon: "#33302f", fog: "#161b26" },
}
const RAINY: Record<Period, SkyColours> = {
  day: { sky: "#717c89", horizon: "#9ca5af", fog: "#b0b7bf" },
  golden: { sky: "#4f5666", horizon: "#857a78", fog: "#8f8a88" },
  twilight: { sky: "#1d212c", horizon: "#3f3a44", fog: "#262833" },
  night: { sky: "#0d1118", horizon: "#2a2827", fog: "#121720" },
}
const FOGGY: Record<Period, SkyColours> = {
  day: { sky: "#c3c9cf", horizon: "#dfe2e5", fog: "#e2e5e8" },
  golden: { sky: "#9a9898", horizon: "#cdbfb2", fog: "#cfc5bc" },
  twilight: { sky: "#3a3b45", horizon: "#6e6670", fog: "#4a4851" },
  night: { sky: "#1c1f27", horizon: "#3b3836", fog: "#2f3138" }, // a city's fog glows faintly with its lights
}
// Dust glows at the horizon: warm and pale by day, amber at dusk, brown at night.
const DUSTY: Record<Period, SkyColours> = {
  day: { sky: "#c2ad85", horizon: "#e8d1a0", fog: "#dcc69a" },
  golden: { sky: "#8a6a4e", horizon: "#e0a466", fog: "#c99a6c" },
  twilight: { sky: "#3a2c2a", horizon: "#8a5a44", fog: "#3e302c" },
  night: { sky: "#16120f", horizon: "#3b2c22", fog: "#1c1814" },
}

function lean(c: SkyColours, to: SkyColours, t: number): SkyColours {
  return { sky: mixHex(c.sky, to.sky, t), horizon: mixHex(c.horizon, to.horizon, t), fog: mixHex(c.fog, to.fog, t) }
}

export function weatherSky(base: SkySpec, c: Conditions, sky: Sky): SkySpec {
  if (isClear(c)) return base
  const p = sky.period
  let col: SkyColours = { sky: base["sky-color"], horizon: base["horizon-color"], fog: base["fog-color"] }
  col = lean(col, OVERCAST[p], greyOf(c))
  col = lean(col, RAINY[p], c.rain)
  col = lean(col, FOGGY[p], c.fog * 0.85)
  col = lean(col, DUSTY[p], c.dust * 0.9)
  const soft = Math.max(greyOf(c), c.fog, c.dust)
  return {
    ...base,
    "sky-color": col.sky,
    "horizon-color": col.horizon,
    "fog-color": col.fog,
    "sky-horizon-blend": lerp(base["sky-horizon-blend"], 0.92, soft), // a softer, lower contrast sky
    "horizon-fog-blend": lerp(base["horizon-fog-blend"], 0.95, Math.max(c.fog, c.dust)),
    "fog-ground-blend": lerp(base["fog-ground-blend"], 0.25, c.fog),
  }
}

// -- the ground: veil, cloud shadows, haze, and the clouds in a tilted view's sky ------------------

export type Ground = {
  on: boolean // anything to draw at all
  dim: number // the ground darkens by this share (rain)
  veil: RGB; veilA: number // a flat wash of colour over the ground: lower contrast, less saturation
  shadow: number // how much a cloud's shadow darkens the ground (0: no shadows to see)
  cover: number // the share of the ground under cloud shadow
  shadowTint: RGB // shade is cooler than sunlight
  haze: RGB; hazeK: number; hazeMax: number // haze into the distance, seen from above (per view)
  extinction: number // the same haze per metre, for a camera at eye level (3.912 / visibility)
  mist: number // how patchy the haze is (drifting banks of mist)
  clouds: number // clouds seen in the sky of a tilted view (their opacity)
  cloudCover: number
  cloudLit: RGB; cloudBase: RGB
}

const NO_GROUND: Ground = {
  on: false, dim: 0, veil: [0, 0, 0], veilA: 0, shadow: 0, cover: 0, shadowTint: [0, 0, 0],
  haze: [0, 0, 0], hazeK: 0, hazeMax: 0, extinction: 0, mist: 0, clouds: 0, cloudCover: 0, cloudLit: [1, 1, 1], cloudBase: [0, 0, 0],
}

// The sun's height as a share of a fully lit day: 0 below the horizon, 1 from 15° up.
const sunUp = (sky: Sky) => smoothstep(2, 15, sky.sun.altitude)

export function groundFor(c: Conditions, sky: Sky, skySpec: SkySpec): Ground {
  if (isClear(c)) return NO_GROUND
  const night = sky.period === "night"
  const day = sky.period === "day" || sky.period === "golden"
  const up = sunUp(sky)
  const s = shadeOf(c)

  // Veil: by day under cloud, a cool grey wash, darker in rain; dust a warm one. At night only
  // dust and fog, faintly: the night map keeps its contrast for the data on it.
  const washes: [RGB, number][] = night || !day
    ? [[rgb("#4a3a2a"), c.dust * 0.08], [rgb(skySpec["fog-color"]), c.fog * 0.1]]
    : [[rgb("#c6ced8"), greyOf(c) * 0.06 * up], [rgb("#8f99a5"), c.rain * 0.1], [rgb("#d2b57e"), c.dust * 0.17], [rgb(skySpec["fog-color"]), c.fog * 0.14]]
  let veilA = 0
  let veil: RGB = [0, 0, 0]
  for (const [col, a] of washes) {
    if (a <= 0) continue
    veil = mixRgb(veil, col, a / (veilA + a))
    veilA += a * (1 - veilA)
  }

  // Cloud shadows: only with the sun up, and only while the clouds come and go; under a full
  // deck the whole city is in shade, an even dimming. Fog hides them.
  const shadowCover = s
  const shadow = 0.15 * up * smoothstep(0.08, 0.3, shadowCover) * (1 - c.fog * 0.8)

  // Haze: the air's own colour, the same as the sky's at the horizon, so ground and sky meet.
  const hazeK = 3.4 * Math.pow(c.fog, 1.25) + 1.3 * c.dust
  const hazeMax = (night ? 0.6 : 0.82) * smoothstep(0, 0.15, Math.max(c.fog, c.dust))
  // At eye level the haze is the real one: Koschmieder's 3.912 / visibility, the visibility back
  // from the fog amount, and dust taking the view down to some 6 km at its thickest.
  const visibility = 10 ** (Math.log10(20000) - c.fog * (Math.log10(20000) - Math.log10(150)))
  const extinction = 3.912 / visibility + (c.dust * 3.912) / 6000

  // Clouds in a tilted view's sky: lit white-grey by day, glowing warm at sunset, faintly lit
  // by the city at night.
  const lit = { day: "#f1f3f5", golden: "#efb28a", twilight: "#8a6470", night: "#2b2c33" }[sky.period]
  const base = { day: "#9aa3ae", golden: "#6d6170", twilight: "#2c2a38", night: "#14171e" }[sky.period]
  const cloudLit = mixRgb(mixRgb(rgb(lit), rgb(base), c.rain * 0.7), rgb("#d9bf8c"), day ? c.dust * 0.5 : 0)
  return {
    on: true,
    dim: day ? c.rain * 0.08 + c.wet * 0.02 : 0,
    veil, veilA,
    shadow,
    cover: shadowCover,
    shadowTint: rgb("#6d7c93"),
    haze: rgb(skySpec["fog-color"]),
    hazeK,
    hazeMax,
    extinction,
    mist: smoothstep(0.15, 0.6, c.fog) * 0.45,
    clouds: smoothstep(0.08, 0.35, c.cloud) * (1 - c.fog * 0.7) * 0.9,
    cloudCover: c.cloud,
    cloudLit,
    cloudBase: mixRgb(rgb(base), rgb("#000000"), c.rain * 0.3),
  }
}

// -- rain ---------------------------------------------------------------------------------------------

export type Rain = {
  share: number // of the most drops there can be (0: no rain)
  fall: number // how fast the drops fall, as a share of rain's own speed (snow drifts)
  streak: number // how long a streak is, from the drop's speed and the exposure
  alpha: number
  color: RGB
  windTo: number // the bearing the wind blows towards
  slant: number // how far the wind leans the rain, 0 to 1
}

export function rainFor(c: Conditions, sky: Sky): Rain | null {
  if (c.rain <= 0.01) return null
  const lit = sky.period === "day" || sky.period === "golden"
  const snow = c.snow > 0.5
  return {
    share: lerp(0.12, 1, c.rain),
    fall: snow ? 0.16 : 1,
    streak: snow ? 0.15 : lerp(0.7, 1.15, c.rain),
    alpha: snow ? 0.75 : lit ? 0.2 + 0.16 * c.rain : 0.13 + 0.1 * c.rain,
    color: rgb(snow ? "#f4f7fb" : lit ? "#e3eaf2" : "#9eadc4"),
    windTo: c.windTo,
    slant: clamp01(c.windKmh / 45) * 0.8,
  }
}

// -- the street lights --------------------------------------------------------------------------

// In mist, fog and rain the light of the lamps spreads into the wet air (a wider, softer halo),
// and wet streets mirror it (stronger pools, a brighter wash along the roads).
export function lampsFor(c: Conditions) {
  const air = Math.max(c.fog, c.rain * 0.7, c.dust * 0.5)
  return {
    halo: 1 + 0.45 * air, // the pools' radius
    glow: 1 + 0.3 * c.wet + 0.2 * c.fog, // the pools' strength
    streets: 1 + 1.2 * c.wet, // the wash along the streets
    bloom: 1 + 0.8 * air, // the bulbs' blur
  }
}

// -- sunlight ----------------------------------------------------------------------------------------

// How much direct sun reaches the ground, 0 to 1: 1 with the sun up in a clear sky, 0 at night
// or under thick cloud, fog or rain. For shadows that should soften under cloud.
export function sunlightFor(c: Conditions, sky: Sky) {
  return clamp01(smoothstep(-0.5, 6, sky.sun.altitude) * clearSkyFor(c))
}

// The share of the sun's light that comes straight through the cloud, fog and dust, whatever
// its height: 1 under a clear sky. How sharp the shadows are.
export function clearSkyFor(c: Conditions) {
  return clamp01((1 - c.shade) * (1 - 0.85 * c.fog) * (1 - 0.5 * c.dust))
}

// -- words ---------------------------------------------------------------------------------------------

// The weather in a word or two, from what the map draws, so the now line says what the map shows.
export function weatherWord(w: Weather | null | undefined): string | null {
  if (!w) return null
  const code = num(w.code)
  const rainMm = num(w.rain_mm) ?? 0
  const c = conditionsOf(w)
  if (code !== null && code >= 95) return "Thunderstorm"
  if (c.snow && c.rain > 0) return "Snow"
  if (rainMm >= 4) return "Heavy rain"
  if (rainMm >= 0.5) return code !== null && code >= 80 && code <= 82 ? "Showers" : "Rain"
  if (rainMm >= 0.05) return code !== null && code >= 51 && code <= 57 ? "Drizzle" : "Light rain"
  if (c.dust >= 0.3) return "Dusty"
  const vis = num(w.visibility_m)
  if ((vis !== null && vis < 1000) || code === 45 || code === 48) return "Fog"
  if (vis !== null && vis < 5000) return c.dust > 0.1 ? "Dusty" : "Mist"
  if (c.dust > 0.1) return "Hazy"
  const cloud = num(w.cloud_pct)
  if (cloud === null) return w.sky ? w.sky.replace(/^./, (s) => s.toUpperCase()) : null
  if (cloud >= 88) return "Overcast"
  if (cloud >= 60) return "Cloudy"
  if (cloud >= 25) return "Partly cloudy"
  if (cloud >= 10) return "Mainly clear"
  return "Clear"
}
