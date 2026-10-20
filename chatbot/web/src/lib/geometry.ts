import { useEffect, useState } from "react"

import { useStage } from "@/lib/chat"
import { CHAT_W, GAP, LAYERS_W, STRIP_H, useLayout } from "@/lib/layout"
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

function useViewportHeight() {
  const [h, setH] = useState(() => window.innerHeight)
  useEffect(() => {
    const on = () => setH(window.innerHeight)
    window.addEventListener("resize", on)
    return () => window.removeEventListener("resize", on)
  }, [])
  return h
}

// Where the floating panels sit, in px from the edges of the map (which starts under the strip).
// The stage, the map's camera padding and its controls all read this, so they agree.
export function useGeometry() {
  const desktop = useMedia("(min-width: 768px)")
  const vh = useViewportHeight()
  const chatOpen = useLayout((s) => s.chatOpen)
  const layersOpen = useLayout((s) => s.layersOpen)
  const stage = {
    open: useStage((s) => s.open && s.items.length > 0),
    expanded: useStage((s) => s.expanded),
  }
  const timelapse = useMapUi((s) => s.timelapse !== null)

  const mapH = vh - STRIP_H
  const left = desktop && chatOpen ? GAP + CHAT_W + GAP : GAP
  const right = desktop && layersOpen ? GAP + LAYERS_W + GAP : GAP
  const stageH = desktop ? Math.round(Math.min(440, Math.max(280, vh * 0.4))) : Math.round(mapH * 0.4)
  const chatBottom = !desktop && chatOpen ? Math.round(mapH * 0.56) + 8 : 0
  return {
    desktop,
    left,
    right,
    stageH,
    stageOpen: stage.open,
    stageExpanded: stage.expanded,
    // Camera padding: the part of the map the panels leave open.
    insets: {
      top: timelapse ? 76 : 0,
      left: left - GAP,
      right: right - GAP,
      bottom: Math.max(chatBottom, stage.open && !stage.expanded && desktop ? stageH + GAP : 0),
    },
  }
}
