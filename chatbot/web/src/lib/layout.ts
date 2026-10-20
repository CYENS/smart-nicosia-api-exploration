import { create } from "zustand"

export const STRIP_H = 52
export const GAP = 12
export const CHAT_W = 400
export const LAYERS_W = 264
// From this width the map panel has a column of its own beside the stage.
export const WIDE_PX = 1180

// Which floating panels are open. The map's camera padding and its controls follow these.
type Layout = {
  chatOpen: boolean
  layersOpen: boolean
  questionsOpen: boolean
}

export const useLayout = create<Layout>(() => ({
  chatOpen: true,
  layersOpen: typeof window === "undefined" || window.innerWidth >= WIDE_PX,
  questionsOpen: false,
}))
