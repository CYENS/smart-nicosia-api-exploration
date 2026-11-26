/* Cards under an answer, from a tool's `ui` command: a feature registers a card for an action
   name (the watch's "watch_confirm": a proposed watch with Save and Not now), and the
   conversation draws it in the answer, where the tool ran. The card gets the command as the
   server sent it. */
import type { ComponentType } from "react"

export type TurnCardProps = { turnId: string; data: Record<string, unknown> }

const cards = new Map<string, ComponentType<TurnCardProps>>()

export function registerTurnCard(action: string, Card: ComponentType<TurnCardProps>) {
  cards.set(action, Card)
}

export function turnCard(action: string) {
  return cards.get(action) ?? null
}
