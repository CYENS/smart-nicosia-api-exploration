/* Tonight in Nicosia map layer: duty pharmacies, Visit Cyprus events, and open-now OSM places,
   rendered via the shared engine point layer and clickable to open in the inspector. */
import { useClock } from "@/lib/clock"
import { removeLayer, showLayer, useMapUi, type FC } from "@/lib/map/engine"
import { registerClickHandler } from "@/lib/registries/map-clicks"
import { useTopics } from "@/lib/registries/topics"
import { loadTonight, type TonightFilter, type TonightSnapshot, useTonight } from "@/lib/tonight/store"

export const TONIGHT_REF = "sys:tonight"

export const TONIGHT_COLORS = {
  duty: "#10b981",
  event: "#ec4899",
  open: "#f59e0b",
} as const

export const TONIGHT_LEGEND: Record<string, string> = {
  "on duty": TONIGHT_COLORS.duty,
  "event": TONIGHT_COLORS.event,
  "open now": TONIGHT_COLORS.open,
}

const SOURCES = ["duty_pharmacies", "dls_addresses", "visit_cyprus", "openstreetmap"]

function pointFeature(lon: number, lat: number, properties: Record<string, unknown>) {
  return {
    type: "Feature" as const,
    geometry: { type: "Point" as const, coordinates: [lon, lat] },
    properties,
  }
}

export function tonightFC(data: TonightSnapshot | null, filter: TonightFilter): FC {
  if (!data) return { type: "FeatureCollection", features: [] }
  const features: FC["features"] = []

  if (filter === "all" || filter === "pharmacies") {
    for (const p of data.pharmacies) {
      if (p.lat === null || p.lon === null) continue
      const walk = p.walk_min ? `${p.walk_min} min walk (${p.distance_m ?? ""} m)` : ""
      const popup = [
        p.name,
        `On duty ${p.duty_date} (until 08:00 next morning)`,
        [p.municipality, p.address].filter(Boolean).join(", "),
        p.directions,
        p.pharmacy_tel ? `Phone: ${p.pharmacy_tel}` : "",
        [walk, p.precision_label].filter(Boolean).join(" · "),
      ]
        .filter(Boolean)
        .join("\n")
      features.push(
        pointFeature(p.lon, p.lat, {
          id: p.id,
          label: p.name,
          kind: "duty_pharmacy",
          state: "on duty",
          color: TONIGHT_COLORS.duty,
          precision: p.precision,
          popup,
        }),
      )
    }
  }

  if (filter === "all" || filter === "events") {
    for (const ev of data.events) {
      if (ev.lat === null || ev.lon === null) continue
      const startTime = ev.start_ts.length >= 16 ? ev.start_ts.slice(11, 16) : ""
      const popup = [
        ev.title,
        startTime ? `Starts ${startTime}` : "On tonight",
        [ev.venue_name, ev.municipality].filter(Boolean).join(", "),
        ev.precision_label,
      ]
        .filter(Boolean)
        .join("\n")
      features.push(
        pointFeature(ev.lon, ev.lat, {
          id: ev.id,
          label: ev.title,
          kind: "city_event",
          state: "event",
          color: TONIGHT_COLORS.event,
          precision: ev.precision,
          popup,
        }),
      )
    }
  }

  if (filter === "all" || filter === "open_now") {
    for (const pl of data.open_now) {
      const popup = [
        pl.name,
        pl.category.replace(/_/g, " "),
        pl.status_text || "Open now",
        pl.walk_min ? `${pl.walk_min} min walk (${pl.distance_m} m)` : "",
      ]
        .filter(Boolean)
        .join("\n")
      features.push(
        pointFeature(pl.lon, pl.lat, {
          id: pl.id,
          label: pl.name,
          kind: "place",
          state: "open now",
          color: TONIGHT_COLORS.open,
          precision: "exact",
          popup,
        }),
      )
    }
  }

  return { type: "FeatureCollection", features }
}

function onMap() {
  return useMapUi.getState().layers.some((l) => l.ref === TONIGHT_REF)
}

function drawTonight() {
  const { on, data, filter } = useTonight.getState()
  if (!on) {
    if (onMap()) removeLayer(TONIGHT_REF)
    return
  }
  const fc = tonightFC(data, filter)
  void showLayer(
    {
      ref: TONIGHT_REF,
      style: "points",
      title: "Tonight in Nicosia",
      count: fc.features.length,
      legend: TONIGHT_LEGEND,
      value_field: null,
      fit: false,
      labels: false,
      sources: SOURCES,
    },
    fc,
  )
}

export function setTonightLayer(on: boolean) {
  if (on === useTonight.getState().on) return
  useTonight.setState({ on })
  if (on) {
    const { status } = useTonight.getState()
    if (status === "ready") drawTonight()
    else void loadTonight()
  } else if (onMap()) {
    removeLayer(TONIGHT_REF)
  }
}

registerClickHandler({
  id: "tonight",
  priority: 18,
  layers: (l) => l.startsWith(`${TONIGHT_REF}::`),
  resolve: (f, at) => {
    const p = f.properties ?? {}
    const id = String(p.id ?? "")
    if (!id) return null
    const coords = f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at
    const lines = String(p.popup ?? "").split("\n").filter(Boolean)
    const title = lines.shift() || String(p.label ?? id)
    return {
      id,
      at: coords,
      fallback: {
        title,
        kind: String(p.kind ?? "place"),
        color: typeof p.color === "string" ? p.color : undefined,
        lines,
        center: coords,
        geometry: f.geometry,
      },
    }
  },
})

let started = false

export function startTonightMap() {
  if (started) return
  started = true

  useTonight.subscribe((s, prev) => {
    if (s.on !== prev.on || s.data !== prev.data || s.filter !== prev.filter) {
      drawTonight()
    }
  })

  useClock.subscribe((s, prev) => {
    if (s.at === prev.at) return
    if (useTonight.getState().on || useTopics.getState().active === "outside") {
      void loadTonight()
    }
  })

  useMapUi.subscribe((s, prev) => {
    if (s.me.loc === prev.me.loc) return
    if (useTonight.getState().on || useTopics.getState().active === "outside") {
      void loadTonight()
    }
  })
}
