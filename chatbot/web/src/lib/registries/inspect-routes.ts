/* Things that open somewhere other than the inspector. A chip in an answer, a map click, a
   search result or a link opens a thing by its id through `inspect`; a route claims ids of its
   own and opens them in its place (an incident, INC-12, opens in the incident inbox). A route
   that shows a thing also says which one, so the page's link names it (#/e/INC-12). */

export type InspectRoute = {
  id: string
  match: (id: string) => boolean
  open: (id: string, opts: { fly?: boolean; focus?: boolean; opener?: HTMLElement | null }) => void
  current?: () => string | null // the thing it shows now, for the page's link
  subscribe?: (fn: () => void) => () => void // tells when that changes
}

const routes: InspectRoute[] = []

export function registerInspectRoute(r: InspectRoute) {
  const i = routes.findIndex((x) => x.id === r.id)
  if (i >= 0) routes.splice(i, 1)
  routes.push(r)
}

export function routeFor(id: string) {
  return routes.find((r) => r.match(id)) ?? null
}

// The thing a route shows now, if any.
export function routedThing() {
  for (const r of routes) {
    const id = r.current?.()
    if (id) return id
  }
  return null
}

const watchers = new Set<() => void>()
const unsubscribers = new Map<string, () => void>()

// The page's link follows what the routes show. Routes registered later join in.
export function onRouted(fn: () => void) {
  watchers.add(fn)
  for (const r of routes) if (r.subscribe && !unsubscribers.has(r.id)) unsubscribers.set(r.id, r.subscribe(() => watchers.forEach((w) => w())))
  return () => { watchers.delete(fn) }
}
