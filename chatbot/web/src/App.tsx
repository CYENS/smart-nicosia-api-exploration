import { createElement, useEffect, useRef, type CSSProperties } from "react"

import { BaseMapNotice } from "@/components/base-map-notice"
import { ChatPanel } from "@/components/chat/chat-panel"
import { Inspector } from "@/components/inspector/inspector"
import { LayersPanel } from "@/components/layers-panel"
import { Rail } from "@/components/rail"
import { SearchMenu } from "@/components/search-menu"
import { Shortcuts } from "@/components/shortcuts"
import { Stage } from "@/components/stage"
import { StatusStrip } from "@/components/status-strip"
import { TimelapsePlayer } from "@/components/timelapse"
import { TooltipProvider } from "@/components/ui/tooltip"
import { parseHash, startDeepLinks } from "@/lib/deeplink"
import { useGeometry } from "@/lib/geometry"
import { initMap, setInsets } from "@/lib/map/engine"
import { useWorkspace } from "@/lib/registries/workspaces"

// The open workspace's own panels, over the map (Live today has none of its own).
function WorkspacePanels() {
  const panels = useWorkspace((s) => s.list.find((w) => w.id === s.active)?.Panels)
  return panels ? createElement(panels) : null
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
        <TimelapsePlayer />
        <Stage />
        <WorkspacePanels />
        <SearchMenu />
        <Shortcuts />
      </div>
    </TooltipProvider>
  )
}

export default App
