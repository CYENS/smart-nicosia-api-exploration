/* "View from here" in the inspector: anything with a place on the map (a viewpoint, a building,
   a stop, a lamp) can be looked out from. A building is looked out from its roof. */
import { EyeIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import type { Entity } from "@/lib/entities"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { viewFrom } from "@/lib/view"

const ROOF_KINDS = new Set(["building_3d"])

function open(e: Entity) {
  const [lon, lat] = e.center!
  void viewFrom({
    from: e.kind === "building_3d" ? e.id : `${lat},${lon}`,
    name: e.name,
    height: ROOF_KINDS.has(e.kind) ? "roof" : undefined,
  })
}

function ViewFromHere({ entity }: SectionProps) {
  return (
    <div className="px-4">
      <Button variant="outline" size="sm" onClick={() => open(entity)}>
        <EyeIcon data-icon="inline-start" />
        {ROOF_KINDS.has(entity.kind) ? "View from its roof" : "View from here"}
      </Button>
    </div>
  )
}

registerInspectorSection({ id: "view-from-here", order: 45, when: (e) => !!e.center, Component: ViewFromHere })
