/* The twin's entities on the page: their shape (as /api/entities sends it), what each kind looks
   like, how to recognise an id in an answer, and cached fetches. */
import {
  Building2Icon, CctvIcon, GlobeIcon, HexagonIcon, LightbulbIcon, LocateFixedIcon, MapPinIcon,
  MonitorIcon, RouteIcon, SignpostIcon, SirenIcon, SquareParkingIcon, Trash2Icon, TriangleAlertIcon, WindIcon,
  type LucideIcon,
} from "lucide-react"

export type EntityKind =
  | "street_light" | "parking_sensor" | "waste_bin" | "air_station" | "info_screen" | "camera"
  | "register_building" | "building_3d" | "place" | "web_place" | "overture_place"
  | "bus" | "bus_stop" | "bus_line" | "road_segment" | "incident"
  | "aircraft" | "its_cctv" | "its_detector"
  | "spot"

export type Tone = "alarm" | "warn" | "live" | "ok" | "muted"

export type EntityState = {
  value: string; label: string; color: string; tone: Tone; stale: boolean
  reason?: string | null; local?: string | null; as_of?: string | null; age_s?: number | null
}
export type Reading = { key: string; label: string; value: number | string; unit: string; as_of?: string | null; stale?: boolean }
export type Attribute = { label: string; value: string | number }
export type Relation = {
  relation: string; label: string; name?: string; id?: string; kind?: EntityKind
  distance_m?: number; state?: string; color?: string; detail?: string; lon?: number; lat?: number
}
export type Source = {
  name: string; detail?: string | null; url?: string | null; as_of?: string | null
  source?: string | null // its id in the data sources panel (lib/sources.ts)
}
export type Footprint = { geometry: GeoJSON.Geometry; height_m: number }

export type SeriesPoint = { t: string; v: number | null; s?: string }
export type Series = {
  metric: string; label: string; unit: string; draw: "line" | "states" | "bars"; step_min: number
  points: SeriesPoint[]; from: string; to: string
  state_colors?: Record<string, string>
  summary?: { min?: number; max?: number; last?: number; mean?: number; occupied_pct?: number } | null
  threshold?: { value: number; label: string }
  totals_24h?: Record<string, number>
  error?: string
}

export type Entity = {
  id: string; kind: EntityKind; kind_label: string; name: string; subtitle: string | null
  geometry: GeoJSON.Geometry | null; center: [number, number] | null; footprints: Footprint[]
  state: EntityState | null; readings: Reading[]; attributes: Attribute[]; relations: Relation[]
  sources: Source[]; as_of: string | null; has_series: boolean
  // Set when the entity is read at a past moment of the city clock.
  moment?: { at: string; label: string; known: boolean; applies: boolean }
  pending?: boolean // a placeholder shown while the record loads (see placeholderEntity)
}

// The usual for each moment of a series (#9's typical day: the median and quartiles for that
// hour of that weekday over the stored weeks), from /api/history/{id}.
export type Band = { points: { t: string; mid: number | null; lo: number | null; hi: number | null }[]; weeks: number; scale: number }

// What a chip, a search result or the composer knows about a thing before it is fetched.
export type EntityRef = { id: string; kind?: EntityKind | string; name?: string }

// Something on the map the twin has no record for (a route, a stop, a reachable area, "you"):
// the inspector shows what the layer says about it.
export type LocalThing = {
  title: string; kind: string; color?: string; lines: string[]; center: [number, number]
  geometry?: GeoJSON.Geometry
}

type KindMeta = { label: string; icon: LucideIcon; chip: string }

