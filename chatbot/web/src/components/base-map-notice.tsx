import { MapIcon } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { useGeometry } from "@/lib/geometry"
import { GAP, STRIP_H } from "@/lib/layout"
import { useMapUi } from "@/lib/map/engine"

// Says why the map is empty, in the open part of the map, under the time-lapse's clock if it's on.
export function BaseMapNotice() {
  const problem = useMapUi((s) => s.baseProblem)
  const g = useGeometry()
  if (!problem) return null
  return (
    <div
      className="pointer-events-none absolute z-10 flex justify-center"
      style={{ top: STRIP_H + GAP + g.clockH + 8, left: g.left, right: g.topRight }}
    >
      <Alert className="panel-float pointer-events-auto max-w-[440px]" aria-live="polite">
        <MapIcon />
        <AlertTitle>The base map isn't loading</AlertTitle>
        <AlertDescription>
          A content blocker may be blocking tiles.openfreemap.org. Allow it for this page, or check
          your connection. Trying again every few seconds.
        </AlertDescription>
      </Alert>
    </div>
  )
}
