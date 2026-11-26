/* The topic chips under the twin's ask bar: Buses, Parking, Traffic and Outside today. Picking
   one puts its layer on the map and opens its card at the bottom left: a summary in plain words,
   the legend, a question to ask and one way to go deeper. One topic at a time; picking another,
   or clearing it, puts back what the topic changed.

   A later feature adds a topic with registerTopic, or a section to an existing topic's card with
   registerTopicSection (the environment twin's surfaces under Outside, say). Only the first four
   topics by order get a chip, so the row stays calm; the rest are reached from Ctrl K. */
import type { LucideIcon } from "lucide-react"
import type { ComponentType } from "react"
import { create } from "zustand"

import { useWorkspace } from "@/lib/registries/workspaces"

export const MAX_CHIPS = 4

export type TopicLink = { label: string; run: () => void }

export type Topic = {
  id: string
  label: string // the chip, one word
  title?: string // the card's heading (by default the label and "now", or "then" in the past)
  icon: LucideIcon
  order: number
  description: string // for Ctrl K and the chip's tooltip
  sources?: string[] // where the card's data comes from (lib/sources.ts)
  // Puts the topic's layers on the map; returns how to put back what it changed.
  show: () => (() => void) | void
  Summary: ComponentType // the plain-words summary at the top of the card
  Legend?: ComponentType
  ask?: () => string // the question "Ask about it" sends to the conversation
  deeper?: () => TopicLink | null // one way to go deeper (a workspace, the Layers sheet)
}

export type TopicSection = { id: string; topic: string; order: number; Section: ComponentType }

type Topics = { list: Topic[]; sections: TopicSection[]; active: string | null }

export const useTopics = create<Topics>(() => ({ list: [], sections: [], active: null }))

export function registerTopic(t: Topic) {
  useTopics.setState((s) => ({
    list: [...s.list.filter((x) => x.id !== t.id), t].sort((a, b) => a.order - b.order),
  }))
}

export function registerTopicSection(sec: TopicSection) {
  useTopics.setState((s) => ({
    sections: [...s.sections.filter((x) => x.id !== sec.id), sec].sort((a, b) => a.order - b.order),
  }))
}

// The topics that get a chip.
export function chipTopics(list: Topic[]) {
  return list.slice(0, MAX_CHIPS)
}

let undo: (() => void) | null = null

// Open a topic (or, with null, close the open one). Its layers come on, the previous topic's go
// back to how they were.
export function selectTopic(id: string | null) {
  const { active, list } = useTopics.getState()
  if (id === active) return
  const was = undo
  undo = null
  was?.()
  const next = id ? list.find((t) => t.id === id) : null
  useTopics.setState({ active: next ? next.id : null })
  if (next) undo = next.show() ?? null
}

export function activeTopic() {
  const { active, list } = useTopics.getState()
  return list.find((t) => t.id === active) ?? null
}

// Opening a workspace goes deeper than a topic: the topic lets go first, so the workspace finds
// the map as the reader had it (and puts it back so when it closes).
useWorkspace.subscribe((s, prev) => {
  if (s.active !== prev.active && useTopics.getState().active) selectTopic(null)
})