export const KINDS: Record<string, KindMeta> = {
  street_light: { label: "Street light", icon: LightbulbIcon, chip: "device" },
  parking_sensor: { label: "Parking bay", icon: SquareParkingIcon, chip: "device" },
  waste_bin: { label: "Waste bin", icon: Trash2Icon, chip: "device" },
  air_station: { label: "Air station", icon: WindIcon, chip: "device" },
  info_screen: { label: "Info screen", icon: MonitorIcon, chip: "device" },
  camera: { label: "Camera", icon: CctvIcon, chip: "device" },
  register_building: { label: "Dangerous building", icon: TriangleAlertIcon, chip: "danger" },
  incident: { label: "Incident", icon: SirenIcon, chip: "danger" }, // opens in the incident inbox
  building_3d: { label: "Building", icon: Building2Icon, chip: "building" },
  place: { label: "Place", icon: MapPinIcon, chip: "place" },
  web_place: { label: "From the web", icon: GlobeIcon, chip: "place" },
  overture_place: { label: "Place", icon: MapPinIcon, chip: "place" },
  street: { label: "Street", icon: SignpostIcon, chip: "place" },
  route: { label: "Route", icon: RouteIcon, chip: "place" },
  stop: { label: "Stop", icon: MapPinIcon, chip: "place" },
  area: { label: "Area", icon: HexagonIcon, chip: "place" },
  you: { label: "Your position", icon: LocateFixedIcon, chip: "place" },
}

export function kindMeta(kind: string | undefined): KindMeta {
  return KINDS[kind ?? ""] ?? { label: "Thing", icon: MapPinIcon, chip: "place" }
}

