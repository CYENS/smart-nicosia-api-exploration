/* Nicosia in the past, in the city clock's compare swipe: left of the swipe, the DLS aerial photo
   of 1993 or 1963 (black and white, through /api/imagery, which keeps its tiles); right of it,
   the twin as it is at the clock's moment.

   The photo is a second, quiet map laid over the first and cut at the swipe (a clip path),
   following the first map's camera move for move: its centre, zoom, tilt, turn, padding and,
   in a view, its terrain and field of view, so the old streets line up with today's as the
   swipe passes over them. It takes no pointer: the map under it pans as usual. It exists only
   while comparing with a photo. */
import maplibregl, { type Map as MlMap } from "maplibre-gl"

import { comparePhoto, setCompare, setExpanded, useClock, type Photo } from "@/lib/clock"
import { flyToThing, getMap, setCity3d, swipeX } from "@/lib/map/engine"
import { setGround } from "@/lib/map/ground"
import { EXAGGERATION, TERRAIN_SOURCE, TERRAIN_TILES } from "@/lib/map/terrain"
import { registerActiveSources } from "@/lib/sources"

const COVER: [number, number, number, number] = [33.18, 35.04, 33.55, 35.27] // chatbot/imagery.py

let overlay: MlMap | null = null
let box: HTMLDivElement | null = null
let shown: Photo | null = null
let raf = 0

registerActiveSources(() => (comparePhoto() ? ["dls_imagery"] : []), (fn) => useClock.subscribe(fn))

function style(photo: Photo): maplibregl.StyleSpecification {
  return {
    version: 8,
    sources: {
      photo: {
        type: "raster", tileSize: 256, minzoom: 11, maxzoom: 18, bounds: COVER,
        tiles: [`${location.origin}/api/imagery/${photo}/{z}/{x}/{y}`],
      },
    },
    layers: [
      { id: "ground", type: "background", paint: { "background-color": "#1b2433" } },
      { id: "photo", type: "raster", source: "photo", paint: { "raster-fade-duration": 150, "raster-resampling": "linear" } },
    ],
    sky: { "sky-color": "#6c7a8c", "horizon-color": "#c9ced6", "fog-color": "#c9ced6", "fog-ground-blend": 0.9, "horizon-fog-blend": 0.6, "sky-horizon-blend": 0.6 },
  }
}

// The left of the swipe shows; the rest is cut away.
function clip() {
  const main = getMap()
  if (!box || !main) return
  const w = main.getCanvas().clientWidth
  box.style.clipPath = `inset(0 ${Math.max(0, w - swipeX())}px 0 0)`
}

function follow() {
  const main = getMap()
  if (!overlay || !main) return
  const terrain = main.getTerrain()
  if (terrain && !overlay.getTerrain()) {
    if (!overlay.getSource(TERRAIN_SOURCE)) {
      overlay.addSource(TERRAIN_SOURCE, { type: "raster-dem", tiles: [TERRAIN_TILES], tileSize: 256, encoding: "terrarium", maxzoom: 15 })
    }
    overlay.setTerrain({ source: TERRAIN_SOURCE, exaggeration: terrain.exaggeration ?? EXAGGERATION })
  } else if (!terrain && overlay.getTerrain()) overlay.setTerrain(null)
  if (overlay.getVerticalFieldOfView() !== main.getVerticalFieldOfView()) overlay.setVerticalFieldOfView(main.getVerticalFieldOfView())
  overlay.setCenterClampedToGround(main.getCenterClampedToGround())
  overlay.setPadding(main.getPadding())
  overlay.jumpTo({
    center: main.getCenter(), zoom: main.getZoom(), bearing: main.getBearing(), pitch: main.getPitch(),
    roll: main.getRoll(), ...(terrain ? { elevation: main.getCenterElevation() } : {}),
  })
  clip()
}

function followSoon() {
  if (raf) return
  raf = requestAnimationFrame(() => { raf = 0; follow() })
}

function open(photo: Photo) {
  const main = getMap()
  if (!main) return
  if (overlay && shown === photo) return
  close()
  shown = photo
  box = document.createElement("div")
  box.className = "past-photo"
  box.setAttribute("aria-hidden", "true")
  Object.assign(box.style, { position: "absolute", inset: "0", pointerEvents: "none" })
  // Over the map's canvas, under its own controls (the scale and the credits).
  const container = main.getContainer()
  container.insertBefore(box, container.querySelector(".maplibregl-control-container"))
  overlay = new maplibregl.Map({
    container: box, style: style(photo), interactive: false, attributionControl: false,
    maxPitch: 89, maxZoom: 24, fadeDuration: 0,
    center: main.getCenter(), zoom: main.getZoom(), bearing: main.getBearing(), pitch: Math.min(main.getPitch(), 85),
  })
  overlay.on("load", follow)
  main.on("move", followSoon)
  main.on("resize", resize)
  follow()
}

function resize() {
  overlay?.resize()
  follow()
}

function close() {
  const main = getMap()
  if (main) {
    main.off("move", followSoon)
    main.off("resize", resize)
  }
  cancelAnimationFrame(raf)
  raf = 0
  overlay?.remove()
  overlay = null
  box?.remove()
  box = null
  shown = null
}

let started = false
export function startPastPhoto() {
  if (started) return
  started = true
  const sync = () => {
    const photo = comparePhoto()
    if (photo) open(photo)
    else if (overlay) close()
  }
  let last = { photo: comparePhoto(), swipe: useClock.getState().swipe }
  useClock.subscribe((s) => {
    const now = { photo: comparePhoto(s), swipe: s.swipe }
    if (now.photo !== last.photo) sync()
    else if (now.swipe !== last.swipe) clip()
    last = now
  })
  sync()
}

// From the assistant's show_aerial_photo: the 2019 photo on the ground, a past photo in the
// compare swipe, or the base map back. With `fly`, the map goes there first.
type PhotoCommand = { ground?: "photo" | "map"; compare?: Photo | "off"; city_3d?: boolean; fly?: { lat: number; lon: number } }

export function applyPhotoCommand(ev: PhotoCommand) {
  if (ev.fly) flyToThing([ev.fly.lon, ev.fly.lat], ev.city_3d ? "building_3d" : undefined)
  if (ev.city_3d) setCity3d(true)
  if (ev.ground) setGround(ev.ground)
  if (ev.compare === "off") { if (comparePhoto()) setCompare(null) }
  else if (ev.compare) { setCompare({ photo: ev.compare }); setExpanded(true) }
}
