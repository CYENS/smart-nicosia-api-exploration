/* The twin's workspaces, shown on the navigation rail. A later feature adds its own with
   registerWorkspace (the city clock, a board, the scenario lab) and renders its panels when its
   workspace is the active one. */
import { RadioIcon, type LucideIcon } from "lucide-react"
import type { ComponentType } from "react"
import { create } from "zustand"

export type Workspace = {
  id: string
  label: string // on the rail, one or two words
  description: string // the rail's tooltip
  icon: LucideIcon
  order: number
  key?: string // Alt + this key opens it
  Panels?: ComponentType // drawn over the map while the workspace is open
  // Drawn in the right column in place of the map panel while the workspace is open (the
  // inspector still takes the column when something is opened); a sheet on a phone.
  Side?: ComponentType
  onEnter?: () => void // sets the map up for it
  onLeave?: () => void // puts back what onEnter changed
}

const workspaces: Workspace[] = []

export function registerWorkspace(w: Workspace) {
  const i = workspaces.findIndex((x) => x.id === w.id)
  if (i >= 0) workspaces.splice(i, 1)
  workspaces.push(w)
  workspaces.sort((a, b) => a.order - b.order)
  useWorkspace.setState({ list: [...workspaces] })
}

export const useWorkspace = create<{ active: string; list: Workspace[] }>(() => ({ active: "live", list: [] }))

export function openWorkspace(id: string) {
  const next = workspaces.find((w) => w.id === id)
  const { active } = useWorkspace.getState()
  if (!next || id === active) return
  workspaces.find((w) => w.id === active)?.onLeave?.()
  useWorkspace.setState({ active: id })
  next.onEnter?.()
}

// The open workspace's side panel, if it has one.
export function useWorkspaceSide() {
  return useWorkspace((s) => s.list.find((w) => w.id === s.active)?.Side ?? null)
}

registerWorkspace({
  id: "live", label: "Live", description: "Live today: the city as it is now", icon: RadioIcon, order: 0, key: "1",
})
