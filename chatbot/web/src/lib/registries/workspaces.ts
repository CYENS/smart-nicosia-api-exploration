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
  if (workspaces.some((w) => w.id === id)) useWorkspace.setState({ active: id })
}

registerWorkspace({
  id: "live", label: "Live", description: "Live today: the city as it is now", icon: RadioIcon, order: 0, key: "1",
})
