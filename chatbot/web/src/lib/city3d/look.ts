/* How the 3D city is coloured. Two looks:

   - the twin's (the default): buildings coloured by height, dark slate lightening as they climb
     by night, pale to deep blue by day, so the skyline reads at a glance and the assistant's
     layers stand out against it;
   - as it looks (with the aerial photo on the ground, or looking out from a place): plaster and
     stone walls and tiled roofs by day, the same dark slate after dark.

   Roofs follow their walls in the twin's look (a shade darker, so their slopes read), and are
   tiles of a few weathered reds as it looks. */

export const CITY_HEIGHTS_NIGHT = [0, "#1a2840", 6, "#203250", 12, "#2a4064", 20, "#37547f", 35, "#5577a6", 60, "#93b3dc"]
export const CITY_HEIGHTS_DAY = [0, "#e5e7eb", 6, "#cbd5e1", 12, "#93c5fd", 20, "#3b82f6", 35, "#1d4ed8", 60, "#312e81"]
// Walls as they look by day: lime plaster and stone, a little greyer as buildings get taller.
export const STONE = [0, "#e9e4da", 20, "#ddd6c8", 60, "#cbc2b0"]
// Clay tiles, weathered differently from roof to roof.
const TILES = ["#b0603f", "#a3553a", "#bc6c49", "#96513d", "#b47658", "#a5664c", "#c07a58"]
const NIGHT_ROOF = "#3a2b33"

export type Look = { night: boolean; real: boolean }

export type Ramp = (string | number)[]

export function rampFor(look: Look): Ramp {
  if (look.night) return CITY_HEIGHTS_NIGHT
  return look.real ? STONE : CITY_HEIGHTS_DAY
}

export function hexRgb(hex: string): [number, number, number] {
  const v = parseInt(hex.slice(1), 16)
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255]
}

const cache = new Map<string, [number, number, number]>()
function parsed(hex: string) {
  let c = cache.get(hex)
  if (!c) { c = hexRgb(hex); cache.set(hex, c) }
  return c
}

// MapLibre's linear interpolation of a colour ramp, in RGB.
export function rampAt(ramp: Ramp, h: number): [number, number, number] {
  if (h <= (ramp[0] as number)) return parsed(ramp[1] as string)
  for (let i = 2; i < ramp.length; i += 2) {
    const h1 = ramp[i] as number
    if (h <= h1) {
      const h0 = ramp[i - 2] as number, t = (h - h0) / (h1 - h0)
      const a = parsed(ramp[i - 1] as string), b = parsed(ramp[i + 1] as string)
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]
    }
  }
  return parsed(ramp[ramp.length - 1] as string)
}

const mix = (a: number[], b: number[], t: number): [number, number, number] =>
  [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]

// A building's walls, and its roof (`tone` 0 to 1 picks its tiles).
export function wallColor(look: Look, h: number) {
  return rampAt(rampFor(look), h)
}

export function roofColor(look: Look, h: number, tone: number): [number, number, number] {
  const wall = wallColor(look, h)
  if (look.night) return mix(wall, parsed(NIGHT_ROOF), 0.45)
  if (!look.real) return mix(wall, [0, 0, 0], 0.12)
  return parsed(TILES[Math.floor(tone * TILES.length) % TILES.length])
}

// A steady pseudo-random 0 to 1 from a building's id, so its tiles keep their colour.
export function toneOf(id: number) {
  let x = (id ^ 0x9e3779b9) >>> 0
  x = Math.imul(x ^ (x >>> 16), 0x85ebca6b) >>> 0
  x = Math.imul(x ^ (x >>> 13), 0xc2b2ae35) >>> 0
  return ((x ^ (x >>> 16)) >>> 0) / 4294967296
}
