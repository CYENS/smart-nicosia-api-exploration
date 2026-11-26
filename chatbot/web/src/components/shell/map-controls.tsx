/* The map's controls in one column under the now line: Layers, the 3D view, locate me, zoom, and
   the compass while the map is turned. */
import { LayersIcon, LocateFixedIcon, MinusIcon, NavigationIcon, PlusIcon } from "lucide-react"
import { useEffect, useState, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { ButtonGroup } from "@/components/ui/button-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useGeometry } from "@/lib/geometry"
import { closeLayers, openLayers } from "@/lib/layers-sheet"
import { useLayout } from "@/lib/layout"
import { getMap, locateMe, useMapUi } from "@/lib/map/engine"
import { useMobility } from "@/lib/mobility/store"
import { cn } from "@/lib/utils"

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches

// The camera's tilt and turn, kept in step with the map.
export function useCameraAngles() {
  const ready = useMapUi((s) => s.ready)
  const [angles, setAngles] = useState({ pitch: 0, bearing: 0 })
  useEffect(() => {
    const map = getMap()
    if (!map) return
    const read = () => setAngles({ pitch: map.getPitch(), bearing: map.getBearing() })
    read()
    map.on("pitchend", read)
    map.on("rotateend", read)
    map.on("moveend", read)
    return () => { map.off("pitchend", read); map.off("rotateend", read); map.off("moveend", read) }
  }, [ready])
  return angles
}

// The 3D view: the map tilted so buildings stand up, or flat.
export function set3dView(on: boolean) {
  getMap()?.easeTo({ pitch: on ? 55 : 0, duration: reducedMotion() ? 0 : 700 })
}

// How many things the map shows beyond the base map, for the Layers button.
export function useLayersOn() {
  const layers = useMapUi((s) => s.layers.length)
  const city = useMapUi((s) => s.city.on)
  const buses = useMobility((s) => s.buses.on)
  const roads = useMobility((s) => s.roads.on)
  return layers + (city ? 1 : 0) + (buses ? 1 : 0) + (roads ? 1 : 0)
}

function Control({ label, hint, children, className, ...props }: {
  label: string; hint?: ReactNode; children: ReactNode; className?: string
} & Omit<React.ComponentProps<typeof Button>, "children">) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="secondary"
            size="icon"
            aria-label={label}
            className={cn("size-10 bg-console text-foreground shadow-none hover:bg-raised [&_svg:not([class*='size-'])]:size-[18px]", className)}
            {...props}
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent side="left">{label}{hint && <> {hint}</>}</TooltipContent>
    </Tooltip>
  )
}

export function MapControls() {
  const g = useGeometry()
  const layersOpen = useLayout((s) => s.layersOpen)
  const on = useLayersOn()
  const { pitch, bearing } = useCameraAngles()
  const tilted = pitch > 10
  const turned = Math.abs(bearing) > 1
  const zoom = (by: number) => {
    const map = getMap()
    if (map) map.easeTo({ zoom: map.getZoom() + by, duration: reducedMotion() ? 0 : 300 })
  }

  return (
    <div
      className="absolute z-10 flex flex-col items-end gap-2"
      style={{ top: g.controlsTop, right: g.controlsRight }}
      role="group"
      aria-label="Map controls"
    >
      <Control
        label={layersOpen ? "Hide the layers" : "Layers"}
        hint={!layersOpen && on > 0 ? <span className="text-background/70">{on} on</span> : undefined}
        aria-expanded={layersOpen}
        aria-controls={layersOpen ? "layers-sheet" : undefined}
        data-layers-button=""
        className={cn("panel-float relative rounded-xl", layersOpen && "bg-sodium/[0.14] inset-ring-1 inset-ring-sodium/55 hover:bg-sodium/20 [&_svg]:text-sodium")}
        onClick={() => (layersOpen ? closeLayers() : openLayers())}
      >
        <LayersIcon />
        {on > 0 && !layersOpen && (
          <span className="absolute -top-1 -right-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-raised px-1 font-readout text-[11px] leading-none font-semibold text-foreground ring-2 ring-console" aria-hidden="true">
            {on}
          </span>
        )}
      </Control>
      <ButtonGroup orientation="vertical" className="panel-float rounded-xl [&>*]:border-0">
        <Control
          label={tilted ? "Flat view" : "3D view"}
          aria-pressed={tilted}
          className={cn("rounded-xl font-readout text-[13px] font-bold", tilted && "text-sodium")}
          onClick={() => set3dView(!tilted)}
        >
          3D
        </Control>
        <Control label="Locate me" hint={<span className="text-background/70">shares where you are with the assistant</span>} className="rounded-xl" onClick={locateMe}>
          <LocateFixedIcon />
        </Control>
      </ButtonGroup>
      {g.desktop && (
        <ButtonGroup orientation="vertical" className="panel-float rounded-xl [&>*]:border-0">
          <Control label="Zoom in" className="rounded-xl" onClick={() => zoom(1)}><PlusIcon /></Control>
          <Control label="Zoom out" className="rounded-xl" onClick={() => zoom(-1)}><MinusIcon /></Control>
        </ButtonGroup>
      )}
      {turned && (
        <Control
          label="Turn the map to north"
          className="panel-float rounded-xl"
          onClick={() => getMap()?.easeTo({ bearing: 0, duration: reducedMotion() ? 0 : 500 })}
        >
          <NavigationIcon style={{ transform: `rotate(${-bearing}deg)` }} />
        </Control>
      )}
    </div>
  )
}
