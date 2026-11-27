/* Weather on the map: the weather of the clock's moment (live by default) laid over the real sun
   and moon. It follows the strip the clock reads (/api/overview, now or `?at=`), eases from one
   moment's weather to the next, and drives:
   - the light on the 3D buildings and the sky of a tilted view (through the engine's hooks);
   - the ground layer (lib/weather/ground.ts): cloud shadows, the weather's wash, haze and mist,
     clouds in a tilted view's sky, wet roads;
   - the rain canvas (lib/weather/rain.ts);
   - the lamps' halo in wet or misty air.
   A switch in the Layers sheet turns it off (remembered on this device): the map is then exactly
   as it was before the weather came to it. Reduced motion: no falling rain or drifting cloud,
   still haze, and the weather changes at once rather than easing.

   On a slow machine it measures itself: when the rain's frames come slower than about 40 a
   second, it draws fewer drops at a lower resolution, fewer octaves of cloud and mist, and lets
   the clouds drift less often. */
import { create } from "zustand"

import { useClock } from "@/lib/clock"
import {
  getMap, onStyleReady, onWeather, refreshSky, setLampWeather, shownSky,
} from "@/lib/map/engine"
import { GroundLayer, WET_LAYER, placeWeather, type GroundFrame } from "@/lib/weather/ground"
import {
  CLEAR, clearSkyFor, conditionsOf, groundFor, isClear, lampsFor, mixConditions, rainFor, sunlightFor, weatherLight, weatherSky,
  weatherWord, type Conditions, type Weather,
} from "@/lib/weather/look"
import { RainOverlay } from "@/lib/weather/rain"

const KEY = "twin.weather.map"
const EASE_MS = 1800

type Quality = "full" | "lite"
type State = {
  on: boolean // "Weather on the map"
  weather: Weather | null // the weather of the clock's moment (or a preview)
  shown: Conditions // what the map shows now, eased
  word: string | null // the weather in a word, as the map shows it
  sunlight: number // how much direct sun reaches the ground, 0 to 1
  quality: Quality
}

function readOn() {
  try {
    return localStorage.getItem(KEY) !== "0"
  } catch {
    return true
  }
}

export const useWeatherMap = create<State>(() => ({
  on: readOn(), weather: null, shown: CLEAR, word: null, sunlight: 1, quality: "full",
}))
const set = useWeatherMap.setState
const get = useWeatherMap.getState

const reduce = window.matchMedia("(prefers-reduced-motion: reduce)")
const moving = () => !reduce.matches

// How much direct sun reaches the ground at the clock's moment, as the map shows it, 0 to 1:
// 1 with the sun up in a clear sky, 0 at night or under thick cloud, fog or rain. With the
// weather off the map, the sky is taken as clear. For shadows that soften under cloud.
export function sunlight() {
  return sunlightFor(get().shown, shownSky())
}

// How sharp shadows are under the weather the map shows: 1 under a clear sky (or with the
// weather off), down towards 0 under thick cloud. The sun's height is left to the caller.
export function clearSky() {
  return clearSkyFor(get().shown)
}

export function setWeatherOn(on: boolean) {
  try { localStorage.setItem(KEY, on ? "1" : "0") } catch { /* private mode */ }
  set({ on })
  retarget()
}

// For checking a look by hand or in a browser test (there was no Saharan dust in the last 62
// days to move the clock to): window.__weather.preview({ dust_ugm3: 300, aod: 1.1 }), null to end.
let preview: Weather | null = null

// -- easing ------------------------------------------------------------------------------------

let from: Conditions = CLEAR
let target: Conditions = CLEAR
let easeStart = 0
let easeRaf = 0

function retarget() {
  const s = get()
  const weather = preview ?? useClock.getState().strip?.weather ?? null
  const next = s.on ? conditionsOf(weather as Weather | null) : CLEAR
  set({ weather: weather as Weather | null, word: weatherWord(weather as Weather | null) })
  if (same(next, target)) { apply(); return }
  target = next
  from = s.shown
  easeStart = performance.now()
  if (!moving() || !getMap()) { cancelAnimationFrame(easeRaf); easeRaf = 0; set({ shown: target }); apply(); return }
  if (!easeRaf) easeRaf = requestAnimationFrame(step)
}

function same(a: Conditions, b: Conditions) {
  return (Object.keys(a) as (keyof Conditions)[]).every((k) => Math.abs(a[k] - b[k]) < 1e-4)
}

function step(now: number) {
  const t = Math.min(1, (now - easeStart) / EASE_MS)
  const e = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2
  set({ shown: mixConditions(from, target, e) })
  apply()
  easeRaf = t < 1 ? requestAnimationFrame(step) : 0
}

// -- the map -------------------------------------------------------------------------------------

const rain = new RainOverlay()
const layer = new GroundLayer(frame, (c) => rain.setCamera(c))
let lastWet = -1

