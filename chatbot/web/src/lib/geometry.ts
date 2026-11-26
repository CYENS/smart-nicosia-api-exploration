import { useEffect, useState } from "react"

import { useStage } from "@/lib/chat"
import { useClock } from "@/lib/clock"
import { useInspector } from "@/lib/inspector"
import {
  CHAT_W, CLOCK_H, CONTROLS_W, GAP, PHONE_GAP, PHONE_ROW, ROW_H, SIDE_W, STRIP_H, WIDE_PX, useLayout,
} from "@/lib/layout"
import { useTopics } from "@/lib/registries/topics"
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

// A phone's chips row: the toggle group's own height.
export const PHONE_CHIPS_H = 36

// Where the floating panels sit, in px from the edges of the page (the map fills the page). The
// stage, the map's camera padding and its controls all read this, so they agree.
//
// Desktop: the left column holds the chips and, under them, the conversation (or the ask bar);
// the right column holds the Layers sheet, the inspector or the open workspace's panel, under
// the now line, with the map's controls just left of it. The city clock's console, while shown,
// runs across the top of the open map between the two.
//
// Phone: the ask bar (while the conversation is hidden), the chips and the now line in rows at
// the top, the map's controls down the right, and one sheet at a time at the bottom: the
// conversation, the inspector, a workspace's panel, the Layers sheet or a topic's card.
export function useGeometry() {
  const desktop = useMedia("(min-width: 768px)")
  // Wide enough for the right column to keep its own room beside the stage.
  const wide = useMedia(`(min-width: ${WIDE_PX}px)`)
  const { vw, vh } = useViewport()
  const chatOpen = useLayout((s) => s.chatOpen)
  const layersOpen = useLayout((s) => s.layersOpen)
  const inspecting = useInspector((s) => s.open)
  const wsSide = useWorkspaceSide() !== null
  const topic = useTopics((s) => s.active !== null)
  const stage = {
    open: useStage((s) => s.open && s.items.length > 0),
    expanded: useStage((s) => s.expanded),
    any: useStage((s) => s.items.length > 0),
  }
  const clockOpen = useClock((s) => s.expanded)
  const clockH = clockOpen ? CLOCK_H : 0

  const rail = 0 // the rail left the page; kept for the panels that still add it
  // The conversation keeps 400 px where it can, and gives way on mid-size screens, more so when
  // the right column is taken too.
  // Every panel in the right column shares one width (and the topic card has it too).
  const sideW = Math.round(Math.min(SIDE_W, vw * 0.32))
  const column = desktop && (inspecting || wsSide || layersOpen) ? sideW : 0
  const chatW = Math.round(Math.min(CHAT_W, vw * (column && !wide ? 0.34 : 0.42)))
  const left = desktop && chatOpen ? GAP + chatW + GAP : GAP
  const sideColumn = column ? GAP + column + GAP : GAP
  // Narrower, the stage runs to the right edge and the right column stops above it.
  const right = wide ? sideColumn : GAP
  // The map's controls: just left of the right column, under the now line.
  const controlsRight = desktop ? sideColumn : PHONE_GAP
  const mapH = vh
  const stageH = desktop ? Math.round(Math.min(440, Math.max(280, vh * 0.4))) : Math.round(mapH * 0.4)
  const sheetH = Math.round(mapH * 0.52)

  // A phone's rows at the top.
  const phoneAsk = !desktop && !chatOpen
  const chipsTop = desktop ? GAP : phoneAsk ? PHONE_GAP + PHONE_ROW + PHONE_GAP : PHONE_GAP
  const nowTop = desktop ? GAP : chipsTop + PHONE_CHIPS_H + PHONE_GAP
  const clockTop = desktop ? STRIP_H + GAP : nowTop
  // Where panels under the top rows start: under the now line (or the clock's console).
  const belowClock = desktop ? STRIP_H + GAP : nowTop + (clockOpen ? CLOCK_H : ROW_H) + PHONE_GAP
  const controlsTop = desktop ? STRIP_H + GAP : belowClock

  // One sheet at a time on a phone; the newest wins. The conversation's is 56% of the page.
  const phoneSheet = !desktop && (inspecting || wsSide || layersOpen)
  const cardShown = topic && (desktop ? !stage.expanded : !(chatOpen || phoneSheet))
  const chatBottom = !desktop && (chatOpen || phoneSheet) ? (phoneSheet ? sheetH : Math.round(vh * 0.56)) + PHONE_GAP : 0
  const cardH = useLayout((s) => s.cardH)
  const cardBottom = !desktop && cardShown ? (cardH || Math.round(vh * 0.34)) + PHONE_GAP : 0
  // The topic card sits over the stage (or its button) at the bottom left of the open map.
  const cardOffset = desktop ? (stage.open && !stage.expanded ? stageH + GAP : stage.any && !stage.open ? ROW_H + GAP : 0) : 0
  const bottomInset = Math.max(chatBottom, cardBottom, stage.open && !stage.expanded && desktop ? stageH + GAP : 0)
  return {
    desktop,
    vw,
    vh,
    wide,
    rail,
    chatW,
    left,
    right,
    column,
    controlsRight,
    controlsTop,
    stageH,
    sideW,
    inspectorW: sideW,
    workspaceW: sideW,
    sheetH,
    inspecting,
    wsSide,
    stageOpen: stage.open,
    stageExpanded: stage.expanded,
    chipsTop,
    nowTop,
    clockTop,
    cardShown,
    cardBottom: GAP + cardOffset,
    // What the right column and the map's controls take from the top-right corner.
    topRight: controlsRight + CONTROLS_W + GAP,
    // Room the right column leaves at the bottom for the stage when they share the right side,
    // or, on a phone, for the sheet.
    layersBottom: !desktop && phoneSheet ? sheetH + 8 : !wide && stage.open && !stage.expanded ? stageH + GAP : 0,
    // The city clock's console: across the open map under the top band on a desktop (clear of
    // the left column and the map's controls), across a phone's width under its rows.
    clockH,
    clockLeft: desktop ? GAP + chatW + GAP : PHONE_GAP,
    clockRight: desktop ? controlsRight + CONTROLS_W + GAP : PHONE_GAP,
    belowClock,
    // Camera padding, from the edges of the page: the part of the map the panels leave open.
    insets: {
      top: desktop ? STRIP_H + GAP + (clockOpen ? CLOCK_H + GAP : 0) : belowClock,
      left: desktop && chatOpen ? chatW + 2 * GAP : 0,
      right: desktop ? controlsRight + CONTROLS_W : 0,
      bottom: bottomInset,
    },
  }
}
