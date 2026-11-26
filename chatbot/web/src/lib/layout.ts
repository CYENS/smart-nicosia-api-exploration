import { create } from "zustand"

export const STRIP_H = 52
export const GAP = 12
export const RAIL_W = 48
export const CHAT_W = 400
export const LAYERS_W = 264
export const INSPECTOR_W = 344
// From this width the map panel has a column of its own beside the stage.
export const WIDE_PX = 1180

// Which floating panels are open. The map's camera padding and its controls follow these.
type Layout = {
  chatOpen: boolean
  layersOpen: boolean
  questionsOpen: boolean // the search menu (Ctrl K)
  searchSeed: string // what the search menu opens with
  shortcutsOpen: boolean
}

export const useLayout = create<Layout>(() => ({
  chatOpen: true,
  layersOpen: typeof window === "undefined" || window.innerWidth >= WIDE_PX,
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
