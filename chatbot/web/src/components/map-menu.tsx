/* The map's right-click menu: "What's here?" about the point, and "I'm here" to share it as your
   position. A shadcn dropdown menu opened at the pointer (the engine says where), so it has the
   menu keyboard: arrows move, Enter chooses, Esc closes and gives the keyboard back to the map. */
import { LocateFixedIcon, MapPinIcon } from "lucide-react"
import { useRef } from "react"

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { inspectSpot } from "@/lib/here"
import { setMyLocation, useMapUi } from "@/lib/map/engine"

const canvas = () => document.querySelector<HTMLElement>(".maplibregl-canvas")

export function MapMenu() {
  const menu = useMapUi((s) => s.menu)
  const chose = useRef(false)
  const close = () => useMapUi.setState({ menu: null })
  return (
    // Keyed by the right-click, so a second one opens the menu afresh where it was.
    <DropdownMenu key={menu?.n ?? 0} open={!!menu} onOpenChange={(open) => { if (!open) close() }}>
      <DropdownMenuTrigger
        nativeButton={false}
        render={<span aria-hidden="true" tabIndex={-1} className="pointer-events-none fixed size-px" style={{ left: menu?.x ?? 0, top: menu?.y ?? 0 }} />}
      />
      <DropdownMenuContent
        className="w-auto min-w-44"
        // A choice takes the keyboard where it leads (the inspector); Esc gives it to the map.
        finalFocus={() => (chose.current ? false : canvas())}
      >
        <DropdownMenuItem onClick={() => {
          if (!menu) return
          chose.current = true
          inspectSpot(menu.at, { focus: true, opener: canvas() })
        }}>
          <MapPinIcon />
          What's here?
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => { if (menu) setMyLocation(menu.at[1], menu.at[0], NaN, "set on the map") }}>
          <LocateFixedIcon />
          I'm here
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
