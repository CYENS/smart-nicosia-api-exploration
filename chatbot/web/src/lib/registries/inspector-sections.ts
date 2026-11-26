/* What the inspector shows about a thing, section by section. Each section says which kinds it
   is for and where it goes; a later feature adds one (a typical-day band, a forecast, the
   parcel) without touching the inspector itself. */
import type { ComponentType } from "react"

import type { Band, Entity, Series } from "@/lib/entities"

export type SectionProps = {
  entity: Entity
  series: Series | null
  seriesStatus: "idle" | "loading" | "ready" | "error"
  band?: Band | null // the usual behind the sparkline, when the twin's memory has one
}

export type InspectorSection = {
  id: string
  order: number // lower first: the headline readout is 10, readings 20, relations 50, sources 90
  kinds?: string[] // every kind when left out
  when?: (entity: Entity) => boolean
  Component: ComponentType<SectionProps>
}

const sections: InspectorSection[] = []

export function registerInspectorSection(s: InspectorSection) {
  const i = sections.findIndex((x) => x.id === s.id)
  if (i >= 0) sections.splice(i, 1)
  sections.push(s)
  sections.sort((a, b) => a.order - b.order)
}

export function sectionsFor(entity: Entity) {
  return sections.filter((s) => (!s.kinds || s.kinds.includes(entity.kind)) && (!s.when || s.when(entity)))
}
