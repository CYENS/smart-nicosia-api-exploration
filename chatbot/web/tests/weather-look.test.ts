// The weather turned into the map's look (src/lib/weather/look.ts). Run with `npm test`
// (Node's own test runner; Node 24 reads TypeScript as it is).
import assert from "node:assert/strict"
import { test } from "node:test"

import { lightFor, skyAt, skyFor } from "../src/lib/map/sky.ts"
import {
  CLEAR, clearSkyFor, conditionsOf, fogAmount, groundFor, isClear, lampsFor, mixConditions, rainAmount, rainFor,
  sunlightFor, weatherLight, weatherSky, weatherWord, type Weather,
} from "../src/lib/weather/look.ts"

const NOON = skyAt(new Date("2026-11-17T10:00:00Z")) // 12:00 in Nicosia, a clear day
const NIGHT = skyAt(new Date("2026-11-17T18:00:00Z")) // 20:00

const clear: Weather = { cloud_pct: 0, cloud_low_pct: 0, cloud_mid_pct: 0, cloud_high_pct: 0, visibility_m: 41360, rain_mm: 0, wet_mm: 0, wind_kmh: 7, wind_from_deg: 299, dust_ugm3: 5, aod: 0.11, code: 0 }
const overcast: Weather = { ...clear, cloud_pct: 91, cloud_low_pct: 91, code: 3 } // 9 Nov 13:00
const shower: Weather = { ...clear, cloud_pct: 73, cloud_low_pct: 49, cloud_mid_pct: 49, cloud_high_pct: 25, rain_mm: 2.5, wet_mm: 4.9, code: 95 }
const fog: Weather = { ...clear, cloud_pct: 68, cloud_low_pct: 68, visibility_m: 40, code: 45 }
const dust: Weather = { ...clear, dust_ugm3: 300, aod: 1.1 }

test("a clear sky changes nothing", () => {
  const c = conditionsOf(clear)
  assert.ok(isClear(c))
  const light = lightFor(NOON)
  assert.equal(weatherLight(light, c, NOON), light)
  const sky = skyFor(NOON)
  assert.equal(weatherSky(sky, c, NOON), sky)
  assert.equal(groundFor(c, NOON, sky).on, false)
  assert.equal(rainFor(c, NOON), null)
  assert.ok(isClear(conditionsOf(null)))
})

test("overcast: flatter, cooler, dimmer light and a greyer sky", () => {
  const c = conditionsOf(overcast)
  const base = lightFor(NOON)
  const l = weatherLight(base, c, NOON)
  assert.ok(l.intensity < base.intensity * 0.6, "softer contrast")
  assert.ok(l.position[2] < base.position[2], "from more overhead")
  assert.notEqual(l.color, base.color)
  const s = weatherSky(skyFor(NOON), c, NOON)
  assert.notEqual(s["sky-color"], skyFor(NOON)["sky-color"])
  assert.ok(s["sky-horizon-blend"] > skyFor(NOON)["sky-horizon-blend"])
  const g = groundFor(c, NOON, s)
  assert.ok(g.on && g.veilA > 0 && g.shadow > 0 && g.cover > 0.8)
  assert.ok(sunlightFor(c, NOON) < 0.2 && sunlightFor(CLEAR, NOON) > 0.95)
  // Shadows fade under cloud, whatever the sun's height; a clear sky leaves them sharp.
  assert.ok(clearSkyFor(c) < 0.2 && clearSkyFor(CLEAR) === 1)
  assert.ok(clearSkyFor(conditionsOf({ ...clear, cloud_pct: 100, cloud_high_pct: 100 })) > 0.6, "thin high cloud lets the sun through")
})

test("at night cloud hides the moon, and the sun never shines", () => {
  const c = conditionsOf(overcast)
  const l = weatherLight(lightFor(NIGHT), c, NIGHT)
  assert.ok(Math.abs(l.intensity - 0.13) < 0.05)
  assert.equal(groundFor(c, NIGHT, skyFor(NIGHT)).shadow, 0)
  assert.equal(sunlightFor(CLEAR, NIGHT), 0)
})

test("rain: drops, wet streets, halo on the lamps", () => {
  const c = conditionsOf(shower)
  assert.ok(c.rain > 0.6 && c.wet === 1)
  const r = rainFor(c, NOON)!
  assert.ok(r.share > 0.6 && r.alpha > 0.2)
  assert.ok(rainAmount(0.01) === 0 && rainAmount(0.1) < rainAmount(1) && rainAmount(10) === 1)
  const after = conditionsOf({ ...overcast, rain_mm: 0, wet_mm: 1.5 })
  assert.equal(rainFor(after, NOON), null)
  assert.ok(after.wet > 0.5 && lampsFor(after).streets > 1)
})

test("fog and dust make haze; dust is warm", () => {
  assert.equal(fogAmount(20000), 0)
  assert.ok(fogAmount(1000) > 0.5 && fogAmount(150) === 1)
  const f = groundFor(conditionsOf(fog), NOON, skyFor(NOON))
  assert.ok(f.hazeK > 2 && f.hazeMax > 0.7 && f.mist > 0)
  const d = conditionsOf(dust)
  assert.ok(d.dust > 0.8)
  const s = weatherSky(skyFor(NOON), d, NOON)
  const [r, , b] = [1, 3, 5].map((i) => parseInt(s["horizon-color"].slice(i, i + 2), 16))
  assert.ok(r > b, "a warm horizon")
  assert.ok(conditionsOf({ ...clear, dust_ugm3: 5, aod: 0.9 }).dust === 0, "haze without dust isn't yellow")
})

test("easing between two moments", () => {
  const a = { ...CLEAR, windTo: 350 }
  const b = { ...CLEAR, rain: 1, windTo: 10 }
  const m = mixConditions(a, b, 0.5)
  assert.equal(m.rain, 0.5)
  assert.ok(Math.abs(m.windTo) < 1e-9 || Math.abs(m.windTo - 360) < 1e-9, "the short way round")
})

test("the word says what the map shows", () => {
  assert.equal(weatherWord(clear), "Clear")
  assert.equal(weatherWord(overcast), "Overcast")
  assert.equal(weatherWord(shower), "Thunderstorm")
  assert.equal(weatherWord({ ...overcast, rain_mm: 1, code: 63 }), "Rain")
  assert.equal(weatherWord(fog), "Fog")
  assert.equal(weatherWord(dust), "Dusty")
  assert.equal(weatherWord({ sky: "clear sky" }), "Clear sky")
  assert.equal(weatherWord(null), null)
})
