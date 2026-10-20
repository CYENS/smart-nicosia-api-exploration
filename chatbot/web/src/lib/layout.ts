import { create } from "zustand"

// Which floating panels are open. The map's camera padding and its controls follow these.
type Layout = {
  chatOpen: boolean
  layersOpen: boolean
  questionsOpen: boolean
}

export const useLayout = create<Layout>(() => ({
  chatOpen: true,
  layersOpen: typeof window === "undefined" || window.innerWidth >= 768,
  questionsOpen: false,
}))

export const STRIP_H = 52
export const GAP = 12
export const CHAT_W = 400
export const LAYERS_W = 264
