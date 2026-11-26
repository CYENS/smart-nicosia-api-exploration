import { useEffect, useState } from "react"

import { useStage } from "@/lib/chat"
import { useClock } from "@/lib/clock"
import { useInspector } from "@/lib/inspector"
import { CHAT_W, CLOCK_H, CLOCK_PILL_H, GAP, INSPECTOR_W, LAYERS_W, RAIL_W, STRIP_H, WIDE_PX, WORKSPACE_W, useLayout } from "@/lib/layout"
import { useWorkspaceSide } from "@/lib/registries/workspaces"

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

// Where the floating panels sit, in px from the edges of the page (the map starts under the strip
// and, on wider screens, right of the rail). The stage, the map's camera padding and its controls
// all read this, so they agree.
//
// The right column holds the map panel (or the open workspace's own panel) or, while something
// is open, the inspector in its place. On a phone the inspector (or the workspace's panel) is a
// sheet at the bottom where the conversation was.
export function useGeometry() {
  const desktop = useMedia("(min-width: 768px)")
  // Wide enough for the map panel to keep its own column beside the stage.
  const wide = useMedia(`(min-width: ${WIDE_PX}px)`)
  const { vw, vh } = useViewport()
  const chatOpen = useLayout((s) => s.chatOpen)
  const layersOpen = useLayout((s) => s.layersOpen)
  const inspecting = useInspector((s) => s.open)
  const wsSide = useWorkspaceSide() !== null
  const sheet = inspecting || wsSide // what takes the right column, or a phone's sheet
  const stage = {
    open: useStage((s) => s.open && s.items.length > 0),
    expanded: useStage((s) => s.expanded),
  }
  const clockH = useClock((s) => (s.expanded ? CLOCK_H : CLOCK_PILL_H))

  const rail = desktop ? RAIL_W : 0
  const mapH = vh - STRIP_H
  // The conversation keeps 400 px where it can, and gives way on mid-size screens, more so when
  // the inspector needs the right side too.
  const chatW = Math.round(Math.min(CHAT_W, vw * (sheet && !wide ? 0.34 : 0.42)))
  const left = rail + (desktop && chatOpen ? GAP + chatW + GAP : GAP)
  const inspectorW = Math.round(Math.min(INSPECTOR_W, vw * 0.3))
  const workspaceW = Math.round(Math.min(WORKSPACE_W, vw * 0.3))
  const sideW = inspecting ? inspectorW : wsSide ? workspaceW : LAYERS_W
  const sideColumn = desktop && (sheet || layersOpen) ? GAP + sideW + GAP : GAP
  // Narrower, the stage runs to the right edge and the right column stops above it.
  const right = wide ? sideColumn : GAP
  const stageH = desktop ? Math.round(Math.min(440, Math.max(280, vh * 0.4))) : Math.round(mapH * 0.4)
  const sheetH = Math.round(mapH * 0.52)
  const chatBottom = !desktop && (chatOpen || sheet) ? (sheet ? sheetH : Math.round(mapH * 0.56)) + 8 : 0
  return {
    desktop,
    vw,
    wide,
    rail,
    chatW,
    left,
    right,
    stageH,
    inspectorW,
    workspaceW,
    sheetH,
    inspecting,
    wsSide,
    stageOpen: stage.open,
    stageExpanded: stage.expanded,
    // What the right column (or the map panel's button, when it's closed) takes from the
    // top-right corner.
    topRight: desktop && (sheet || layersOpen) ? sideColumn : GAP + 150 + GAP,
    // Room the right column leaves at the bottom for the stage when they share the right side,
    // or, on a phone, for the inspector's sheet.
    layersBottom: !desktop && sheet ? sheetH + 8 : !wide && stage.open && !stage.expanded ? stageH + GAP : 0,
    // The city clock: across the open map at the top on a desktop (clear of the Conversation
    // button while the conversation is closed), across the phone's width.
    clockH,
    clockLeft: desktop ? (chatOpen ? left : rail + GAP + 150 + GAP) : 8,
    clockRight: desktop ? (sheet || layersOpen ? sideColumn : GAP + 150 + GAP) : 8,
    // Where panels that would sit under the clock on a phone start instead.
    belowClock: desktop ? STRIP_H + GAP : STRIP_H + 8 + clockH + 8,
    // Camera padding, from the edges of the map: the part of it the panels leave open.
    insets: {
      top: clockH + GAP,
      left: left - rail - GAP,
      right: sideColumn - GAP,
      bottom: Math.max(chatBottom, stage.open && !stage.expanded && desktop ? stageH + GAP : 0),
    },
  }
}
