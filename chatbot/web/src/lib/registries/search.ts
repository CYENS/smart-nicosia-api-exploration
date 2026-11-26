/* Where Ctrl K looks. Each provider answers a query with items; the search menu groups them under
   their heading, providers in order. A later feature adds a provider (bus stops, boards, saved
   scenarios) with registerSearchProvider. */
import type { LucideIcon } from "lucide-react"

export type SearchItem = {
  key: string // unique in the menu
  title: string
  detail?: string
  icon: LucideIcon
  color?: string // a status dot beside the title
  group?: string // overrides the provider's heading
  disabled?: boolean
  opens?: boolean // opens something that takes the keyboard (the inspector): focus goes there
  run: () => void
}

export type SearchProvider = {
  id: string
  heading: string
  order: number
  // Without a query the menu shows the providers that answer an empty one (commands, recent).
  search: (q: string, signal: AbortSignal) => SearchItem[] | Promise<SearchItem[]>
  remote?: boolean // answers from the server: the menu shows a skeleton while it waits
}

const providers: SearchProvider[] = []

export function registerSearchProvider(p: SearchProvider) {
  const i = providers.findIndex((x) => x.id === p.id)
  if (i >= 0) providers.splice(i, 1)
  providers.push(p)
  providers.sort((a, b) => a.order - b.order)
}

export function searchProviders() {
  return [...providers]
}

// Local providers filter their own items: every word of the query in the item's words.
export function matches(q: string, ...texts: (string | undefined)[]) {
  const words = q.toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "").split(/\s+/).filter(Boolean)
  if (!words.length) return true
  const hay = texts.filter(Boolean).join(" ").toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "")
  return words.every((w) => hay.includes(w))
}
