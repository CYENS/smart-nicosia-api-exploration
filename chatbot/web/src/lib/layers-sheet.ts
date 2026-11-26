/* The Layers sheet's state: which City systems group is open (remembered on this device), and
   opening the sheet from anywhere (the map's layers button, a topic card, Ctrl K). */
import { create } from "zustand"

import { closeInspector, useInspector } from "@/lib/inspector"
import { useLayout } from "@/lib/layout"

const KEY = "twin.layers.group"

function readGroup(): string | null {
  try {
    const v = localStorage.getItem(KEY)
    if (v !== null) return v || null
  } catch { /* private mode */ }
  return "sensors"
}

export const useLayersSheet = create<{ group: string | null }>(() => ({ group: readGroup() }))

export function setLayersGroup(group: string | null) {
  useLayersSheet.setState({ group })
  try { localStorage.setItem(KEY, group ?? "") } catch { /* private mode */ }
}

// What had the keyboard when the sheet opened: closing it gives the keyboard back there.
let opener: HTMLElement | null = null

export function layersOpener() {
  return opener && opener.isConnected ? opener : null
}

// `from` names the opener when the keyboard is somewhere about to go away (the search box).
export function openLayers(group?: string, from?: Element | null) {
  const a = (from ?? document.activeElement) as HTMLElement | null
  opener = a instanceof HTMLElement && a !== document.body ? a : null
  if (group) setLayersGroup(group)
  if (useInspector.getState().open) closeInspector()
  // A phone shows one sheet at a time: the Layers sheet takes over from the conversation.
  const phone = !window.matchMedia("(min-width: 768px)").matches
  useLayout.setState(phone ? { layersOpen: true, chatOpen: false } : { layersOpen: true })
}

export function closeLayers() {
  useLayout.setState({ layersOpen: false })
}

// The inspector and the Layers sheet share the right column: opening one closes the other.
useInspector.subscribe((s, prev) => {
  if (s.open && !prev.open && useLayout.getState().layersOpen) useLayout.setState({ layersOpen: false })
})
