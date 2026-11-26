import { createElement, useEffect, useRef, type CSSProperties } from "react"

import { BaseMapNotice } from "@/components/base-map-notice"
import { ChatPanel } from "@/components/chat/chat-panel"
import { Inspector } from "@/components/inspector/inspector"
import { LayersPanel } from "@/components/layers-panel"
import { MapMenu } from "@/components/map-menu"
import { Rail } from "@/components/rail"
import { SearchMenu } from "@/components/search-menu"
import { Shortcuts } from "@/components/shortcuts"
import { Stage } from "@/components/stage"
import { StatusStrip } from "@/components/status-strip"
import { CityClock } from "@/components/clock/city-clock"
import { CompareSwipe } from "@/components/clock/compare-swipe"
import { TooltipProvider } from "@/components/ui/tooltip"
import { startClock } from "@/lib/clock"
import { parseHash, startDeepLinks } from "@/lib/deeplink"
import { useGeometry } from "@/lib/geometry"
import { initMap, setInsets } from "@/lib/map/engine"
import "@/lib/here" // "What's here?": any point of the map, its card, the click, press and Ctrl K
import "@/lib/mobility" // buses, stops and road speeds: their workspace, kinds, layers and commands
import { useWorkspace, useWorkspaceSide } from "@/lib/registries/workspaces"

// The open workspace's own panels, over the map (Live today has none of its own), and its side
// panel in the right column.
function WorkspacePanels() {
  const panels = useWorkspace((s) => s.list.find((w) => w.id === s.active)?.Panels)
  const side = useWorkspaceSide()
  return (
    <>
      {panels && createElement(panels)}
      {side && createElement(side)}
    </>
  )
}

export function App() {
  const mapEl = useRef<HTMLDivElement>(null)
  const g = useGeometry()
  const { top, right, bottom, left } = g.insets

  useEffect(() => {
    // A link brings back its camera and the thing it had open.
    const link = parseHash(location.hash)
    initMap(mapEl.current!, link.camera)
    startDeepLinks(link)
    startClock()
  }, [])

  // The camera centres on the part of the map the panels leave open.
  useEffect(() => {
    setInsets({ top, right, bottom, left })
  }, [top, right, bottom, left])

  // MapLibre's own controls sit just inside the panels.
  const vars = {
    "--inset-left": `${left}px`,
    "--inset-right": `${right}px`,
    "--inset-bottom": `${bottom}px`,
  } as CSSProperties

  return (
    <TooltipProvider delay={250}>
      <div className="map-root relative h-full w-full overflow-hidden bg-night" style={vars}>
        {/* MapLibre makes its container position: relative, so the box around it does the placing. */}
        <div className="absolute top-[52px] right-0 bottom-0" style={{ left: g.rail }}>
          <div ref={mapEl} role="region" aria-label="Map of Nicosia" className="size-full" />
        </div>
        <StatusStrip />
        <Rail />
        <ChatPanel />
        <LayersPanel />
        <Inspector />
        <BaseMapNotice />
        <CompareSwipe />
        <CityClock />
        <Stage />
        <WorkspacePanels />
        <SearchMenu />
        <MapMenu />
        <Shortcuts />
      </div>
    </TooltipProvider>
  )
}

export default App
