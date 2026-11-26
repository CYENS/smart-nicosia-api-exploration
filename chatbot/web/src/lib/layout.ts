import { create } from "zustand"

// The calm layout: one thing per corner over a full-page map. Top left the topic chips, with the
// conversation (or, while it is hidden, the ask bar) under them; top right the now line, with
// the map's controls under it; bottom left the open topic's card. Panels in the right column
// (the Layers sheet, the inspector, a workspace's panel) start under the top band, as the
// conversation does.

// The top band the chips and the now line sit in: panels start under it (and a GAP).
export const STRIP_H = 52
export const GAP = 12
export const ROW_H = 40 // the chips, the now line and the ask bar
export const CHAT_W = 400
export const SIDE_W = 344 // every panel in the right column, and the topic card
export const LAYERS_W = SIDE_W
export const INSPECTOR_W = SIDE_W
export const WORKSPACE_W = SIDE_W
export const CONTROLS_W = 40 // the column of map controls under the now line
// The city clock's console, across the top of the open map while it is shown.
export const CLOCK_H = 86
// From this width the right column has room of its own beside the stage.
export const WIDE_PX = 1180
// A phone's top rows: the ask bar (while the conversation is hidden), the chips, the now line.
export const PHONE_ROW = 44
export const PHONE_GAP = 8

// Which floating panels are open. The map's camera padding and its controls follow these.
type Layout = {
  chatOpen: boolean
  layersOpen: boolean // the Layers sheet
  questionsOpen: boolean // the search menu (Ctrl K)
  searchSeed: string // what the search menu opens with
  shortcutsOpen: boolean
}

const desktopNow = () => typeof window === "undefined" || window.innerWidth >= 768

export const useLayout = create<Layout>(() => ({
  // The twin is chat-central: on a desktop the conversation is open from the start. A phone
  // starts on the map with the ask bar on top, and the conversation opens as a sheet on asking.
  chatOpen: desktopNow(),
  layersOpen: false,
  questionsOpen: false,
  searchSeed: "",
  shortcutsOpen: false,
}))

// What had the keyboard before the search menu opened: a result that opens the inspector hands
// it on, so closing the inspector goes back there (the menu's own row is gone by then).
let searchOpener: HTMLElement | null = null

export function rememberSearchOpener() {
  const a = document.activeElement as HTMLElement | null
  searchOpener = a && a !== document.body ? a : null
}

export function searchOpenerElement() {
  return searchOpener
}

export function openSearch(seed = "") {
  rememberSearchOpener()
  useLayout.setState({ questionsOpen: true, searchSeed: seed })
}

export function setChatOpen(chatOpen: boolean) {
  useLayout.setState({ chatOpen })
}
