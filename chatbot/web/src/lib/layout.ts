import { create } from "zustand"

export const STRIP_H = 52
export const GAP = 12
export const RAIL_W = 48
export const CHAT_W = 400
export const LAYERS_W = 264
export const INSPECTOR_W = 344
// The city clock across the top of the map: the full console, or the compact pill.
export const CLOCK_H = 86
export const CLOCK_PILL_H = 40
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

export function openSearch(seed = "") {
  useLayout.setState({ questionsOpen: true, searchSeed: seed })
}
