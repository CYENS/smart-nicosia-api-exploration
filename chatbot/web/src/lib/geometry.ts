import { useEffect, useState } from "react"

import { useStage } from "@/lib/chat"
import { CHAT_W, GAP, LAYERS_W, STRIP_H, WIDE_PX, useLayout } from "@/lib/layout"
import { useMapUi } from "@/lib/map/engine"

function useMedia(query: string) {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const m = window.matchMedia(query)
    const on = () => setMatch(m.matches)
    m.addEventListener("change", on)
    return () => m.removeEventListener("change", on)
  }, [query])
  return match
}

function useViewport() {
  const read = () => ({ vw: window.innerWidth, vh: window.innerHeight })
  const [size, setSize] = useState(read)
  useEffect(() => {
    const on = () => setSize(read())
    window.addEventListener("resize", on)
    return () => window.removeEventListener("resize", on)
  }, [])
  return size
}

// Where the floating panels sit, in px from the edges of the map (which starts under the strip).
// The stage, the map's camera padding and its controls all read this, so they agree.
export function useGeometry() {
  const desktop = useMedia("(min-width: 768px)")
  // Wide enough for the map panel to keep its own column beside the stage.
  const wide = useMedia(`(min-width: ${WIDE_PX}px)`)
  const { vw, vh } = useViewport()
  const chatOpen = useLayout((s) => s.chatOpen)
  const layersOpen = useLayout((s) => s.layersOpen)
  const stage = {
    open: useStage((s) => s.open && s.items.length > 0),
    expanded: useStage((s) => s.expanded),
  }
  const timelapse = useMapUi((s) => s.timelapse !== null)

  const mapH = vh - STRIP_H
  // The conversation keeps 400 px where it can, and gives way on mid-size screens.
  const chatW = Math.round(Math.min(CHAT_W, vw * 0.42))
  const left = desktop && chatOpen ? GAP + chatW + GAP : GAP
  const layersColumn = desktop && layersOpen ? GAP + LAYERS_W + GAP : GAP
  // Narrower, the stage runs to the right edge and the map panel stops above it.
  const right = wide ? layersColumn : GAP
  const stageH = desktop ? Math.round(Math.min(440, Math.max(280, vh * 0.4))) : Math.round(mapH * 0.4)
  const chatBottom = !desktop && chatOpen ? Math.round(mapH * 0.56) + 8 : 0
  return {
    desktop,
    wide,
    chatW,
    left,
    right,
    stageH,
    stageOpen: stage.open,
    stageExpanded: stage.expanded,
    // What the map panel (or its button, when it's closed) takes from the top-right corner.
    topRight: desktop && layersOpen ? layersColumn : GAP + 150 + GAP,
    // Room the map panel leaves at the bottom for the stage, when they share the right side.
    layersBottom: !wide && stage.open && !stage.expanded ? stageH + GAP : 0,
    // Camera padding: the part of the map the panels leave open.
    insets: {
      top: timelapse ? 76 : 0,
      left: left - GAP,
      right: layersColumn - GAP,
      bottom: Math.max(chatBottom, stage.open && !stage.expanded && desktop ? stageH + GAP : 0),
    },
  }
}
