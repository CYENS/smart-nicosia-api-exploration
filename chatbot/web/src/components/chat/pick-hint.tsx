import { MapPinIcon } from "lucide-react"
import { useEffect, useRef } from "react"

import { Button } from "@/components/ui/button"
import { useGeometry } from "@/lib/geometry"
import { endPick, useMapUi } from "@/lib/map/engine"

// While the user picks their spot on the map ("Pick my spot on the map"): what to do, the map's
// centre for whoever is on the keyboard, and Cancel (Esc too). It sits in the open part of the
// map, where the base map notice goes.
export function PickHint() {
  const picking = useMapUi((s) => s.picking)
  const g = useGeometry()
  const first = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!picking) return
    first.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return
      e.preventDefault()
      e.stopPropagation()
      endPick(null)
    }
    window.addEventListener("keydown", onKey, true) // before any other Esc on the page
    return () => window.removeEventListener("keydown", onKey, true)
  }, [picking])

  if (!picking) return null
  return (
    <div
      className="pointer-events-none absolute z-20 flex justify-center px-3"
      style={{ top: g.insets.top + 8, left: g.left, right: g.topRight }}
    >
      <div
        role="status"
        className="panel-float pointer-events-auto flex max-w-[460px] flex-wrap items-center gap-x-3 gap-y-2 rounded-xl py-2 pr-2 pl-3.5 text-[14px]"
      >
        <span className="flex flex-1 items-center gap-2">
          <MapPinIcon className="size-4 shrink-0" aria-hidden="true" />
          Click the map where you are
        </span>
        <span className="flex gap-1.5">
          <Button ref={first} variant="secondary" size="sm" onClick={() => endPick("centre")}>Use the map centre</Button>
          <Button variant="ghost" size="sm" onClick={() => endPick(null)}>Cancel</Button>
        </span>
      </div>
    </div>
  )
}
