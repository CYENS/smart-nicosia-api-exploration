import { useEffect, useRef, type CSSProperties } from "react"

import { ChatPanel } from "@/components/chat/chat-panel"
import { LayersPanel } from "@/components/layers-panel"
import { QuestionMenu } from "@/components/question-menu"
import { Stage } from "@/components/stage"
import { StatusStrip } from "@/components/status-strip"
import { TimelapsePlayer } from "@/components/timelapse"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useGeometry } from "@/lib/geometry"
import { initMap, setInsets } from "@/lib/map/engine"

export function App() {
  const mapEl = useRef<HTMLDivElement>(null)
  const g = useGeometry()
  const { top, right, bottom, left } = g.insets

  useEffect(() => {
    initMap(mapEl.current!)
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
        <div className="absolute inset-x-0 top-[52px] bottom-0">
          <div ref={mapEl} role="region" aria-label="Map of Nicosia" className="size-full" />
        </div>
        <StatusStrip />
        <ChatPanel />
        <LayersPanel />
        <TimelapsePlayer />
        <Stage />
        <QuestionMenu />
      </div>
    </TooltipProvider>
  )
}

export default App
