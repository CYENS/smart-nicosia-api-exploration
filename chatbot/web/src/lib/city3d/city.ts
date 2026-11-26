/* The 3D city's roofs (lib/city3d/roofs.ts) as a layer over MapLibre's walls. The map engine hands
   each DLS building here as it arrives (prepareBuilding, before it reaches the map, so its walls
   stop at the eave), then the batch (addRoofs); the roofs follow what the engine hides, the look
   and the terrain. Old-town houses (chatbot/oldtown.py) come the same way, with their block's id
   in `blk`. */
import type { Map as MlMap } from "maplibre-gl"

import { roofColor, toneOf, wallColor, type Look } from "@/lib/city3d/look"
import { MeshLayer, type Piece } from "@/lib/city3d/mesh-layer"
import { planRoof, roofMesh, type RoofKind } from "@/lib/city3d/roofs"

/* eslint-disable @typescript-eslint/no-explicit-any */
type Feature = { id?: string | number; geometry: any; properties: Record<string, any> }

export const ROOFS = "city3d::roofs"
type Rec = { mats: Uint8Array; h: number; tone: number; kind: RoofKind; blk?: number }

const layer = new MeshLayer(ROOFS, 13.5)
const recs = new Map<number, Rec>()
const pieces = new Map<number, Piece>() // kept, to draw again when the layer comes back
const houses = new Map<number, number[]>() // a block's id: its houses
let look: Look = { night: true, real: false }
let map: MlMap | null = null

// The kinds of roof drawn so far, for the Layers sheet's summary.
export function roofCounts() {
  const out: Record<RoofKind, number> = { flat: 0, hip: 0, gable: 0, mixed: 0 }
  for (const r of recs.values()) out[r.kind]++
  return out
}

// Before a building reaches the map: where its walls stop (`_w`), and its roof, kept for addRoofs.
export function prepareBuilding(f: Feature) {
  const p = f.properties
  if (f.id === undefined || p.split) return
  const id = Number(f.id)
  const planned = planRoof(p as any, f.geometry)
  if (!planned) return
  p._w = planned.plan.wall
  const mesh = roofMesh(planned)
  const rec: Rec = { mats: Uint8Array.from(mesh.mat), h: p.h, tone: toneOf(id), kind: planned.plan.kind, blk: p.blk }
  recs.set(id, rec)
  if (p.blk !== undefined) houses.set(p.blk, [...(houses.get(p.blk) ?? []), id])
  if (!mesh.pos.length) return
  const color = new Float32Array(mesh.mat.length * 3)
  paint(rec, color)
  pieces.set(id, { id, lon: planned.frame.lon0, lat: planned.frame.lat0, pos: mesh.pos, normal: mesh.normal, color })
}

function paint(rec: Rec, out: Float32Array) {
  const wall = wallColor(look, rec.h), roof = roofColor(look, rec.h, rec.tone)
  for (let i = 0; i < rec.mats.length; i++) out.set(rec.mats[i] ? wall : roof, i * 3)
}

// A tile's buildings, once they are in the map's source.
export function addRoofs(features: Feature[]) {
  const batch = features.map((f) => pieces.get(Number(f.id))).filter((p): p is Piece => !!p)
  if (batch.length) layer.add(batch)
}

export function attachRoofs(m: MlMap, before?: string) {
  map = m
  layer.ground = groundOf
  if (!m.getLayer(ROOFS)) {
    m.addLayer(layer, before)
    if (!layer.count && pieces.size) layer.add([...pieces.values()])
  }
  watchTerrain(m)
}

export function detachRoofs(m: MlMap) {
  if (m.getLayer(ROOFS)) m.removeLayer(ROOFS)
}

// What the map hides of the city (a block an answer redraws, the building the eye stands in):
// their roofs go too, and an old-town block's houses with it.
export function hideRoofs(ids: Iterable<number>) {
  const all: number[] = []
  for (const id of ids) {
    all.push(id)
    for (const h of houses.get(id) ?? []) all.push(h)
  }
  layer.setHidden(all)
}

// The ids of the houses standing in for these blocks (the engine hides them with their block).
export function housesOf(blocks: Iterable<number>) {
  const out: number[] = []
  for (const b of blocks) out.push(...(houses.get(b) ?? []))
  return out
}

export function setRoofLook(next: Look) {
  if (next.night === look.night && next.real === look.real) return
  look = next
  for (const [id, p] of pieces) paint(recs.get(id)!, p.color as Float32Array)
  layer.recolor((id, out) => { const r = recs.get(id); if (r) paint(r, out) })
}

// -- the ground under the roofs ---------------------------------------------------------------------

function groundOf(lon: number, lat: number) {
  if (!map?.getTerrain()) return 0
  const h = map.queryTerrainElevation([lon, lat])
  return h === null || h === undefined || !Number.isFinite(h) ? 0 : h
}

let watching: MlMap | null = null
let liftTimer = 0
function liftSoon() {
  clearTimeout(liftTimer)
  liftTimer = window.setTimeout(() => layer.lift(), 200)
}

function watchTerrain(m: MlMap) {
  if (watching === m) return
  watching = m
  m.on("terrain", liftSoon)
  m.on("sourcedata", (e) => { if (e.sourceId === "terrain::dem" && e.isSourceLoaded) liftSoon() })
}
