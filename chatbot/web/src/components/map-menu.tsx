/* The map's right-click menu: what the features registered for a point of the map ("What's
   here?", "I'm here", ...; registerMapMenuItem in lib/map/engine.ts). A shadcn dropdown menu
   opened at the pointer, so it has the menu keyboard: arrows move, Enter chooses, Esc closes and
   gives the keyboard back to the map. */
import { useRef } from "react"

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { mapMenuItems, useMapUi } from "@/lib/map/engine"

const canvas = () => document.querySelector<HTMLElement>(".maplibregl-canvas")

export function MapMenu() {
  const menu = useMapUi((s) => s.menu)
  const opened = useRef(false) // an item that opens something (the inspector) takes the keyboard
  const close = () => useMapUi.setState({ menu: null })
  return (
    // Keyed by the right-click, so a second one opens the menu afresh where it was.
    <DropdownMenu key={menu?.n ?? 0} open={!!menu} onOpenChange={(open) => { if (!open) close() }}>
      <DropdownMenuTrigger
        nativeButton={false}
        render={<span aria-hidden="true" tabIndex={-1} className="pointer-events-none fixed size-px" style={{ left: menu?.x ?? 0, top: menu?.y ?? 0 }} />}
      />
      <DropdownMenuContent className="w-auto min-w-44" aria-label="This place on the map" finalFocus={() => (opened.current ? false : canvas())}>
        {menu && mapMenuItems().map(({ id, label, icon: Icon, opens, run }) => (
          <DropdownMenuItem key={id} onClick={() => {
            opened.current = !!opens
            run({ lng: menu.at[0], lat: menu.at[1] })
          }}>
            {Icon && <Icon />}
            {label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
