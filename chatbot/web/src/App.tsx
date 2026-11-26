import { createElement, useEffect, useRef, type CSSProperties } from "react"

import { BaseMapNotice } from "@/components/base-map-notice"
import { ChatPanel } from "@/components/chat/chat-panel"
import { Inspector } from "@/components/inspector/inspector"
import "@/components/layers/system-rows" // the Layers sheet's City systems rows
import { SearchMenu } from "@/components/search-menu"
import { AskBar } from "@/components/shell/ask-bar"
import { GlobalKeys } from "@/components/shell/global-keys"
import { LayersSheet } from "@/components/shell/layers-sheet"
import { MapControls } from "@/components/shell/map-controls"
import { NowLine } from "@/components/shell/now-line"
import { TopicCard } from "@/components/shell/topic-card"
import { TopicChips } from "@/components/shell/topic-chips"
import { Shortcuts } from "@/components/shortcuts"
import { SourcesPanel } from "@/components/sources/sources-panel"
import { Stage } from "@/components/stage"
import { CityClock } from "@/components/clock/city-clock"
import { CompareSwipe } from "@/components/clock/compare-swipe"
import "@/components/topics/builtin" // Buses, Parking, Traffic and Outside
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { startClock } from "@/lib/clock"
import { parseHash, startDeepLinks } from "@/lib/deeplink"
import { useGeometry } from "@/lib/geometry"
import { useLayout } from "@/lib/layout"
import { startCredits } from "@/lib/map/credits"
import { initMap, setInsets } from "@/lib/map/engine"
import "@/lib/mobility" // buses, stops and road speeds: their workspace, kinds, layers and commands
import "@/lib/watch" // the incident inbox, its Layers row, marks on the clock, toasts from the live stream
import { useWorkspace, useWorkspaceSide } from "@/lib/registries/workspaces"

// The open workspace's own panels, over the map (Live has none of its own), and its side panel
// in the right column (the Layers sheet takes the column while it is open).
function WorkspacePanels() {
  const panels = useWorkspace((s) => s.list.find((w) => w.id === s.active)?.Panels)
  const side = useWorkspaceSide()
  const layersOpen = useLayout((s) => s.layersOpen)
  return (
    <>
      {panels && createElement(panels)}
      {side && !layersOpen && createElement(side)}
    </>
  )
}

export function App() {
  const mapEl = useRef<HTMLDivElement>(null)
  const g = useGeometry()
  const chatOpen = useLayout((s) => s.chatOpen)
  const { top, right, bottom, left } = g.insets

  useEffect(() => {
    // A link brings back its camera and the thing it had open.
    const link = parseHash(location.hash)
    initMap(mapEl.current!, link.camera)
    startCredits()
    startDeepLinks(link)
    startClock()
  }, [])

  // The camera centres on the part of the map the panels leave open.
  useEffect(() => {
    setInsets({ top, right, bottom, left })
  }, [top, right, bottom, left])

  // MapLibre's own controls (the scale and the credits) sit just inside the panels.
  const vars = {
    "--inset-left": `${g.left}px`,
    "--inset-right": `${g.controlsRight}px`,
    "--inset-bottom": `${bottom}px`,
  } as CSSProperties

  return (
    <TooltipProvider delay={250}>
      <div className="map-root relative h-full w-full overflow-hidden bg-night" style={vars}>
        <h1 className="sr-only">Nicosia digital twin</h1>
        {/* MapLibre makes its container position: relative, so the box around it does the placing. */}
        <div className="absolute inset-0">
          <div ref={mapEl} role="region" aria-label="Map of Nicosia" className="size-full" />
        </div>
        {/* In reading and Tab order: top left, top right, the right column, bottom left. */}
        <TopicChips />
        {chatOpen && <ChatPanel />}
        <AskBar />
        <NowLine />
        <CityClock />
        <MapControls />
        <Inspector />
        <WorkspacePanels />
        <LayersSheet />
        <TopicCard />
        <Stage />
        <BaseMapNotice />
        <CompareSwipe />
        <SearchMenu />
        <Shortcuts />
        <SourcesPanel />
        <GlobalKeys />
        {/* Toasts from the twin's watch: announced politely, Alt T puts the keyboard on them. */}
        <Toaster
          position="top-center"
          offset={{ top: g.desktop ? 64 : g.belowClock + 4 }}
          mobileOffset={{ top: g.belowClock + 4, left: 8, right: 8 }}
          containerAriaLabel="Notifications"
          visibleToasts={4}
        />
      </div>
    </TooltipProvider>
  )
}

export default App