function frame(): GroundFrame | null {
  const c = get().shown
  if (isClear(c)) return null
  const sky = shownSky()
  const spec = weatherSky(skyBase(), c, sky)
  return {
    ground: groundFor(c, sky, spec),
    windTo: c.windTo,
    windKmh: c.windKmh,
    octaves: get().quality === "lite" ? 3 : 5,
    moving: moving(),
  }
}

// The sky the engine set before the weather leaned it (the hook keeps it).
let lastSkyBase: Parameters<typeof weatherSky>[0] | null = null
function skyBase() {
  return lastSkyBase ?? { "sky-color": "#6ea4df", "horizon-color": "#d6e4f2", "fog-color": "#e7edf3", "sky-horizon-blend": 0.6, "horizon-fog-blend": 0.7, "fog-ground-blend": 0.85, "atmosphere-blend": 0 }
}

function apply() {
  const map = getMap()
  const c = get().shown
  const sky = shownSky()
  set({ sunlight: sunlightFor(c, sky) })
  if (!map) return
  refreshSky()
  setLampWeather(lampsFor(c))
  rain.quiet = radarOn() ? 0.5 : 1
  rain.set(rainFor(c, sky), moving())
  // Wet roads by day.
  const wet = sky.period === "day" || sky.period === "golden" ? c.wet : 0
  if (map.getLayer(WET_LAYER) && Math.abs(wet - lastWet) > 0.01) {
    lastWet = wet
    map.setLayoutProperty(WET_LAYER, "visibility", wet > 0.02 ? "visible" : "none")
    map.setPaintProperty(WET_LAYER, "line-opacity", 0.22 * wet)
  }
  map.triggerRepaint()
  drift()
}

// While the clouds and the mist drift, the map is drawn again now and then: often enough that
// they glide (about a pixel a step), seldom enough to leave the machine alone.
let driftTimer = 0
function drift() {
  window.clearTimeout(driftTimer)
  const map = getMap()
  const f = frame()
  if (!map || !f || !f.moving || document.hidden) return
  const g = f.ground
  if (g.shadow <= 0 && g.mist <= 0 && !(g.clouds > 0 && map.getPitch() > 55)) return
  const pxM = (156543 * Math.cos((map.getCenter().lat * Math.PI) / 180)) / 2 ** map.getZoom()
  const pxPerS = Math.max(((f.windKmh / 3.6) * 4) / pxM, 0.5)
  const fps = Math.min(get().quality === "lite" ? 3 : 8, Math.max(1, pxPerS))
  driftTimer = window.setTimeout(() => { map.triggerRepaint(); drift() }, 1000 / fps)
}
document.addEventListener("visibilitychange", () => drift())

// The map again, as the weather is (the rain radar came or went, say).
export function refreshWeather() {
  apply()
}

// Whether the map shows any weather (for its credit).
export function weatherShows() {
  const s = get()
  return s.on && !isClear(s.shown)
}

// The environment twin's rain radar, when on: the rain falls fainter over it, so its colours read.
let radarOn = () => false
export function onRadar(fn: () => boolean) {
  radarOn = fn
}

// The quality fallback: judged from the rain's frames (the costliest moving part) every few
// seconds while it falls. Slow twice in a row (not the first seconds, when the map is still
// loading) goes to lite; quick twice in a row goes back.
let slow = 0
window.setInterval(() => {
  const f = rain.frames
  if (f.length < 120) return
  const sorted = [...f].sort((a, b) => a - b)
  const median = sorted[Math.floor(sorted.length / 2)]
  const lite = get().quality === "lite"
  slow = (lite ? median < 18 : median > 25) ? slow + 1 : 0
  if (slow < 2) return
  slow = 0
  set({ quality: lite ? "full" : "lite" })
  rain.quality = lite ? 1 : 0.5
  rain.frames = []
  apply()
}, 4000)

// -- start ---------------------------------------------------------------------------------------

export function startWeatherMap() {
  onWeather({
    light: (l, sky) => weatherLight(l, get().shown, sky),
    sky: (s, sky) => {
      lastSkyBase = s
      return weatherSky(s, get().shown, sky)
    },
    place: () => {
      const map = getMap()
      if (!map) return
      placeWeather(map, layer)
      rain.attach(map.getCanvas())
      lastWet = -1
      window.setTimeout(apply) // once the engine has finished with its layers
    },
  })
  onStyleReady(() => apply())
  useClock.subscribe((s, p) => {
    if (s.strip !== p.strip) retarget()
    else if (s.at !== p.at) apply() // the sky moved under the same weather
  })
  reduce.addEventListener("change", () => { cancelAnimationFrame(easeRaf); easeRaf = 0; set({ shown: target }); apply() })
  retarget()
  ;(window as unknown as { __weather: unknown }).__weather = {
    state: get,
    preview: (w: Weather | null) => { preview = w; retarget() },
    setOn: setWeatherOn,
    frames: () => rain.frames.slice(),
    sunlight,
  }
}