// Ids an answer can name, by kind. Device names that aren't codes (cameras, screens) are left
// alone in prose: they read as names, and the trace links them anyway.
const ID_KINDS: [RegExp, EntityKind][] = [
  [/^YL\d{2,6}$/, "street_light"],
  [/^YP\d{2,6}$/, "parking_sensor"],
  [/^YW\d{2,6}$/, "waste_bin"],
  [/^(NIC|LIM|LAR)-\d{1,5}(-\d+)?$|^FAM-15[AB]-\d{1,5}$/, "register_building"],
  [/^B\d{3,10}$/, "building_3d"],
  [/^(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{12}$/, "air_station"],
  [/^bus-\d{1,6}$/, "bus"],
  [/^stop-\d{1,7}$/, "bus_stop"],
  [/^line-(npt|ic|pame)-[a-z0-9]{1,8}$/, "bus_line"],
  [/^road-\d{1,5}$/, "road_segment"],
  [/^web-[0-9a-f]{8}$/, "web_place"],
  [/^ov-[0-9a-f]{12}$/, "overture_place"],
  [/^INC-\d{1,7}$/, "incident"],
  [/^ac-[0-9a-f]{6}$/i, "aircraft"],
  [/^cctv-\d{1,6}$/i, "its_cctv"],
  [/^det-\d{1,6}$/i, "its_detector"],
  [/^spot-\d{1,2}\.\d{1,6}-\d{1,2}\.\d{1,6}$/, "spot"],
]
export const ID_IN_TEXT = /\b(Y[LPW]\d{2,6}|(?:NIC|LIM|LAR)-\d{1,5}(?:-\d+)?|FAM-15[AB]-\d{1,5}|B\d{3,10}|[0-9a-f]{12}|bus-\d{1,6}|stop-\d{1,7}|line-(?:npt|ic|pame)-[a-z0-9]{1,8}|road-\d{1,5}|web-[0-9a-f]{8}|ov-[0-9a-f]{12}|INC-\d{1,7}|ac-[0-9a-f]{6}|cctv-\d{1,6}|det-\d{1,6}|spot-\d{1,2}\.\d{1,6}-\d{1,2}\.\d{1,6})\b/g

export function kindOfId(id: string): EntityKind | undefined {
  return ID_KINDS.find(([re]) => re.test(id))?.[1]
}

// Ids of things on the map that are drawn by an answer and have no record of their own.
const LOCAL_ID = /^(route|route_usual|stop\d+|iso\d+(\.\d+)?|origin|you|street)$/
export function isLocalId(id: string) {
  return LOCAL_ID.test(id)
}

// A record to show at once while the real one loads, for a kind whose id says enough (a spot:
// where it is). A feature registers its own with registerPlaceholder.
const placeholders: ((id: string) => Entity | null)[] = []

export function registerPlaceholder(fn: (id: string) => Entity | null) {
  placeholders.push(fn)
}

export function placeholderEntity(id: string): Entity | null {
  for (const fn of placeholders) {
    const e = fn(id)
    if (e) return { ...e, pending: true }
  }
  return null
}

// -- fetching, with a short cache so moving between things is instant -------------------------

const TTL_MS = 60_000
const cache = new Map<string, { at: number; entity: Entity }>()
const seriesCache = new Map<string, { at: number; series: Series | null }>()

function query(at?: [number, number] | null, extra: Record<string, string> = {}) {
  const p = new URLSearchParams(extra)
  if (at) { p.set("lon", String(at[0])); p.set("lat", String(at[1])) }
  const s = p.toString()
  return s ? `?${s}` : ""
}

export class EntityError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function getJson(url: string, signal?: AbortSignal) {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new EntityError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`, res.status)
  }
  return res.json()
}

// `moment` is a past moment of the city clock as Nicosia wall time ("2026-11-21T21:00"); left
// out, the thing as it is now.
export function cachedEntity(id: string, moment?: string | null) {
  const hit = cache.get(`${id.toLowerCase()}@${moment ?? ""}`)
  return hit && Date.now() - hit.at < TTL_MS * (moment ? 10 : 1) ? hit.entity : null
}

export async function fetchEntity(id: string, at?: [number, number] | null, signal?: AbortSignal, moment?: string | null): Promise<Entity> {
  const hit = cachedEntity(id, moment)
  if (hit) return hit
  const extra: Record<string, string> = { series: "false", ...(moment ? { at: moment } : {}) }
  const entity = (await getJson(`/api/entities/${encodeURIComponent(id)}${query(at, extra)}`, signal)) as Entity
  for (const key of new Set([id.toLowerCase(), entity.id.toLowerCase()])) cache.set(`${key}@${moment ?? ""}`, { at: Date.now(), entity })
  return entity
}

export async function fetchSeries(id: string, at?: [number, number] | null, signal?: AbortSignal, moment?: string | null): Promise<Series | null> {
  const key = `${id.toLowerCase()}@${moment ?? ""}`
  const hit = seriesCache.get(key)
  if (hit && Date.now() - hit.at < TTL_MS * 5) return hit.series
  const body = await getJson(`/api/entities/${encodeURIComponent(id)}/series${query(at, moment ? { at: moment } : {})}`, signal)
  seriesCache.set(key, { at: Date.now(), series: body.series })
  return body.series as Series | null
}

// The metric of /api/history that matches each kind's sparkline, and how to scale it to the
// sparkline's unit (a lamp's power in kW to W).
const BAND_METRIC: Record<string, [string, number]> = {
  street_light: ["power_kw", 1000], waste_bin: ["fill_pct", 1], air_station: ["caqi", 1], parking_sensor: ["occupancy_pct", 1],
}

export async function fetchBand(entity: Entity, series: Series, signal?: AbortSignal): Promise<Band | null> {
  const metric = BAND_METRIC[entity.kind]
  if (!metric) return null
  const [name, scale] = metric
  const p = new URLSearchParams({ hours: "24", metric: name, until: series.to })
  const body = await getJson(`/api/history/${encodeURIComponent(entity.id)}?${p}`, signal)
  if (!body.baseline || !Array.isArray(body.points)) return null
  const k = (v: unknown) => (typeof v === "number" ? v * scale : null)
  return {
    points: body.points.map((x: { time: string; usual?: number; usual_low?: number; usual_high?: number }) => ({ t: x.time, mid: k(x.usual), lo: k(x.usual_low), hi: k(x.usual_high) })),
    weeks: body.baseline.weeks, scale,
  }
}

export type SearchResult = {
  id: string | null; kind: string; name: string; group: string; detail?: string
  lat?: number | null; lon?: number | null; state?: string; color?: string; bbox?: [number, number, number, number]
}

export async function searchEntities(q: string, signal?: AbortSignal): Promise<SearchResult[]> {
  const body = await getJson(`/api/entities/search?${new URLSearchParams({ q })}`, signal)
  return body.results as SearchResult[]
}

// -- reading out times ----------------------------------------------------------------------------

export function ago(iso: string | null | undefined, now = Date.now()) {
  if (!iso) return null
  const s = Math.max(0, (now - Date.parse(iso)) / 1000)
  if (!Number.isFinite(s)) return null
  if (s < 90) return "just now"
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 48 * 3600) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} days ago`
}

export function clockTime(iso: string | null | undefined) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" })
}

export function metres(m: number | undefined) {
  if (m === undefined) return null
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`
}
