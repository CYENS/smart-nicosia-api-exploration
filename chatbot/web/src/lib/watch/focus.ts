/* Where the keyboard goes around the incident inbox: into it at its title (or at an incident's
   heading) when it opens, and back to whatever opened it when it closes (WAI-ARIA: focus returns
   to the opener), or to the Layers button when that was a row of the Layers sheet. */
import { openWorkspace, useWorkspace } from "@/lib/registries/workspaces"

let opener: HTMLElement | null = null
let pending: "title" | "detail" | null = null

export function rememberInboxOpener() {
  const a = document.activeElement
  if (a instanceof HTMLElement && a !== document.body && !a.closest("[data-workspace]")) opener = a
}

export function wantInboxFocus(where: "title" | "detail" | null) {
  pending = where
}

// True once for the place that was asked for.
export function takeInboxFocus(where: "title" | "detail") {
  if (pending !== where) return false
  pending = null
  return true
}

function visible(el: HTMLElement | null): el is HTMLElement {
  return !!el && el.isConnected && el.getClientRects().length > 0
}

export function leaveInbox() {
  const inside = !!document.activeElement?.closest("[data-workspace]")
  if (useWorkspace.getState().active === "incidents") openWorkspace("live")
  const back = opener
  opener = null
  if (!inside) return
  const target = visible(back) ? back
    : document.querySelector<HTMLElement>("[data-layers-button]") ?? document.querySelector<HTMLElement>(".maplibregl-canvas")
  requestAnimationFrame(() => target?.focus({ preventScroll: true }))
}
